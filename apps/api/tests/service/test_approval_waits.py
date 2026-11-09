"""Approval waits for single-operation tasks (ADR-036 slice 2); portable fakes only.

DBOS is replaced by a client that records enqueued continuations; tests run the operation
step and the continuation step directly, as the workflows would.
"""

import json
import threading
import time
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from pydantic import JsonValue, ValidationError

from cardea.kernel.models import OperationState, TaskState
from cardea.service import approval_requests, operation_runtime
from cardea.service import kernel as handlers
from cardea.service.app import create_app
from cardea.service.auth import Authentication, Proof
from cardea.service.database import Database, Fault, borrow
from cardea.service.kernel import Kernel, provision
from cardea.service.models import (
    RIGHTS,
    OperationTaskRequest,
    Permission,
    Provisioning,
    now,
)
from cardea.service.operation_runtime import OperationRuntime
from cardea.service.operations import OperationContext, OperationDescriptor, Registry
from cardea.service.policy import Policy

TOKENS = {"runtime", "supervisor", "approver", "controller", "outsider"}
ENQUEUED: list[tuple[dict[str, Any], tuple[Any, ...]]] = []


class FakeClient:
    def __init__(self, **kwargs: Any) -> None:
        pass

    def enqueue_in_transaction(self, conn: Any, options: dict[str, Any], *args: Any) -> None:
        ENQUEUED.append((options, args))

    def send_in_transaction(self, *args: Any, **kwargs: Any) -> None:
        pass

    def destroy(self) -> None:
        pass


class Auth:
    def runtime_proof(self) -> Proof:
        return Proof("spiffe", "test", "runtime", time.time() + 60)

    def validate(self, token: str, kind: str = "spiffe") -> Proof:
        if token not in TOKENS:
            raise Fault("invalid_credential", "Unknown test credential", 401)
        return Proof("spiffe", "test", token, time.time() + 60)


RULE = {
    "rule_id": "large-payments",
    "level": "conditional",
    "when": "args.amount > 500",
    "reason": "Payments above 500 need finance approval",
}


def descriptor(version: int = 1, rules: tuple[dict[str, Any], ...] = (RULE,)) -> dict[str, Any]:
    return {
        "tool": {
            "name": "pay.send",
            "title": "Send one payment",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "to": {"type": "string"},
                    "amount": {"type": "integer"},
                    "memo": {"type": "string"},
                },
                "required": ["to", "amount"],
                "additionalProperties": False,
            },
            "outputSchema": {
                "type": "object",
                "properties": {"payment_id": {"type": "string"}},
                "required": ["payment_id"],
            },
        },
        "cardea": {
            "version": version,
            "effect": "effect",
            "handler": "test.pay",
            "timeout_seconds": 1,
            "approval": {"rules": list(rules)},
        },
    }


class Handler:
    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, JsonValue], OperationContext]] = []

    async def __call__(
        self, arguments: dict[str, JsonValue], context: OperationContext
    ) -> dict[str, JsonValue]:
        self.calls.append((arguments, context))
        return {"payment_id": f"pay-{context.receipt_id}"}


GRANTS = (*RIGHTS, "pay.send")
HOUR = timedelta(hours=1)


