"""Approval expiry, withdrawal on cancel and crash points (ADR-036 slice 3); portable fakes.

ADR acceptance tests 4 (expire), 5 (the expiry half: missed during downtime), 6 (crash
around the decision) and 7 (crash during the approved effect), plus withdrawal.
"""

import json
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
import test_approval_waits as waits
from fastapi.testclient import TestClient
from test_approval_waits import (
    RULE,
    Auth,
    Handler,
    decide,
    descriptor,
    events,
    failure,
    headers,
    request_row,
    state,
    submit,
)

from cardea.kernel.models import OperationState, TaskState
from cardea.service import approval_requests
from cardea.service.app import create_app
from cardea.service.auth import Authentication
from cardea.service.database import Database, borrow
from cardea.service.kernel import Kernel
from cardea.service.models import now
from cardea.service.operation_runtime import OperationRuntime
from cardea.service.operations import OperationDescriptor, Registry

kernel, handler = waits.kernel, waits.handler
EXPIRING = {**RULE, "expires_after_seconds": 60}


class Crash(Exception):
    pass


def expiring(kernel: Kernel, handler: Handler, **rule: Any) -> None:
    kernel.operations = Registry(
        (OperationDescriptor.model_validate(descriptor(rules=({**EXPIRING, **rule},))),),
        {"test.pay": handler},
    )


def waiting(kernel: Kernel, amount: int = 900) -> tuple[str, dict[str, Any]]:
    task_id = submit(kernel, amount)
    assert OperationRuntime(kernel).execute(task_id) == "waiting"
    return task_id, request_row(kernel, task_id)


def after_expiry(row: dict[str, Any], seconds: float = 1) -> datetime:
    return datetime.fromisoformat(row["expires_at"]) + timedelta(seconds=seconds)


def no_arguments(kernel: Kernel) -> None:
    for event in events(kernel, "approval."):
        assert "arguments" not in event["payload"] and "supplier-1" not in json.dumps(event)


def receipts(kernel: Kernel, task_id: str) -> list[Any]:
    with kernel.database.read() as conn, borrow(conn) as tx:
        return list(tx.list_operations(task_id))


# 4. Expire while waiting


def test_expiry_fails_the_task_and_a_later_decision_is_refused(
    kernel: Kernel, handler: Handler
) -> None:
    expiring(kernel, handler)
    task_id, row = waiting(kernel)
    created = datetime.fromisoformat(row["created_at"])
    assert datetime.fromisoformat(row["expires_at"]) == created + timedelta(seconds=60)
    assert approval_requests.expire_due(kernel) == {}  # not due yet
    assert approval_requests.expire_due(kernel, after_expiry(row)) == {
        row["request_id"]: "expired"
    }
    assert request_row(kernel, task_id)["state"] == "expired"
    assert state(kernel, task_id) is TaskState.FAILED
    assert failure(kernel, task_id).code == "approval_expired"
    # Idempotent: a second tick finds nothing.
    assert approval_requests.expire_due(kernel, after_expiry(row, 120)) == {}
    assert [e["type"] for e in events(kernel, "approval.")] == [
        "approval.requested",
        "approval.expired",
        f"approval.{row['request_id']}",
    ]
    no_arguments(kernel)
    response = decide(TestClient(create_app(kernel)), "approver", row)
    assert response.status_code == 409 and response.json()["code"] == "approval_expired"
    # A recovered operation workflow neither proposes again nor dispatches.
    assert OperationRuntime(kernel).execute(task_id) == "failed"
    assert handler.calls == [] and receipts(kernel, task_id) == []
    assert waits.continuations() == []


def test_operation_task_fails_even_when_the_rule_says_resume(
    kernel: Kernel, handler: Handler
) -> None:
    expiring(kernel, handler, on_expiry="resume")
    task_id, row = waiting(kernel)
    assert row["on_expiry"] == "resume"
    approval_requests.expire_due(kernel, after_expiry(row))
    assert failure(kernel, task_id).code == "approval_expired"


def test_only_agent_runs_resume_on_expiry() -> None:
    agent = cast(Any, SimpleNamespace(runtime_id="agent-run-v1"))
    operation = cast(Any, SimpleNamespace(runtime_id="operation-v1"))
    resume, fail = {"on_expiry": "resume"}, {"on_expiry": "fail"}
    assert approval_requests.resumes_on_expiry(agent, cast(Any, resume))
    assert not approval_requests.resumes_on_expiry(agent, cast(Any, fail))
    assert not approval_requests.resumes_on_expiry(operation, cast(Any, resume))


