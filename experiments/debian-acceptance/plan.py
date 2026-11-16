"""A scripted agent harness for acceptance runs: plans instead of a model.

The daemon started by `launcher.py` installs `PlanHarness` as its only harness. Each
attempt of an agent run plays the next *action* queued for its agent principal in
`WORK/control/<agent_id>.json`, which the acceptance script writes:

    {"queue": [action, ...], "default": action}

An action is

    {"calls": [{"tool": "records.claim", "arguments": {...}}, ...],
     "parallel": false,       # make the calls at once instead of in order
     "sleep": 0,              # seconds to wait before the outcome
     "outcome": "succeed"}    # or "fail" (a failed attempt) or "crash" (the harness raises)

With an empty queue the `default` action is played; without one, the attempt succeeds and
calls nothing. Every attempt appends one line to `WORK/transcript.jsonl`: the task, the
attempt, the agent, what each call returned, and the `mailbox.inbox` block it was given.
Like a model, the harness reaches the kernel only through the attempt's MCP endpoint.
"""

import asyncio
import fcntl
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SUCCEED: dict[str, Any] = {"calls": [], "outcome": "succeed"}
INBOX = '<context operation="mailbox.inbox" status="ok"'


@contextmanager
def locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with (path.parent / (path.name + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def control_path(work: Path, agent_id: str) -> Path:
    return work / "control" / f"{agent_id}.json"


def set_plan(
    work: Path,
    agent_id: str,
    queue: list[dict[str, Any]] | None = None,
    default: dict[str, Any] | None = None,
) -> None:
    """Replace an agent's plan (used by the acceptance scripts)."""
    path = control_path(work, agent_id)
    with locked(path):
        path.write_text(json.dumps({"queue": queue or [], "default": default or SUCCEED}))


def next_action(work: Path, agent_id: str) -> dict[str, Any]:
    path = control_path(work, agent_id)
    with locked(path):
        try:
            plan = json.loads(path.read_text())
        except (OSError, ValueError):
            return dict(SUCCEED)
        if plan.get("queue"):
            action = plan["queue"].pop(0)
            path.write_text(json.dumps(plan))
            return dict(action)
        return dict(plan.get("default") or SUCCEED)


def transcript(work: Path) -> list[dict[str, Any]]:
    """Every attempt played so far, oldest first."""
    try:
        lines = (work / "transcript.jsonl").read_text().splitlines()
    except OSError:
        return []
    return [json.loads(line) for line in lines if line.strip()]


def inbox(prompt: str) -> dict[str, Any] | None:
    """The `mailbox.inbox` context block of a prompt, if it has one."""
    if INBOX not in prompt:
        return None
    body = prompt[prompt.index(INBOX) :].split(">", 1)[1].split("</context>", 1)[0]
    try:
        block = json.loads(body)
    except ValueError:
        return None
    return block if isinstance(block, dict) else None


def tool_name(operation: str, offered: tuple[str, ...]) -> str:
    """The MCP tool name the endpoint lists for an operation (dots become dashes)."""
    for candidate in (operation, operation.replace(".", "-")):
        if candidate in offered:
            return candidate
    return operation.replace(".", "-")


class PlanHarness:
    def __init__(self, work: Path, store: Path) -> None:
        self.work = work
        self.store = store

    def agent_of(self, task_id: str) -> str:
        conn = sqlite3.connect(f"file:{self.store}?mode=ro", uri=True, timeout=10)
        try:
            row = conn.execute(
                "SELECT agent_id FROM task_bindings WHERE task_id=?", (task_id,)
            ).fetchone()
        finally:
            conn.close()
        return str(row[0]) if row else "unknown"

    async def run(self, request: Any) -> Any:
        import httpx2
        from cardea.runtime.harness import HarnessResult
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        task_id, _, attempt = request.job_key.rpartition(":")
        agent = self.agent_of(task_id)
        action = next_action(self.work, agent)
        record: dict[str, Any] = {
            "task_id": task_id,
            "attempt": int(attempt) if attempt.isdigit() else attempt,
            "agent_id": agent,
            "action": action,
            "inbox": inbox(request.prompt),
            "calls": [],
        }
        headers = {"Authorization": f"Bearer {request.mcp_token}"}
        try:
            async with (
                httpx2.AsyncClient(headers=headers, timeout=600) as http,
                streamable_http_client(request.mcp_url, http_client=http) as streams,
                ClientSession(*streams) as session,
            ):
                await session.initialize()

                async def call(step: dict[str, Any]) -> dict[str, Any]:
                    name = tool_name(step["tool"], tuple(request.tool_names))
                    result = await session.call_tool(name, step.get("arguments", {}))
                    text = " ".join(str(getattr(c, "text", "")) for c in result.content)
                    failed = getattr(result, "is_error", getattr(result, "isError", False))
                    structured = getattr(
                        result, "structured_content", getattr(result, "structuredContent", None)
                    )
                    return {
                        "tool": step["tool"],
                        "is_error": bool(failed),
                        "structured": structured,
                        "text": text[:2000],
                    }

                steps = list(action.get("calls", []))
                if action.get("parallel"):
                    record["calls"] = list(await asyncio.gather(*(call(s) for s in steps)))
                else:
                    for step in steps:
                        record["calls"].append(await call(step))
            if action.get("sleep"):
                await asyncio.sleep(float(action["sleep"]))
        finally:
            outcome = str(action.get("outcome", "succeed"))
            record["outcome"] = outcome
            path = self.work / "transcript.jsonl"
            with locked(path), path.open("a") as stream:
                stream.write(json.dumps(record, default=str) + "\n")
        if outcome == "crash":
            raise ConnectionError("the scripted agent's process died (planned)")
        if outcome == "fail":
            return HarnessResult.failed("planned_failure", "Planned failure.")
        return HarnessResult(text="Done.", success=True, turns=1, cost_usd=0.0)
