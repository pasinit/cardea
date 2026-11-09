"""Approval waits for agent runs (ADR-036 slice 4); portable fakes only.

DBOS is replaced by a client that records enqueued continuations; tests run each attempt
with `AgentRunRuntime.execute`, as the agent-run and continuation workflows would.
"""

import json
import time
from datetime import timedelta
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


# 4. Expiry: resume tells the run; fail fails it


def overdue(kernel: Kernel, row: dict[str, Any]) -> dict[str, str]:
    """Backdate the request's expiry, then tick at real time.

    A tick at a future clock would stamp the woken task in the future and later real-time
    transitions would regress, so the test database drops the intent trigger instead.
    """
    with kernel.database.engine.begin() as conn:
        conn.exec_driver_sql("DROP TRIGGER IF EXISTS approval_intent_immutable")
        conn.exec_driver_sql(
            "UPDATE approval_requests SET expires_at=? WHERE request_id=?",
            ((now() - timedelta(seconds=1)).isoformat(), row["request_id"]),
        )
    return approval_requests.expire_due(kernel)


EXPIRING = {**HELD, "expires_after_seconds": 60}


def test_expiry_with_resume_wakes_the_run_and_tells_it(
    kernel: Kernel, tools: Tools, session: FakeSession
) -> None:
    recorder = setup(kernel, rule=EXPIRING)
    prompts: list[str] = []

    async def resumed(client: ClientSession, request: SessionRequest) -> SessionResult:
        prompts.append(request.prompt)
        return SessionResult(text="The approval expired; nothing was sent.", is_error=False)

    session.scripts = [proposes, resumed]
    task_id = submit(kernel)
    assert AgentRunRuntime(kernel).execute(task_id) == "waiting"
    row = held_row(kernel, task_id)
    assert overdue(kernel, row) == {
        row["request_id"]: "expired"
    }
    assert task_of(kernel, task_id).state is TaskState.QUEUED
    assert len(recorder.continuations()) == 1
    assert AgentRunRuntime(kernel).execute(task_id) == "succeeded"
    assert tools.sent == [] and 'decision="expired"' in prompts[0]
    with pytest.raises(Fault) as late:
        decide(kernel, row)
    assert late.value.code == "approval_expired"


def test_expiry_with_fail_fails_the_waiting_run(
    kernel: Kernel, tools: Tools, session: FakeSession
) -> None:
    recorder = setup(kernel, rule={**EXPIRING, "on_expiry": "fail"})
    session.scripts = [proposes]
    task_id = submit(kernel)
    assert AgentRunRuntime(kernel).execute(task_id) == "waiting"
    row = held_row(kernel, task_id)
    overdue(kernel, row)
    task = task_of(kernel, task_id)
    assert task.state is TaskState.FAILED and task.failure.code == "approval_expired"
    assert recorder.continuations() == [] and tools.sent == []


def test_expiry_during_the_attempt_applies_after_it_parks(
    kernel: Kernel, tools: Tools, session: FakeSession
) -> None:
    """A request that closes while its attempt still runs is not lost: the attempt parks,
    finds the closure already committed, and the continuation applies `fail`."""
    recorder = setup(kernel, rule={**EXPIRING, "on_expiry": "fail"})

    async def expires_meanwhile(client: ClientSession, request: SessionRequest) -> SessionResult:
        result = await proposes(client, request)
        row = held_row(kernel, request.job_key.split(":")[0])
        assert overdue(kernel, row) == {
            row["request_id"]: "expired"
        }
        return result

    session.scripts = [expires_meanwhile]
    task_id = submit(kernel)
    assert AgentRunRuntime(kernel).execute(task_id) == "waiting"
    # The closure was committed before the park, so the park woke the task at once.
    assert task_of(kernel, task_id).state is TaskState.QUEUED
    assert len(recorder.continuations()) == 1
    assert AgentRunRuntime(kernel).execute(task_id) == "failed"
    assert task_of(kernel, task_id).failure.code == "approval_expired"
    assert tools.sent == []


# 11 / 16. One open request, the same intent returns it, and the wait cap