def test_expiry_voids_a_request_that_could_no_longer_run(kernel: Kernel, handler: Handler) -> None:
    expiring(kernel, handler)
    task_id, row = waiting(kernel)
    kernel.operations = Registry(
        (OperationDescriptor.model_validate(descriptor(version=2, rules=(EXPIRING,))),),
        {"test.pay": handler},
    )
    assert approval_requests.expire_due(kernel, after_expiry(row)) == {row["request_id"]: "void"}
    assert request_row(kernel, task_id)["void_reason"] == "operation_changed"
    assert failure(kernel, task_id).code == "approval_void"


def test_pending_request_of_an_ended_task(kernel: Kernel, handler: Handler) -> None:
    """A task that ended another way: decisions get task_terminal; expiry leaves the task."""
    expiring(kernel, handler)
    task_id, row = waiting(kernel)
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        task = tx.get_task(task_id)
        tx.change_task(task_id, TaskState.CANCELLED, expected_revision=task.revision, at=now())
    response = decide(TestClient(create_app(kernel)), "approver", row)
    assert response.status_code == 409 and response.json()["code"] == "task_terminal"
    assert approval_requests.expire_due(kernel, after_expiry(row)) == {
        row["request_id"]: "expired"
    }
    assert state(kernel, task_id) is TaskState.CANCELLED


# 5. Expired during downtime: the first tick after restart applies it


def test_expiry_missed_during_downtime_applies_on_the_first_tick(
    kernel: Kernel, handler: Handler
) -> None:
    expiring(kernel, handler)
    task_id, row = waiting(kernel)
    kernel.close()
    kernel.database.close()
    database = Database(kernel.database.directory)
    restarted = Kernel(database, cast(Authentication, Auth()), operations=kernel.operations)
    try:
        assert state(restarted, task_id) is TaskState.WAITING
        # The daemon was down for an hour past expires_at; one tick catches up.
        assert approval_requests.expire_due(restarted, after_expiry(row, 3600)) == {
            row["request_id"]: "expired"
        }
        assert failure(restarted, task_id).code == "approval_expired"
        assert handler.calls == []
    finally:
        database.close()


# A decision racing expiry: exactly one wins


