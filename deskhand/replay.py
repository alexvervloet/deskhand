"""Reading a run back, and asking what a change would have done to it.

    python -m deskhand.replay <run_id>              # the trajectory
    python -m deskhand.replay <run_id> --at 7       # what the model saw at step 7
    python -m deskhand.replay <run_id> --diverge    # replay against the current config

Two capabilities, and they answer different questions.

**Replay** answers *what happened*. Because `transcript.rebuild()` is a pure
function of the step rows, the conversation as it stood before any step can be
reconstructed exactly, months later, byte for byte. Nothing is executed and
nothing is called: this is reading, not running.

**Divergence** answers *what would a change have done*. You edit the system
prompt, or point at a different model, and replay a recorded run against it:
the new model is asked to make each decision again, with the observations the
original run actually got, and the first place its choice differs is reported.

Divergence never executes a tool. When the replayed model asks for a call, the
*recorded* result of that call is handed back instead. That is what makes it
safe to run against a run that moved real money — and it is also the source of
its one hard limitation, below.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deskhand.db import connection, fetch_all, fetch_one
from deskhand.providers import Provider, get_provider
from deskhand.runtime import transcript
from deskhand.runtime.loop import SYSTEM_PROMPT
from deskhand.tools import api_schemas

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"
AMBER, GREEN, RED, BLUE = "\033[33m", "\033[32m", "\033[31m", "\033[34m"


# ------------------------------------------------------------------- loading


@dataclass(frozen=True, slots=True)
class RecordedTurn:
    """One model decision from a persisted run, with what followed it."""

    seq: int
    blocks: list[dict[str, Any]]
    stop_reason: str
    # (tool_use_id, name, args) for each call this turn asked for.
    calls: list[tuple[str, str, dict[str, Any]]]
    # tool_use_id -> the result that was recorded for it.
    results: dict[str, str]

    @property
    def signature(self) -> list[tuple[str, str]]:
        """What this turn *decided*, in a comparable form.

        Tool name plus canonical arguments. Deliberately not the prose: two
        runs that call `issue_refund` for the same amount have made the same
        decision even if they narrate it differently, and a divergence report
        that fired on rewording would be useless.
        """
        return [
            (name, json.dumps(args, sort_keys=True, separators=(",", ":"), default=str))
            for _, name, args in self.calls
        ]


def load(run_id: str) -> tuple[dict[str, Any], list[RecordedTurn]]:
    run = fetch_one(
        "select r.*, t.reference as ticket_reference from runs r"
        "  join tickets t on t.id = r.ticket_id where r.id = %s",
        (run_id,),
    )
    if run is None:
        raise SystemExit(f"no run {run_id}")

    steps = fetch_all(
        "select seq, kind::text, content, tool_name, cost_micros, latency_ms,"
        "       input_tokens, output_tokens"
        "  from steps where run_id = %s order by seq",
        (run_id,),
    )

    # Results are keyed by tool_use_id so a turn can find what followed it even
    # when several calls were made at once.
    results = {
        s["content"]["tool_use_id"]: s["content"].get("result", "")
        for s in steps
        if s["kind"] == "tool_result" and s["content"].get("tool_use_id")
    }

    turns = []
    for step in steps:
        if step["kind"] != "model_call":
            continue
        blocks = step["content"].get("blocks", [])
        calls = [
            (b["id"], b["name"], b.get("input") or {})
            for b in blocks
            if b.get("type") == "tool_use"
        ]
        turns.append(
            RecordedTurn(
                seq=step["seq"],
                blocks=blocks,
                stop_reason=step["content"].get("stop_reason", ""),
                calls=calls,
                results={tid: results.get(tid, "") for tid, _, _ in calls},
            )
        )
    return dict(run), turns


# -------------------------------------------------------------------- replay


def show(run_id: str) -> int:
    """Print the trajectory as it was recorded."""
    run, _ = load(run_id)
    steps = fetch_all(
        "select seq, kind::text, content, tool_name, cost_micros, latency_ms"
        "  from steps where run_id = %s order by seq",
        (run_id,),
    )

    print()
    print(f"{BOLD}run {run_id}{RESET}  ticket {run['ticket_reference']}")
    print(
        f"{DIM}{run['status']} ({run['stop_reason']})"
        f" · {run['provider']}/{run['model']} · attempt {run['attempt']}{RESET}"
    )
    print()

    # Approvals live in their own table, and a *granted* one writes no step —
    # only denials do. So a run that stopped, waited for a person, and was
    # allowed to continue would otherwise replay with no sign that the most
    # consequential thing in it ever happened. They are interleaved by the step
    # they gated.
    decisions: dict[int, list[dict[str, Any]]] = {}
    for row in fetch_all(
        "select a.step_seq, a.tool_name, a.status::text as status, a.preview,"
        "       a.reason, a.decided_at, u.email as decided_by"
        "  from approvals a left join users u on u.id = a.decided_by"
        " where a.run_id = %s order by a.created_at",
        (run_id,),
    ):
        decisions.setdefault(int(row["step_seq"]), []).append(row)

    for step in steps:
        kind, content = step["kind"], step["content"]
        head = f"  {step['seq']:>3}  "

        for decision in decisions.get(int(step["seq"]), []):
            colour = {"approved": GREEN, "denied": RED}.get(decision["status"], AMBER)
            who = decision["decided_by"] or "nobody"
            print(
                f"       {colour}⏸ {decision['status']}{RESET} by {who}"
                f"  {DIM}{decision['preview']}{RESET}"
            )
            if decision["reason"]:
                print(f"         {DIM}reason: {decision['reason']}{RESET}")

        if kind == "model_call":
            calls = [b for b in content.get("blocks", []) if b.get("type") == "tool_use"]
            text = " ".join(
                b.get("text", "") for b in content.get("blocks", []) if b.get("type") == "text"
            ).strip()
            print(f"{head}{DIM}model{RESET}  {content.get('stop_reason', '')}")
            for call in calls:
                print(f"       {BLUE}{call['name']}{RESET}({_args(call.get('input') or {})})")
            if text:
                print(f"       {text[:160]}")

        elif kind == "tool_result":
            ok = content.get("ok", True)
            mark = f"{GREEN}ok{RESET}" if ok else f"{RED}failed{RESET}"
            replayed = f" {AMBER}replayed{RESET}" if content.get("replayed") else ""
            print(f"{head}{content.get('name', step['tool_name'])}  {mark}{replayed}")
            print(f"       {DIM}{_oneline(content.get('result', ''))}{RESET}")

        elif kind == "approval":
            colour = GREEN if content.get("decision") == "approved" else RED
            print(
                f"{head}{colour}approval {content.get('decision')}{RESET}"
                f"  {content.get('tool_name')}"
                + (f" — {content['reason']}" if content.get("reason") else "")
            )

        elif kind == "final":
            print(f"{head}{GREEN}final{RESET}")
            print(f"       {_oneline(content.get('summary', ''))}")

        else:
            print(f"{head}{kind}  {_oneline(json.dumps(content))}")

    print()
    print(f"  {DIM}--at N to see the exact conversation before step N{RESET}")
    print()
    return 0


def at(run_id: str, seq: int) -> int:
    """Print the conversation exactly as it stood before step `seq`."""
    run, _ = load(run_id)
    with connection() as conn, conn.cursor() as cur:
        messages = transcript.rebuild(cur, run_id, run["prompt"], before_seq=seq)

    print()
    print(f"{BOLD}what the model saw before step {seq} of run {run_id}{RESET}")
    print(f"{DIM}reconstructed from the step log — no model was called{RESET}")
    print()
    for message in messages:
        role = message["role"]
        colour = AMBER if role == "user" else BLUE
        print(f"{colour}── {role} {'─' * (66 - len(role))}{RESET}")
        content = message["content"]
        if isinstance(content, str):
            print(content)
        else:
            for block in content:
                kind = block.get("type")
                if kind == "text":
                    print(block["text"])
                elif kind == "thinking":
                    print(f"{DIM}[thinking]{RESET}")
                elif kind == "tool_use":
                    print(f"{BLUE}{block['name']}{RESET}({_args(block.get('input') or {})})")
                elif kind == "tool_result":
                    flag = f" {RED}(error){RESET}" if block.get("is_error") else ""
                    print(f"{DIM}tool_result{RESET}{flag}")
                    print(str(block.get("content", "")))
        print()
    return 0


# ---------------------------------------------------------------- divergence


@dataclass
class Divergence:
    step: int | None
    original: list[tuple[str, str]]
    replayed: list[tuple[str, str]]
    matched_turns: int
    total_turns: int
    note: str = ""

    @property
    def diverged(self) -> bool:
        return self.step is not None


def diverge(run_id: str, provider: Provider, system: str) -> Divergence:
    """Replay a recorded run against a changed configuration.

    The new model is asked to make each decision again, given exactly the
    observations the original run got, and the first turn where its choice
    differs is reported.

    **The limitation, stated plainly:** once the replayed model asks for
    something the original run never asked for, there is no recorded result to
    hand back, and the replay stops. Divergence tells you *where* behaviour
    changed and not what would have happened afterwards — for that you have to
    let a real run go, with real tools and a real approval gate.
    """
    run, turns = load(run_id)
    if not turns:
        return Divergence(None, [], [], 0, 0, note="the run has no model calls to replay")

    tools = api_schemas()

    with connection() as conn, conn.cursor() as cur:
        for index, turn in enumerate(turns):
            # The conversation as it stood before this turn, built by the same
            # function the live loop uses. Up to the first divergence the
            # replayed decisions are identical to the recorded ones, so the
            # recorded history *is* the replayed history — which means this can
            # be read back rather than accumulated here.
            #
            # Reusing `rebuild` is the point. A second implementation of "what
            # the model saw" drifts from the first, and it drifted: the copy
            # this replaced dropped `is_error` from tool results and skipped
            # denial steps entirely, so a prompt tested against a run containing
            # a failure or a human "no" was tested against a run that never had
            # one.
            messages = transcript.rebuild(cur, run_id, run["prompt"], before_seq=turn.seq)
            reply = provider.complete(system, messages, tools)

            replayed_signature = [
                (
                    block["name"],
                    json.dumps(
                        block.get("input") or {},
                        sort_keys=True,
                        separators=(",", ":"),
                        default=str,
                    ),
                )
                for block in reply.content
                if block.get("type") == "tool_use"
            ]

            if replayed_signature != turn.signature:
                return Divergence(
                    step=turn.seq,
                    original=turn.signature,
                    replayed=replayed_signature,
                    matched_turns=index,
                    total_turns=len(turns),
                )

            if not turn.calls:
                # Both finished here, and agreed on finishing. Prose is not
                # compared: two runs that both stop are not diverging because
                # they worded the summary differently.
                return Divergence(None, [], [], index + 1, len(turns))

    return Divergence(None, [], [], len(turns), len(turns))


def report(run_id: str, result: Divergence) -> int:
    print()
    if result.note:
        print(f"  {DIM}{result.note}{RESET}\n")
        return 0

    if not result.diverged:
        print(
            f"  {GREEN}no divergence{RESET} — all {result.matched_turns} decision(s) matched"
        )
        print(f"  {DIM}run {run_id}{RESET}\n")
        return 0

    print(f"  {AMBER}diverged at step {result.step}{RESET}")
    print(f"  {DIM}{result.matched_turns} of {result.total_turns} decisions matched first{RESET}")
    print()
    print(f"  {DIM}originally:{RESET}")
    for name, args in result.original or [("(finished)", "")]:
        print(f"    {RED}{name}{RESET}({_short(args)})")
    print(f"  {DIM}now:{RESET}")
    for name, args in result.replayed or [("(finished)", "")]:
        print(f"    {GREEN}{name}{RESET}({_short(args)})")
    print()
    print(
        f"  {DIM}the replay stops here: there is no recorded observation for a call{RESET}"
    )
    print(f"  {DIM}the original run never made.{RESET}")
    print()
    return 1


# ------------------------------------------------------------------- helpers


def _args(args: dict[str, Any]) -> str:
    return ", ".join(f"{k}={_short(json.dumps(v, default=str))}" for k, v in args.items())


def _short(text: str, limit: int = 60) -> str:
    text = text.strip().strip('"')
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _oneline(text: str, limit: int = 120) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m deskhand.replay",
        description="Read a run back, or ask what a change would have done to it.",
    )
    parser.add_argument("run_id")
    parser.add_argument(
        "--at", type=int, metavar="N",
        help="print the conversation exactly as it stood before step N",
    )
    parser.add_argument(
        "--diverge", action="store_true",
        help="replay against the current configuration and report the first difference",
    )
    parser.add_argument(
        "--system-prompt", type=Path, metavar="FILE",
        help="use this system prompt for the replay instead of the current one",
    )
    args = parser.parse_args(argv)

    if args.at is not None:
        return at(args.run_id, args.at)

    if args.diverge or args.system_prompt:
        system = (
            args.system_prompt.read_text() if args.system_prompt else SYSTEM_PROMPT
        )
        return report(args.run_id, diverge(args.run_id, get_provider(), system))

    return show(args.run_id)


if __name__ == "__main__":
    sys.exit(main())