def document(
    *,
    supervisor: tuple[str, ...] = GRANTS,
    approver: bool = True,
    approvals: dict[str, Any] | None = None,
) -> Provisioning:
    principals = [
        {
            "principal_id": p,
            "kind": "service" if p == "runtime" else "human",
            "auth_kind": "spiffe",
            "issuer": "test",
            "subject": p,
        }
        for p in ("runtime", "supervisor", "approver", "controller", "outsider")
    ] + [{"principal_id": "agent", "kind": "agent", "runtime_id": "runtime"}]
    expires = (now() + HOUR).isoformat()
    permissions = [
        {
            "principal_id": "supervisor",
            "namespace": "test",
            "operation": right,
            "delegate": True,
            # The supervisor may approve its own task's requests by default (ADR-036).
            "approve": right == "pay.send",
            "expires_at": expires,
        }
        for right in supervisor
    ]
    if approver:
        for who in ("approver", "controller"):
            # Approve-only: may decide payments without being able to make them.
            permissions.append(
                {
                    "principal_id": who,
                    "namespace": "test",
                    "operation": "pay.send",
                    "allow": False,
                    "approve": True,
                    "expires_at": expires,
                }
            )
    # The kernel runtime and the agent hold approve too: the requester exclusion must stop them.
    for who in ("runtime", "agent"):
        permissions.append(
            {
                "principal_id": who,
                "namespace": "test",
                "operation": "pay.send",
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
            "approve": right == "pay.send",
            "expires_at": expires,
        }
        for right in GRANTS
    ]
    return Provisioning.model_validate(
        {
            "principals": principals,
            "permissions": permissions,
            "platform": platform,
            **({"approvals": approvals} if approvals else {}),
        }
    )


@pytest.fixture
def handler() -> Handler:
    return Handler()


@pytest.fixture
def kernel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, handler: Handler) -> Iterator[Kernel]:
    ENQUEUED.clear()
    monkeypatch.setattr(handlers, "DBOSClient", FakeClient)
    monkeypatch.setattr(operation_runtime, "LEASE_MARGIN", 0.3)
    monkeypatch.setattr(operation_runtime, "SETTLE_MARGIN", 0.1)
    registry = Registry((OperationDescriptor.model_validate(descriptor()),), {"test.pay": handler})
    db = Database(tmp_path / "data")
    db.migrate()
    provision(db, document(), registry)
    instance = Kernel(db, cast(Authentication, Auth()), operations=registry)
    try:
        yield instance
    finally:
        db.close()


def submit(kernel: Kernel, amount: int = 900, **changes: Any) -> str:
    request = OperationTaskRequest.model_validate(
        {
            "request_id": f"pay-{amount}",
            "agent_id": "agent",
            "namespace": "test",
            "objective": "Pay the supplier invoice",
            "operation": "pay.send",
            "arguments": {"to": "supplier-1", "amount": amount},
            "grants": list(GRANTS),
            **changes,
        }
    )
    return kernel.submit(kernel.auth.validate("supervisor"), request).task.task_id


def state(kernel: Kernel, task_id: str) -> TaskState:
    with kernel.database.read() as conn, borrow(conn) as tx:
        return tx.get_task(task_id).state


def failure(kernel: Kernel, task_id: str) -> Any:
    with kernel.database.read() as conn, borrow(conn) as tx:
        return tx.get_task(task_id).failure


def request_row(kernel: Kernel, task_id: str) -> dict[str, Any]:
    with kernel.database.read() as conn:
        row = conn.exec_driver_sql(
            "SELECT * FROM approval_requests WHERE task_id=?", (task_id,)
        ).mappings()
        rows = [dict(r) for r in row]
    assert len(rows) == 1
    return rows[0]


def headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Cardea-Auth": "spiffe"}


def decide(
    client: TestClient, token: str, row: dict[str, Any], decision: str = "approve", **body: Any
) -> Any:
    return client.post(
        f"/v0.2/approvals/{row['request_id']}/decision",
        json={
            "decision_id": f"{token}-{decision}",
            "decision": decision,
            "intent_digest": row["intent_digest"],
            **body,
        },
        headers=headers(token),
    )


def waiting(kernel: Kernel, amount: int = 900, **changes: Any) -> tuple[str, dict[str, Any]]:
    task_id = submit(kernel, amount, **changes)
    assert OperationRuntime(kernel).execute(task_id) == "waiting"
    return task_id, request_row(kernel, task_id)