@pytest.mark.parametrize("amount", [600, 700, 800, 900, 1000])
def test_decision_racing_expiry_has_one_winner(
    kernel: Kernel, handler: Handler, amount: int
) -> None:
    expiring(kernel, handler)
    task_id, row = waiting(kernel, amount)
    client = TestClient(create_app(kernel))
    start = threading.Barrier(2)
    results: dict[str, Any] = {}

    def approve() -> None:
        start.wait()
        results["decision"] = decide(client, "approver", row)

    def tick() -> None:
        start.wait()
        results["expiry"] = approval_requests.expire_due(kernel, after_expiry(row))

    threads = [threading.Thread(target=approve), threading.Thread(target=tick)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    final = request_row(kernel, task_id)["state"]
    with kernel.database.read() as conn:
        decisions = conn.exec_driver_sql("SELECT COUNT(*) FROM approval_decisions").scalar_one()
    if results["decision"].status_code == 200:
        assert final == "approved" and decisions == 1
        assert results["expiry"] in ({}, {row["request_id"]: "approved"})
        assert len(waits.continuations()) == 1
        assert state(kernel, task_id) is TaskState.QUEUED
    else:
        assert results["decision"].json()["code"] == "approval_expired"
        assert final == "expired" and decisions == 0
        assert waits.continuations() == []
        assert failure(kernel, task_id).code == "approval_expired"


# Withdrawal on cancel


def cancel(client: TestClient, task_id: str, request_id: str = "stop-1") -> Any:
    return client.post(
        f"/v0.2/tasks/{task_id}/cancel",
        json={"request_id": request_id},
        headers=headers("supervisor"),
    )


def test_cancel_withdraws_the_pending_request(kernel: Kernel, handler: Handler) -> None:
    expiring(kernel, handler)
    task_id, row = waiting(kernel)
    client = TestClient(create_app(kernel))
    assert cancel(client, task_id).status_code == 200
    assert cancel(client, task_id).status_code == 200  # replayed signal
    assert state(kernel, task_id) is TaskState.CANCELLED
    assert request_row(kernel, task_id)["state"] == "withdrawn"
    assert [e["type"] for e in events(kernel, "approval.")] == [
        "approval.requested",
        "approval.withdrawn",
        f"approval.{row['request_id']}",
    ]
    no_arguments(kernel)
    closed = decide(client, "approver", row)
    assert closed.status_code == 409
    assert closed.json()["code"] == "approval_closed" and closed.json()["state"] == "withdrawn"
    assert approval_requests.expire_due(kernel, after_expiry(row)) == {}
    assert OperationRuntime(kernel).execute(task_id, approval=row["request_id"]) == "cancelled"
    assert handler.calls == [] and waits.continuations() == []


def test_cancel_after_approval_before_execution_voids(kernel: Kernel, handler: Handler) -> None:
    task_id, row = waiting(kernel)
    client = TestClient(create_app(kernel))
    assert decide(client, "approver", row).status_code == 200
    assert cancel(client, task_id).status_code == 200
    stored = request_row(kernel, task_id)
    assert (stored["state"], stored["void_reason"]) == ("void", "task_cancelled")
    assert OperationRuntime(kernel).execute(task_id, approval=row["request_id"]) == "cancelled"
    assert handler.calls == []


# 6. Crash around the decision: no decision, or one with exactly one continuation


class TransactionalClient(waits.FakeClient):
    """Enqueues in the caller's transaction, deduplicated by workflow ID, as DBOS does."""

    def enqueue_in_transaction(self, conn: Any, options: dict[str, Any], *args: Any) -> None:
        conn.exec_driver_sql(
            "INSERT OR IGNORE INTO test_enqueued VALUES (?)", (options["workflow_id"],)
        )


def enqueued(kernel: Kernel) -> list[str]:
    with kernel.database.read() as conn:
        rows = conn.exec_driver_sql(
            "SELECT workflow_id FROM test_enqueued WHERE workflow_id LIKE '%.approval.%'"
        )
        return [str(r[0]) for r in rows]


@pytest.mark.parametrize("point", ["before_commit", "after_commit"])
def test_crash_around_the_decision(kernel: Kernel, point: str) -> None:
    with kernel.database.engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE test_enqueued (workflow_id TEXT PRIMARY KEY)")
    kernel.client = cast(Any, TransactionalClient())
    task_id, row = waiting(kernel)
    body = approval_requests.DecisionRequest(
        decision_id="d-1", decision="approve", intent_digest=row["intent_digest"]
    )
    proof = kernel.auth.validate("approver")

    def crash(stage: str) -> None:
        if stage == f"approval.decision_{point}":
            raise Crash

    kernel.fault = crash
    with pytest.raises(Crash):
        approval_requests.decide(kernel, proof, row["request_id"], body)
    kernel.fault = lambda stage: None
    expected = [approval_requests.continuation_id(task_id, row["request_id"])]
    if point == "before_commit":
        assert request_row(kernel, task_id)["state"] == "pending"
        assert state(kernel, task_id) is TaskState.WAITING and enqueued(kernel) == []
    else:
        assert request_row(kernel, task_id)["state"] == "approved"
        assert state(kernel, task_id) is TaskState.QUEUED and enqueued(kernel) == expected
    # The client retries with the same decision_id: one decision, one continuation.
    assert approval_requests.decide(kernel, proof, row["request_id"], body)["state"] == "approved"
    assert enqueued(kernel) == expected


@pytest.mark.parametrize("point", ["before_commit", "after_commit"])
def test_crash_around_expiry(kernel: Kernel, handler: Handler, point: str) -> None:
    expiring(kernel, handler)
    task_id, row = waiting(kernel)

    def crash(stage: str) -> None:
        if stage == f"approval.expiry_{point}":
            raise Crash

    kernel.fault = crash
    # The tick logs the failure and carries on; the next tick retries.
    outcome = approval_requests.expire_due(kernel, after_expiry(row))
    assert outcome == {row["request_id"]: "error"}
    kernel.fault = lambda stage: None
    pending = point == "before_commit"
    assert request_row(kernel, task_id)["state"] == ("pending" if pending else "expired")
    retried = approval_requests.expire_due(kernel, after_expiry(row))
    assert retried == ({row["request_id"]: "expired"} if pending else {})
    assert failure(kernel, task_id).code == "approval_expired"
    assert len(events(kernel, "approval.expired")) == 1


# 7. Crash during the approved effect: outcome unknown, never replayed


@pytest.mark.parametrize(
    ("point", "calls"), [("operation.intent_committed", 0), ("operation.before_settle", 1)]
)
def test_crash_during_the_approved_effect(
    kernel: Kernel, handler: Handler, point: str, calls: int
) -> None:
    task_id, row = waiting(kernel)
    assert decide(TestClient(create_app(kernel)), "approver", row).status_code == 200

    def crash(stage: str) -> None:
        if stage == point:
            raise Crash

    kernel.fault = crash
    with pytest.raises(Crash):
        OperationRuntime(kernel).execute(task_id, approval=row["request_id"])
    kernel.fault = lambda stage: None
    assert state(kernel, task_id) is TaskState.RUNNING
    # DBOS recovers the continuation workflow: it fences, it does not dispatch again.
    assert OperationRuntime(kernel).execute(task_id, approval=row["request_id"]) == "failed"
    assert failure(kernel, task_id).code == "reconciliation_required"
    assert len(handler.calls) == calls
    (receipt,) = receipts(kernel, task_id)
    assert receipt.request.operation_id == "apr-01"
    assert receipt.state is OperationState.OUTCOME_UNKNOWN
    assert request_row(kernel, task_id)["state"] == "executed"
