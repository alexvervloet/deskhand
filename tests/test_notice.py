"""A run is told once, shortly before a ceiling stops it.

The ceilings stay exactly as hard as they were; these tests check that, too.
What changes is that the model can see the closest one in time to finish and
write its summary, rather than being cut off mid-task with nothing said.
"""

from __future__ import annotations

import json
from typing import LiteralString

import pytest

from deskhand.db import connection, fetch_all
from deskhand.providers import ScriptedProvider, call, text
from deskhand.runtime import loop, runs
from tests.test_runtime import drive, run_row, start_run, steps_of
from tests.test_transcript import Recording, assert_append_only

pytestmark = pytest.mark.usefixtures("fresh")

# Reads forever, and never the same read twice, so loop detection stays out of
# it and only a ceiling can stop the run.
ENDLESS = [[call("search_kb", query=f"shipping times {i}")] for i in range(40)]


def _set(run_id: str, statement: LiteralString, *params: object) -> None:
    """Run one literal `update runs ... where id = %s` against this run."""
    with connection() as conn, conn.cursor() as cur:
        cur.execute(statement, (*params, run_id))
        conn.commit()


def notices(run_id: str) -> list[str]:
    return [s["content"]["text"] for s in steps_of(run_id) if s["kind"] == "notice"]


def test_a_run_near_its_step_cap_is_told_once_and_still_stopped() -> None:
    run_id = start_run("NW-2")
    _set(run_id, "update runs set max_steps = %s where id = %s", 12)
    assert drive(run_id, ScriptedProvider(script=ENDLESS)) == "exhausted"

    told = notices(run_id)
    assert len(told) == 1, told
    assert "about 3 more turns" in told[0]
    # And it was true: three model turns followed it.
    seqs = [s["seq"] for s in steps_of(run_id)]
    notice_at = next(s["seq"] for s in steps_of(run_id) if s["kind"] == "notice")
    after = [s for s in steps_of(run_id) if s["seq"] > notice_at and s["kind"] == "model_call"]
    assert len(after) == 3, seqs
    # The cap is as hard as it was.
    assert run_row(run_id)["stop_reason"] == runs.STOP_STEP_CAP


def test_the_notice_reaches_the_model_after_the_turns_tool_results() -> None:
    run_id = start_run("NW-2")
    _set(run_id, "update runs set max_steps = %s where id = %s", 12)
    calls: list = []
    drive(run_id, Recording(ENDLESS, calls))

    told = notices(run_id)[0]
    with_notice = [sent for sent, _ in calls if told in json.dumps(sent)]
    assert with_notice, "the notice never reached the model"
    message = with_notice[0][-1]
    assert message["role"] == "user"
    kinds = [b["type"] for b in message["content"]]
    assert kinds[-1] == "text" and set(kinds[:-1]) == {"tool_result"}, kinds
    # And it changes nothing about the history before it.
    assert_append_only(calls)


def test_a_short_run_hears_nothing() -> None:
    run_id = start_run("NW-2")
    script = [[call("get_ticket", reference="NW-2")], text("Nothing to do.")]
    assert drive(run_id, ScriptedProvider(script=script)) == "succeeded"
    assert notices(run_id) == []


def test_a_run_near_its_deadline_is_told_in_seconds() -> None:
    run_id = start_run("NW-2")
    _set(run_id, "update runs set deadline_at = now() + interval '90 seconds' where id = %s")
    script = [[call("get_ticket", reference="NW-2")], text("Done.")]
    assert drive(run_id, ScriptedProvider(script=script)) == "succeeded"
    told = notices(run_id)
    assert len(told) == 1 and "seconds" in told[0], told


def test_a_run_near_its_spend_cap_is_told() -> None:
    run_id = start_run("NW-2")
    _set(run_id, "update runs set cost_micros = max_spend_micros * 9 / 10 where id = %s")
    script = [[call("get_ticket", reference="NW-2")], text("Done.")]
    assert drive(run_id, ScriptedProvider(script=script)) == "succeeded"
    told = notices(run_id)
    assert len(told) == 1 and "spending limit" in told[0], told


def test_a_resumed_run_is_not_told_twice() -> None:
    """The decision is a row. A worker that resumes finds it and says nothing."""
    run_id = start_run("NW-2")
    _set(run_id, "update runs set max_steps = %s where id = %s", 12)

    class DiesOnce(ScriptedProvider):
        died = False

        def complete(self, system, messages, tools):
            if not self.died and any(
                b.get("type") == "text" and "Runtime notice" in b.get("text", "")
                for m in messages
                if isinstance(m["content"], list)
                for b in m["content"]
            ):
                DiesOnce.died = True
                raise RuntimeError("worker died")
            return super().complete(system, messages, tools)

    with pytest.raises(RuntimeError):
        drive(run_id, DiesOnce(script=ENDLESS), worker="a")
    with connection() as conn:
        _set(run_id, "update runs set lease_expires_at = now() - interval '1 second' where id = %s")
        assert loop.advance(conn, run_id, "a", DiesOnce(script=ENDLESS)) == "exhausted"

    assert len(notices(run_id)) == 1
    assert fetch_all("select id from steps where run_id = %s and kind = 'notice'", (run_id,))