def events(kernel: Kernel, prefix: str) -> list[dict[str, Any]]:
    with kernel.database.read() as conn:
        rows = conn.exec_driver_sql(
            "SELECT event_type,payload FROM events WHERE event_type LIKE ? ORDER BY cursor",
            (prefix + "%",),
        ).all()
    return [{"type": r[0], "payload": json.loads(r[1])} for r in rows]


def continuations() -> list[tuple[dict[str, Any], tuple[Any, ...]]]:
    return [e for e in ENQUEUED if e[0]["workflow_name"] == "cardea.m6.approval"]


# 2. Below threshold: authority alone


def test_below_threshold_runs_at_once_with_no_request(kernel: Kernel, handler: Handler) -> None:
    task_id = submit(kernel, amount=100)
    assert OperationRuntime(kernel).execute(task_id) == "succeeded"
    assert len(handler.calls) == 1
    with kernel.database.read() as conn:
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM approval_requests").scalar_one() == 0


# 1. Approve, 15. notifications, receipt contents


def test_approve_runs_the_frozen_intent_once_as_apr_01(kernel: Kernel, handler: Handler) -> None:
    task_id, row = waiting(kernel)
    assert handler.calls == [] and state(kernel, task_id) is TaskState.WAITING
    assert row["state"] == "pending" and row["operation_id"] == "apr-01"
    assert row["on_expiry"] == "fail"  # single-operation default (ADR-036 decision 2)
    client = TestClient(create_app(kernel))
    view = client.get(f"/v0.2/tasks/{task_id}", headers=headers("supervisor")).json()
    assert view["waiting_for"] == {"approval": row["request_id"]}
    # The whole intent is readable by an approver.
    intent = client.get(f"/v0.2/approvals/{row['request_id']}", headers=headers("approver"))
    assert intent.status_code == 200, intent.text
    body = intent.json()
    assert body["arguments"] == {"to": "supplier-1", "amount": 900}
    assert [r["rule_id"] for r in body["rules"]] == ["large-payments"]
    listed = client.get(
        "/v0.2/approvals", params={"namespace": "test"}, headers=headers("approver")
    ).json()
    assert [a["request_id"] for a in listed["approvals"]] == [row["request_id"]]
    assert "arguments" not in listed["approvals"][0]

    decided = decide(client, "approver", row, reason="Invoice checked")
    assert decided.status_code == 200, decided.text
    assert decided.json()["state"] == "approved"
    assert state(kernel, task_id) is TaskState.QUEUED
    ((options, args),) = continuations()
    assert options["workflow_id"] == f"cardea:{task_id}.approval.{row['request_id']}"
    assert options["workflow_name"] == "cardea.m6.approval"
    assert args == (task_id, row["request_id"])

    assert OperationRuntime(kernel).execute(task_id, approval=row["request_id"]) == "succeeded"
    assert len(handler.calls) == 1
    assert handler.calls[0][0] == {"to": "supplier-1", "amount": 900}
    # A recovered or repeated continuation never dispatches again.
    assert OperationRuntime(kernel).execute(task_id, approval=row["request_id"]) == "succeeded"
    assert OperationRuntime(kernel).execute(task_id) == "succeeded"
    assert len(handler.calls) == 1
    executed = request_row(kernel, task_id)
    assert executed["state"] == "executed"
    with kernel.database.read() as conn, borrow(conn) as tx:
        (receipt,) = tx.list_operations(task_id)
    assert receipt.request.operation_id == "apr-01"
    assert receipt.state is OperationState.SUCCEEDED
    assert receipt.receipt_id == executed["receipt_id"]
    block = cast(Any, receipt.request.arguments)["context"]["approval"]
    assert block == {
        "request_id": row["request_id"],
        "decision_id": "approver-approve",
        "approver_id": "approver",
        "decision": "approve",
        "decided_at": block["decided_at"],
        "argument_sha256": row["argument_sha256"],
        "intent_digest": row["intent_digest"],
        "rule_ids": ["large-payments"],
        "rules_digest": row["rules_digest"],
    }
    task_receipt = client.get(f"/v0.2/tasks/{task_id}/receipt", headers=headers("supervisor"))
    assert task_receipt.json()["receipt"]["request"]["operation_id"] == "apr-01"
    # Journal: requested, decided and the waking event; none carries arguments.
    journal = events(kernel, "approval.")
    assert [e["type"] for e in journal] == [
        "approval.requested",
        "approval.decided",
        f"approval.{row['request_id']}",
    ]
    for event in journal:
        assert "arguments" not in event["payload"] and "supplier-1" not in json.dumps(event)
        assert event["payload"]["intent_digest"] == row["intent_digest"]


