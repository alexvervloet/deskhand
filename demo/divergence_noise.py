#!/usr/bin/env python
"""How much of a divergence report is the prompt, and how much is the model.

    python demo/divergence_noise.py <run_id> [--samples 4]

`deskhand.replay --diverge` answers "would this prompt have decided
differently". It answers it by re-asking the model for one turn and comparing
the result to what was recorded, so the answer is only worth anything if the
model would otherwise have repeated itself. This measures whether it does.

Two batches against the same recorded turn: one with the current system prompt,
one with a changed prompt. Each reports how often the agent still asked for the
money, and what shapes the turn took. The recorded run supplies the
observations, so both batches decide with exactly the context the original had.

Every number is counted from the replies, not asserted, so read the output
rather than this docstring for the figures. The shape of the result on the run
this was written against, over thirteen changed and ten control samples: the
control asked for the refund every time, the changed prompt never did, and most
or all of the control samples chose the identical two tools with the identical
amount as the recorded turn.

That last part is the point. `--diverge` reported those identical control
decisions as divergences, every time, because an internal note was worded
differently. The decision is stable and reportable; the prose around it is not,
and the comparison cannot tell them apart. See the `signature` docstring in
deskhand/replay.py, which claims the behaviour this measures the absence of.

**This spends money.** It is `samples * 2` model calls, on a real key, and it
refuses to run against the scripted provider — a mock that ignores the system
prompt would report perfect stability and perfect insensitivity, which are both
artefacts of the mock rather than facts about anything.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deskhand.config import settings  # noqa: E402
from deskhand.db import connection  # noqa: E402
from deskhand.providers import ClaudeProvider  # noqa: E402
from deskhand.replay import RecordedTurn, load  # noqa: E402
from deskhand.runtime import transcript  # noqa: E402
from deskhand.runtime.loop import SYSTEM_PROMPT  # noqa: E402
from deskhand.tools import api_schemas  # noqa: E402

logging.disable(logging.CRITICAL)

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"
AMBER, GREEN, RED = "\033[33m", "\033[32m", "\033[31m"

DEFAULT_PROMPT = Path("examples/escalate-instead-of-refunding.txt")
# The act the changed prompt forbids. Presence or absence of this in a turn is
# the one thing worth counting: it is the decision, as opposed to the narration
# around it.
WATCHED = "issue_refund"


def decision_turn(turns: list[RecordedTurn]) -> RecordedTurn:
    """The last turn that asked for anything.

    Divergence is only interesting where a decision was made, and the last
    tool-calling turn of a recorded run is where the run committed to
    something. Earlier turns are the investigation that led there.
    """
    calling = [t for t in turns if t.calls]
    if not calling:
        raise SystemExit("that run made no tool calls; there is no decision to re-ask")
    return calling[-1]


def sample(
    system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]], n: int
) -> tuple[int, Counter[str]]:
    provider = ClaudeProvider()
    watched: int = 0
    shapes: Counter[str] = Counter()
    for _ in range(n):
        reply = provider.complete(system, messages, tools)
        names = [b["name"] for b in reply.content if b.get("type") == "tool_use"]
        watched += WATCHED in names
        shapes["+".join(names) or "(no tool calls)"] += 1
    return watched, shapes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python demo/divergence_noise.py",
        description="Measure how much of a divergence report is the prompt and how much is the model.",
    )
    parser.add_argument("run_id")
    parser.add_argument("--samples", type=int, default=4, metavar="N")
    parser.add_argument("--system-prompt", type=Path, default=DEFAULT_PROMPT)
    args = parser.parse_args(argv)

    if not settings.has_model_key:
        print(
            f"{RED}This needs a real model.{RESET} The scripted provider ignores the system"
            "\nprompt and replays a fixed trajectory, so it would report that nothing"
            "\never varies and that no prompt ever matters. Both would be facts about"
            "\nthe mock. Set ANTHROPIC_API_KEY."
        )
        return 2

    run, turns = load(args.run_id)
    turn = decision_turn(turns)
    with connection() as conn, conn.cursor() as cur:
        messages = transcript.rebuild(cur, args.run_id, run["prompt"], before_seq=turn.seq)
    tools = api_schemas()

    print()
    print(f"{BOLD}run {args.run_id}{RESET}  {DIM}{run['model']}{RESET}")
    print(f"recorded step {turn.seq}: {'+'.join(n for n, _ in turn.signature)}")
    print(f"{DIM}{args.samples * 2} model calls{RESET}\n")

    for label, system in (
        ("control", SYSTEM_PROMPT),
        (f"changed ({args.system_prompt.name})", args.system_prompt.read_text()),
    ):
        watched, shapes = sample(system, messages, tools, args.samples)
        colour = RED if watched else GREEN
        print(f"  {BOLD}{label}{RESET}")
        print(f"    {colour}{WATCHED} in {watched}/{args.samples}{RESET}")
        for shape, n in shapes.most_common():
            marker = AMBER if shape == "+".join(n for n, _ in turn.signature) else DIM
            print(f"      {n}x  {marker}{shape}{RESET}")
        print()

    print(
        f"{DIM}A shape in amber matched the recorded turn's tools exactly. Any of those"
        f"\nthat --diverge still called divergent, it called divergent on prose.{RESET}\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