def test_open_request_rules_and_the_wait_cap(
    kernel: Kernel, tools: Tools, session: FakeSession
) -> None:
    setup(kernel)

    async def first(client: ClientSession, request: SessionRequest) -> SessionResult:
        a = await call(client, "mail-send", to="user@example.invalid", body="recap")
        same = await call(client, "mail-send", to="user@example.invalid", body="recap")
        assert a.structured_content and same.structured_content
        assert same.structured_content["request_id"] == a.structured_content["request_id"]
        other = await call(client, "mail-send", to="other@example.invalid", body="recap")
        assert other.is_error and "approval_pending" in text(other)
        return SessionResult(text="Waiting.", is_error=False)

    async def again(client: ClientSession, request: SessionRequest) -> SessionResult:
        capped = await call(client, "mail-send", to="other@example.invalid", body="retry")
        assert capped.is_error and "approval_limit" in text(capped), capped.content
        return SessionResult(text="Could not ask again.", is_error=False)

    session.scripts = [first, again]
    task_id = submit(kernel, max_approval_waits=1)
    assert AgentRunRuntime(kernel).execute(task_id) == "waiting"
    with kernel.database.read() as conn:
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM approval_requests").scalar_one() == 1
    decide(kernel, held_row(kernel, task_id), "reject")
    assert AgentRunRuntime(kernel).execute(task_id) == "succeeded"
    assert tools.sent == []


def test_wait_fields_bound_the_run_and_keep_old_digests(kernel: Kernel) -> None:
    plain = runs.run_request()
    assert "max_approval_waits" not in plain.model_dump(mode="json")
    assert plain.approval_waits == 4
    with pytest.raises(ValueError):
        runs.run_request(max_approval_waits=17)
    with pytest.raises(ValueError):
        runs.run_request(approval_wait_seconds=30 * 24 * 3600 + 1)
    setup(kernel)
    base = task_of(kernel, submit(kernel, request_id="plain"))
    longer = task_of(kernel, submit(kernel, request_id="longer", approval_wait_seconds=86400))
    extra = longer.limits.deadline - base.limits.deadline
    assert timedelta(hours=23, minutes=59) < extra < timedelta(hours=24, minutes=1)


# 5. Crash while parked; 8. lease loss after proposing


def test_restart_while_parked_keeps_the_wait(
    kernel: Kernel, tools: Tools, session: FakeSession
) -> None:
    setup(kernel)

    async def resumed(client: ClientSession, request: SessionRequest) -> SessionResult:
        return SessionResult(text="Sent after approval.", is_error=False)

    session.scripts = [proposes, resumed]
    task_id = submit(kernel)
    assert AgentRunRuntime(kernel).execute(task_id) == "waiting"
    # A recovered agent-run workflow finds the run parked and starts nothing.
    assert AgentRunRuntime(kernel).execute(task_id) == "waiting"
    assert len(session.requests) == 1
    kernel.database.close()
    database = runs.Database(kernel.database.directory)
    kernel.database = database
    assert task_of(kernel, task_id).state is TaskState.WAITING
    decide(kernel, held_row(kernel, task_id))
    assert AgentRunRuntime(kernel).execute(task_id) == "succeeded"
    assert len(tools.sent) == 1


class Lost(Exception):
    pass


def test_lease_loss_after_proposing_parks_without_a_session(
    kernel: Kernel, tools: Tools, session: FakeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_run_runtime, "attempt_lease_seconds", lambda request: 3.0)
    monkeypatch.setattr(agent_run_runtime, "LEASE_MARGIN", 1.0)
    setup(kernel)

    def dies(stage: str) -> None:
        if stage == "agent_run.before_finish":
            raise Lost

    session.scripts = [proposes]
    task_id = submit(kernel, max_attempts=1)
    kernel.fault = dies
    with pytest.raises(Lost):
        AgentRunRuntime(kernel).execute(task_id)
    kernel.fault = lambda stage: None
    assert task_of(kernel, task_id).state is TaskState.RUNNING
    # Recovery fences the lost attempt; the request survives and the run parks on it
    # without a new session and without using an attempt (max_attempts=1).
    assert AgentRunRuntime(kernel).execute(task_id) == "waiting"
    assert len(session.requests) == 1
    task = task_of(kernel, task_id)
    assert task.state is TaskState.WAITING
    assert task.checkpoint.state["attempt"] == 1
    assert held_row(kernel, task_id)["state"] == "pending"


def test_cancel_withdraws_a_parked_run(
    kernel: Kernel, tools: Tools, session: FakeSession
) -> None:
    setup(kernel)
    session.scripts = [proposes]
    task_id = submit(kernel)
    assert AgentRunRuntime(kernel).execute(task_id) == "waiting"
    kernel.control(kernel.auth.validate("supervisor"), task_id, Signal(request_id="stop"), "cancel")
    assert task_of(kernel, task_id).state is TaskState.CANCELLED
    row = held_row(kernel, task_id)
    assert row["state"] == "withdrawn"
    with pytest.raises(Fault) as closed:
        decide(kernel, row)
    assert closed.value.code == "approval_closed"