# 3. Reject


def test_reject_fails_the_task_with_no_receipt_or_call(kernel: Kernel, handler: Handler) -> None:
    task_id, row = waiting(kernel)
    client = TestClient(create_app(kernel))
    response = decide(client, "approver", row, "reject", reason="Not budgeted")
    assert response.status_code == 200 and response.json()["state"] == "rejected"
    assert OperationRuntime(kernel).execute(task_id, approval=row["request_id"]) == "failed"
    assert failure(kernel, task_id).code == "approval_rejected"
    assert handler.calls == []
    with kernel.database.read() as conn, borrow(conn) as tx:
        assert tx.list_operations(task_id) == []
    assert decide(client, "controller", row).json()["code"] == "approval_closed"


# 5. Crash while waiting: nothing waits in memory


def test_restart_while_waiting_keeps_the_wait(kernel: Kernel, handler: Handler) -> None:
    task_id, row = waiting(kernel)
    kernel.close()
    kernel.database.close()
    database = Database(kernel.database.directory)
    restarted = Kernel(database, cast(Authentication, Auth()), operations=kernel.operations)
    try:
        assert state(restarted, task_id) is TaskState.WAITING
        assert request_row(restarted, task_id)["state"] == "pending"
        # A recovered operation workflow does not propose again or dispatch.
        assert OperationRuntime(restarted).execute(task_id) == "waiting"
        client = TestClient(create_app(restarted))
        assert decide(client, "approver", row).status_code == 200
        assert OperationRuntime(restarted).execute(task_id, approval=row["request_id"]) == (
            "succeeded"
        )
        assert len(handler.calls) == 1
    finally:
        database.close()


# 9. Approver revoked before execution


def test_approver_revoked_before_execution_voids(kernel: Kernel, handler: Handler) -> None:
    task_id, row = waiting(kernel)
    client = TestClient(create_app(kernel))
    assert decide(client, "approver", row).status_code == 200
    provision(kernel.database, document(approver=False), kernel.operations)
    assert OperationRuntime(kernel).execute(task_id, approval=row["request_id"]) == "failed"
    assert failure(kernel, task_id).code == "approval_void"
    voided = request_row(kernel, task_id)
    assert (voided["state"], voided["void_reason"]) == ("void", "approver_authority_lost")
    assert handler.calls == []
    assert [e["type"] for e in events(kernel, "approval.void")] == ["approval.void"]


# 10. Supervisor revoked while waiting


def test_supervisor_revoked_while_waiting_voids_at_decision(
    kernel: Kernel, handler: Handler
) -> None:
    task_id, row = waiting(kernel)
    provision(
        kernel.database,
        document(supervisor=tuple(r for r in GRANTS if r != "pay.send")),
        kernel.operations,
    )
    client = TestClient(create_app(kernel))
    response = decide(client, "approver", row)
    assert response.status_code == 409
    assert response.json()["code"] == "approval_void"
    assert response.json()["reason"] == "authority_lost"
    assert request_row(kernel, task_id)["state"] == "void"
    assert OperationRuntime(kernel).execute(task_id, approval=row["request_id"]) == "failed"
    assert failure(kernel, task_id).code == "approval_void"
    assert handler.calls == []


