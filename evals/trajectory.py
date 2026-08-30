"""A completed run, in a shape you can make claims about.

The point of this file is that trajectory evals should read like the sentence
they are checking. `path.executed("issue_refund") == 0` is a claim about what
the agent did; `len([s for s in steps if s["tool_name"] == ...]) == 0` is a
claim about a list comprehension.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from deskhand.db import fetch_all, fetch_one
from deskhand.runtime import transcript


@dataclass
class Trajectory:
    run: dict[str, Any]
    steps: list[dict[str, Any]]
    approvals: list[dict[str, Any]]
    invocations: list[dict[str, Any]]

    @classmethod
    def load(cls, run_id: str) -> Trajectory:
        run = fetch_one("select * from runs where id = %s", (run_id,))
        assert run is not None, f"no run {run_id}"
        return cls(
            run=run,
            steps=fetch_all("select * from steps where run_id = %s order by seq", (run_id,)),
            approvals=fetch_all(
                "select * from approvals where run_id = %s order by created_at", (run_id,)
            ),
            invocations=fetch_all(
                "select * from tool_invocations where run_id = %s order by created_at",
                (run_id,),
            ),
        )

    # ------------------------------------------------------------- outcome

    @property
    def status(self) -> str:
        return self.run["status"]

    @property
    def stop_reason(self) -> str | None:
        return self.run["stop_reason"]

    @property
    def summary(self) -> str:
        for step in reversed(self.steps):
            if step["kind"] == "final":
                return str(step["content"].get("summary", ""))
        return ""

    # ---------------------------------------------------------------- path

    @property
    def path(self) -> list[str]:
        """Tool names in the order they actually executed."""
        return [s["tool_name"] for s in self.steps if s["kind"] == "tool_result"]

    def executed(self, tool: str) -> int:
        """How many times a tool *ran*. A request that was never approved, or
        was denied, is not an execution — which is the distinction most of
        these evals turn on."""
        return sum(
            1
            for inv in self.invocations
            if inv["tool_name"] == tool and inv["status"] == "succeeded"
        )

    def requested(self, tool: str) -> int:
        """How many times the model *asked* for a tool, executed or not."""
        asked = 0
        for step in self.steps:
            if step["kind"] != "model_call":
                continue
            for block in step["content"].get("blocks", []):
                if block.get("type") == "tool_use" and block.get("name") == tool:
                    asked += 1
        return asked

    def called_before(self, first: str, second: str) -> bool:
        path = self.path
        return first in path and second in path and path.index(first) < path.index(second)

    def result_of(self, tool: str) -> str:
        for step in self.steps:
            if step["kind"] == "tool_result" and step["tool_name"] == tool:
                return str(step["content"].get("result", ""))
        return ""

    def failures(self) -> list[str]:
        return [
            str(s["content"].get("result", ""))
            for s in self.steps
            if s["kind"] == "tool_result" and s["content"].get("ok") is False
        ]

    def replayed(self) -> list[str]:
        return [
            str(s["tool_name"])
            for s in self.steps
            if s["kind"] == "tool_result" and s["content"].get("replayed")
        ]

    # ------------------------------------------------------------ approvals

    def approvals_for(self, tool: str) -> list[dict[str, Any]]:
        return [a for a in self.approvals if a["tool_name"] == tool]

    def gated(self, tool: str) -> bool:
        """Did every execution of this tool have an approval behind it?"""
        approved = sum(
            1 for a in self.approvals if a["tool_name"] == tool and a["status"] == "approved"
        )
        return self.executed(tool) <= approved

    # ----------------------------------------------------------- integrity

    def messages(self) -> list[dict[str, Any]]:
        """The conversation as the model saw it, rebuilt from the step log."""
        from deskhand.db import connection

        with connection() as conn, conn.cursor() as cur:
            return transcript.rebuild(cur, str(self.run["id"]), self.run["prompt"])

    def model_saw(self, needle: str) -> bool:
        return needle in str(self.messages())

    def unfenced_tool_results(self) -> list[str]:
        """Tool output that reached the model without a fence around it.

        Should always be empty. If it ever is not, untrusted text is arriving
        indistinguishable from the runtime's own words.
        """
        token = transcript.fence_token(str(self.run["id"]))
        opener = f"<<<untrusted:{token}>>>"
        loose = []
        for message in self.messages():
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if block.get("type") != "tool_result":
                    continue
                text = str(block.get("content", ""))
                if not text.startswith(opener):
                    loose.append(text[:80])
        return loose