# 13. Agent approvers decide from inside their own task


def reviewer_request(**changes: Any) -> AgentRunRequest:
    return AgentRunRequest.model_validate(
        {
            "request_id": "review-1",
            "agent_id": "reviewer",
            "namespace": "test",
            "objective": "Review pending mail",
            "instructions": "Decide the pending request.",
            "operations": ["approvals.decide"],
            "grants": [*RIGHTS, "models.invoke", "approve:mail.send"],
            "attempt_timeout_seconds": 30,
            **changes,
        }
    )


def reviewing(row: dict[str, Any], outcomes: list[types.CallToolResult]) -> Any:
    async def review(client: ClientSession, request: SessionRequest) -> SessionResult:
        listed = await client.list_tools()
        assert [t.name for t in listed.tools] == ["approvals-decide"]
        outcomes.append(
            await call(
                client,
                "approvals-decide",
                request_id=row["request_id"],
                decision="approve",
                intent_digest=row["intent_digest"],
                reason="Checked the recap",
            )
        )
        return SessionResult(text="Reviewed.", is_error=False)

    return review


def test_reviewer_agent_approves_within_its_ceiling(
    kernel: Kernel, tools: Tools, session: FakeSession
) -> None:
    recorder = setup(kernel)
    session.scripts = [proposes]
    task_id = submit(kernel)
    assert AgentRunRuntime(kernel).execute(task_id) == "waiting"
    row = held_row(kernel, task_id)
    outcomes: list[types.CallToolResult] = []
    session.scripts = [reviewing(row, outcomes)]
    supervisor = kernel.auth.validate("supervisor")
    review = kernel.submit(supervisor, reviewer_request()).task.task_id
    assert AgentRunRuntime(kernel).execute(review) == "succeeded"
    (outcome,) = outcomes
    assert not outcome.is_error, outcome.content
    assert outcome.structured_content and outcome.structured_content["state"] == "approved"
    assert held_row(kernel, task_id)["state"] == "approved"
    with kernel.database.read() as conn:
        decision = approval_requests.decision_for(conn, row["request_id"])
    assert decision is not None and decision["approver_id"] == "reviewer"
    assert task_of(kernel, task_id).state is TaskState.QUEUED
    assert len(recorder.continuations()) == 1
    (receipt,) = calls(kernel, review)
    assert receipt.request.name == "approvals.decide" and receipt.state is OperationState.SUCCEEDED


@pytest.mark.parametrize("case", ["no_grant", "supervisor_lost_approve", "requester"])
def test_agent_approver_refusals(
    kernel: Kernel, tools: Tools, session: FakeSession, case: str
) -> None:
    setup(kernel)
    session.scripts = [proposes]
    task_id = submit(kernel)
    assert AgentRunRuntime(kernel).execute(task_id) == "waiting"
    row = held_row(kernel, task_id)
    outcomes: list[types.CallToolResult] = []
    session.scripts = [reviewing(row, outcomes)]
    changes: dict[str, Any] = {}
    if case == "no_grant":
        changes = {"grants": [*RIGHTS, "models.invoke"]}
    if case == "requester":
        changes = {"agent_id": "agent"}
    review = kernel.submit(kernel.auth.validate("supervisor"), reviewer_request(**changes))
    if case == "supervisor_lost_approve":
        setup(kernel, supervisor_approves=False)
    assert AgentRunRuntime(kernel).execute(review.task.task_id) == "succeeded"
    (outcome,) = outcomes
    assert outcome.is_error
    expected = "self_approval" if case == "requester" else "approver_denied"
    assert expected in text(outcome), outcome.content
    assert held_row(kernel, task_id)["state"] == "pending"
    with kernel.database.read() as conn:
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM approval_decisions").scalar_one() == 0


def test_event_payloads_carry_no_arguments(
    kernel: Kernel, tools: Tools, session: FakeSession
) -> None:
    setup(kernel)
    session.scripts = [proposes]
    task_id = submit(kernel)
    AgentRunRuntime(kernel).execute(task_id)
    decide(kernel, held_row(kernel, task_id), "reject")
    with kernel.database.read() as conn:
        rows = conn.exec_driver_sql(
            "SELECT event_type,payload FROM events WHERE event_type LIKE 'approval.%'"
        ).all()
    assert {r[0] for r in rows} >= {"approval.requested", "approval.decided"}
    for _, payload in rows:
        assert "arguments" not in json.loads(payload) and "user@example" not in payload
