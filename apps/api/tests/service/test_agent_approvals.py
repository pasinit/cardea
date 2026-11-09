"""Approval waits for agent runs (ADR-036 slice 4); portable fakes only.

DBOS is replaced by a client that records enqueued continuations; tests run each attempt
with `AgentRunRuntime.execute`, as the agent-run and continuation workflows would.
"""

import json
import time
from datetime import datetime, timedelta
from typing import Any, cast

import pytest
import test_agent_runs as runs
from mcp import types
from mcp.client.session import ClientSession
from test_agent_runs import GRANTS, FakeSession, Tools, call, calls, submit

from cardea.kernel.models import OperationState, TaskState
from cardea.runtime.agent_session import SessionRequest, SessionResult
from cardea.service import agent_run_runtime, approval_requests
from cardea.service.agent_run_runtime import AgentRunRuntime
from cardea.service.approval_requests import DecisionRequest
from cardea.service.auth import Proof
from cardea.service.database import Fault, borrow
from cardea.service.kernel import Kernel, provision
from cardea.service.models import RIGHTS, AgentRunRequest, Provisioning, Signal, now

kernel, session, tools = runs.kernel, runs.session, runs.tools
HOUR = timedelta(hours=1)
HELD = {"rule_id": "mail", "level": "always", "reason": "Mail needs approval"}


class Recorder:
    """Records continuations enqueued in the deciding transaction."""

    def __init__(self) -> None:
        self.enqueued: list[tuple[dict[str, Any], tuple[Any, ...]]] = []

    def enqueue_in_transaction(self, conn: Any, options: dict[str, Any], *args: Any) -> None:
        self.enqueued.append((options, args))

    def send_in_transaction(self, *args: Any, **kwargs: Any) -> None:
        pass

    def destroy(self) -> None:
        pass

    def continuations(self) -> list[tuple[dict[str, Any], tuple[Any, ...]]]:
        return [e for e in self.enqueued if ".approval." in e[0]["workflow_id"]]


def setup(
    kernel: Kernel, *, rule: dict[str, Any] | None = None, supervisor_approves: bool = True
) -> Recorder:
    """Provision approvers, an approval rule on mail.send and a reviewer agent."""
    expires = (now() + HOUR).isoformat()
    principals = [
        {
            "principal_id": p,
            "kind": "service" if p == "runtime" else "human",
            "auth_kind": "spiffe",
            "issuer": "test",
            "subject": p,
        }
        for p in ("runtime", "supervisor", "outsider", "approver")
    ] + [
        {"principal_id": a, "kind": "agent", "runtime_id": "runtime"}
        for a in ("agent", "reviewer")
    ]
    permissions: list[dict[str, Any]] = [
        {
            "principal_id": "supervisor",
            "namespace": "test",
            "operation": right,
            "delegate": True,
            "approve": supervisor_approves and right == "mail.send",
            "expires_at": expires,
        }
        for right in GRANTS
    ]
    for who in ("approver", "reviewer", "agent"):
        permissions.append(
            {
                "principal_id": who,
                "namespace": "test",
                "operation": "mail.send",
                "allow": False,
                "approve": True,
                "expires_at": expires,
            }
        )
    platform = [
        {
            "principal_id": "platform",
            "namespace": "test",
            "operation": right,
            "approve": right == "mail.send",
            "expires_at": expires,
        }
        for right in GRANTS
    ]
    entry = {"namespace": "test", "operation": "mail.send", "rules": [rule or HELD]}
    provision(
        kernel.database,
        Provisioning.model_validate(
            {
                "principals": principals,
                "permissions": permissions,
                "platform": platform,
                "approvals": {"entries": [entry]},
            }
        ),
        kernel.operations,
    )
    recorder = Recorder()
    kernel.client = cast(Any, recorder)
    return recorder


def approver() -> Proof:
    return Proof("spiffe", "test", "approver", time.time() + 60)


def held_row(kernel: Kernel, task_id: str) -> dict[str, Any]:
    with kernel.database.read() as conn:
        rows = conn.exec_driver_sql(
            "SELECT * FROM approval_requests WHERE task_id=? ORDER BY created_at", (task_id,)
        ).mappings()
        return [dict(r) for r in rows][-1]


def decide(kernel: Kernel, row: dict[str, Any], decision: str = "approve", **body: Any) -> Any:
    return approval_requests.decide(
        kernel,
        approver(),
        str(row["request_id"]),
        DecisionRequest.model_validate(
            {
                "decision_id": f"human-{decision}-{row['request_id'][-6:]}",
                "decision": decision,
                "intent_digest": row["intent_digest"],
                **body,
            }
        ),
    )