# 11. Arguments or descriptor changed


def test_operation_reinstalled_at_a_new_version_voids(kernel: Kernel, handler: Handler) -> None:
    task_id, row = waiting(kernel)
    client = TestClient(create_app(kernel))
    assert decide(client, "approver", row).status_code == 200
    kernel.operations = Registry(
        (OperationDescriptor.model_validate(descriptor(version=2)),), {"test.pay": handler}
    )
    assert OperationRuntime(kernel).execute(task_id, approval=row["request_id"]) == "failed"
    assert request_row(kernel, task_id)["void_reason"] == "operation_changed"
    assert handler.calls == []


def test_stale_or_forged_intent_digest_is_refused_without_writes(kernel: Kernel) -> None:
    task_id, row = waiting(kernel)
    client = TestClient(create_app(kernel))
    response = decide(client, "approver", row, intent_digest="0" * 64)
    assert response.status_code == 409 and response.json()["code"] == "intent_mismatch"
    assert request_row(kernel, task_id)["state"] == "pending"
    with kernel.database.read() as conn:
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM approval_decisions").scalar_one() == 0
    assert continuations() == []


# 13. Who may approve


def test_requester_runtime_and_outsider_cannot_approve(kernel: Kernel) -> None:
    _, row = waiting(kernel)
    client = TestClient(create_app(kernel))
    refused = decide(client, "runtime", row)
    assert refused.status_code == 403 and refused.json()["code"] == "self_approval"
    # The logical agent holds `approve` here but is the requester; it has no credential of
    # its own in this profile, so its exclusion is checked on the approver rule directly.
    with kernel.database.read() as conn:
        stored = approval_requests.get(conn, row["request_id"])
        with pytest.raises(Fault) as agent:
            approval_requests.check_approver(Policy(conn), "agent", stored)
    assert agent.value.code == "self_approval"
    outsider = decide(client, "outsider", row)
    assert outsider.status_code == 403 and outsider.json()["code"] == "approver_denied"
    path = f"/v0.2/approvals/{row['request_id']}"
    assert client.get(path, headers=headers("outsider")).status_code == 404
    # The supervisor reads as supervisor and may approve by default.
    assert client.get(path, headers=headers("supervisor")).status_code == 200
    assert decide(client, "supervisor", row).status_code == 200


def test_approve_only_principal_cannot_invoke(kernel: Kernel) -> None:
    with kernel.database.read() as conn:
        policy = Policy(conn)
        policy.require("controller", "test", "pay.send", "approve")
        with pytest.raises(Fault) as denied:
            policy.require("controller", "test", "pay.send")
    assert denied.value.code == "authority_denied"


def test_four_eyes_host_rule_refuses_the_supervisor(kernel: Kernel, handler: Handler) -> None:
    approvals = {
        "entries": [
            {
                "namespace": "test",
                "operation": "pay.send",
                "rules": [
                    {
                        "rule_id": "four-eyes",
                        "level": "always",
                        "reason": "Every payment needs a second person",
                        "distinct_from_supervisor": True,
                    }
                ],
            }
        ]
    }
    provision(kernel.database, document(approvals=approvals), kernel.operations)
    # The host rule replaces the descriptor default, so even a small payment waits.
    task_id, row = waiting(kernel, amount=10)
    assert [r["rule_id"] for r in json.loads(row["rules_json"])] == ["four-eyes"]
    client = TestClient(create_app(kernel))
    refused = decide(client, "supervisor", row)
    assert refused.status_code == 403 and refused.json()["code"] == "four_eyes_required"
    assert decide(client, "approver", row).status_code == 200
    assert OperationRuntime(kernel).execute(task_id, approval=row["request_id"]) == "succeeded"
    assert len(handler.calls) == 1


# 12. Idempotent decisions and racing approvers


def test_decision_retry_is_idempotent_and_a_changed_body_conflicts(kernel: Kernel) -> None:
    _, row = waiting(kernel)
    client = TestClient(create_app(kernel))
    first = decide(client, "approver", row)
    again = decide(client, "approver", row)
    assert first.status_code == again.status_code == 200
    assert first.json() == again.json()
    conflict = client.post(
        f"/v0.2/approvals/{row['request_id']}/decision",
        json={
            "decision_id": "approver-approve",
            "decision": "reject",
            "intent_digest": row["intent_digest"],
        },
        headers=headers("approver"),
    )
    assert conflict.status_code == 409 and conflict.json()["code"] == "decision_conflict"
    closed = decide(client, "controller", row, "reject")
    assert closed.status_code == 409
    assert closed.json() | {"message": ""} == {
        "schema_version": "0.2",
        "code": "approval_closed",
        "message": "",
        "state": "approved",
    }
    assert len(continuations()) == 1


def test_racing_approvers_produce_one_decision(kernel: Kernel) -> None:
    _, row = waiting(kernel)
    client = TestClient(create_app(kernel))
    results: list[Any] = []
    start = threading.Barrier(2)

    def race(token: str) -> None:
        start.wait()
        results.append(decide(client, token, row))

    threads = [threading.Thread(target=race, args=(t,)) for t in ("approver", "controller")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(r.status_code for r in results) == [200, 409]
    assert [r.json()["code"] for r in results if r.status_code == 409] == ["approval_closed"]
    with kernel.database.read() as conn:
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM approval_decisions").scalar_one() == 1
    assert len(continuations()) == 1


# 14. Autonomy configuration


def test_condition_error_requires_approval(kernel: Kernel, handler: Handler) -> None:
    broken = {**RULE, "rule_id": "memo-check", "when": "args.memo == 'urgent'"}
    kernel.operations = Registry(
        (OperationDescriptor.model_validate(descriptor(rules=(broken,))),), {"test.pay": handler}
    )
    # `memo` is absent: the condition errors and fails closed.
    _, row = waiting(kernel, amount=10)
    (rule,) = json.loads(row["rules_json"])
    assert rule["rule_id"] == "memo-check" and rule["error"]


def test_ungranted_operation_is_denied_with_no_request(kernel: Kernel, handler: Handler) -> None:
    task_id = submit(kernel)
    provision(
        kernel.database,
        document(supervisor=tuple(r for r in GRANTS if r != "pay.send")),
        kernel.operations,
    )
    assert OperationRuntime(kernel).execute(task_id) == "failed"
    assert failure(kernel, task_id).code == "authority_denied"
    with kernel.database.read() as conn:
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM approval_requests").scalar_one() == 0


def test_provisioning_checks_approval_rules_and_modes(kernel: Kernel) -> None:
    unknown = {"entries": [{"namespace": "test", "operation": "pay.other", "rules": []}]}
    with pytest.raises(Fault) as refused:
        provision(kernel.database, document(approvals=unknown), kernel.operations)
    assert refused.value.code == "operation_not_installed"
    with pytest.raises(ValidationError):
        Permission(
            principal_id="x",
            namespace="test",
            operation="pay.send",
            allow=False,
            delegate=True,
            expires_at=now() + HOUR,
        )
    with kernel.database.read() as conn:
        digest = conn.exec_driver_sql(
            "SELECT value FROM service_meta WHERE name='approval_rules_digest'"
        ).scalar_one()
    assert len(digest) == 64


def test_descriptor_digest_is_unchanged_without_approval_rules() -> None:
    plain = descriptor()
    del plain["cardea"]["approval"]
    without = OperationDescriptor.model_validate(plain)
    explicit = OperationDescriptor.model_validate(
        {**plain, "cardea": {**plain["cardea"], "approval": None}}
    )
    assert explicit.digest == without.digest
    assert OperationDescriptor.model_validate(descriptor()).digest != without.digest