def task_of(kernel: Kernel, task_id: str) -> Any:
    with kernel.database.read() as conn, borrow(conn) as tx:
        return tx.get_task(task_id)


def text(result: types.CallToolResult) -> str:
    return " ".join(cast(types.TextContent, c).text for c in result.content)


async def proposes(client: ClientSession, request: SessionRequest) -> SessionResult:
    held = await call(client, "mail-send", to="user@example.invalid", body="recap")
    assert not held.is_error, held.content
    assert held.structured_content is not None
    assert held.structured_content["status"] == "approval_pending"
    assert "Do not call it again" in text(held)
    return SessionResult(text="Asked for approval to send the recap.", is_error=False)


ONCE = [{"operation": "mail.send", "min_calls": 1, "max_calls": 1}]


# 1. Approve: the continuation runs apr-01 and the run resumes with its result


def test_approve_resumes_the_run_with_the_result(
    kernel: Kernel, tools: Tools, session: FakeSession
) -> None:
    recorder = setup(kernel)
    prompts: list[str] = []

    async def first(client: ClientSession, request: SessionRequest) -> SessionResult:
        result = await proposes(client, request)
        # The held call used no tool-call budget: the one allowed call still runs.
        found = await call(client, "listings-search")
        assert not found.is_error, found.content
        return result

    async def resumed(client: ClientSession, request: SessionRequest) -> SessionResult:
        prompts.append(request.prompt)
        return SessionResult(text="Recap sent after approval.", is_error=False)

    session.scripts = [first, resumed]
    task_id = submit(kernel, expect=ONCE, max_tool_calls=1)
    # Expectations are not evaluated while a request is open: the run waits.
    assert AgentRunRuntime(kernel).execute(task_id) == "waiting"
    task = task_of(kernel, task_id)
    assert task.state is TaskState.WAITING
    row = held_row(kernel, task_id)
    assert (row["state"], row["operation_id"], row["on_expiry"]) == ("pending", "apr-01", "resume")
    assert tools.sent == [] and [c.request.operation_id for c in calls(kernel, task_id)] == [
        "call-0001"
    ]
    assert decide(kernel, row)["state"] == "approved"
    assert task_of(kernel, task_id).state is TaskState.QUEUED
    ((options, args),) = recorder.continuations()
    assert options["workflow_name"] == "cardea.m6.agent_run.approval"
    assert options["queue_name"] == "cardea-agent-runs"
    assert options["workflow_id"] == f"cardea:{task_id}.approval.{row['request_id']}"
    assert args == (task_id, row["request_id"])

    assert AgentRunRuntime(kernel).execute(task_id) == "succeeded"
    assert len(tools.sent) == 1
    executed = held_row(kernel, task_id)
    assert executed["state"] == "executed"
    (apr,) = [c for c in calls(kernel, task_id) if c.request.operation_id == "apr-01"]
    assert apr.state is OperationState.SUCCEEDED and apr.receipt_id == executed["receipt_id"]
    assert cast(Any, apr.request.arguments)["context"]["approval"]["approver_id"] == "approver"
    (prompt,) = prompts
    assert f'<approval request="{row["request_id"]}" operation="mail.send"' in prompt
    assert 'decision="approved" by="approver"' in prompt
    assert "message_id" in prompt and "resumes after an approval wait" in prompt
    # A repeated continuation does nothing more.
    assert AgentRunRuntime(kernel).execute(task_id) == "succeeded"
    assert len(tools.sent) == 1


# 3. Reject: no effect, the run is told with the escaped reason


def test_reject_tells_the_run_and_runs_nothing(
    kernel: Kernel, tools: Tools, session: FakeSession
) -> None:
    setup(kernel)
    prompts: list[str] = []

    async def resumed(client: ClientSession, request: SessionRequest) -> SessionResult:
        prompts.append(request.prompt)
        return SessionResult(text="The recap was not approved.", is_error=False)

    session.scripts = [proposes, resumed]
    task_id = submit(kernel)
    assert AgentRunRuntime(kernel).execute(task_id) == "waiting"
    row = held_row(kernel, task_id)
    assert decide(kernel, row, "reject", reason='Not now "<b>"')["state"] == "rejected"
    assert AgentRunRuntime(kernel).execute(task_id) == "succeeded"
    assert tools.sent == []
    assert [c.request.operation_id for c in calls(kernel, task_id)] == []
    (prompt,) = prompts
    assert 'decision="rejected"' in prompt
    assert 'reason="Not now &quot;&lt;b&gt;&quot;"' in prompt and "<b>" not in prompt
    assert "untrusted data" in prompt
