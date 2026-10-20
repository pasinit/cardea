"""Installed operation registry and in-process execution (ADR-023); portable fakes only."""

import json
import os
import time
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from pydantic import JsonValue, ValidationError

from cardea.kernel.models import OperationState, ResourceRef, RetryClass, TaskState
from cardea.service import kernel as handlers
from cardea.service import operation_runtime
from cardea.service.app import create_app
from cardea.service.auth import Authentication, Proof
from cardea.service.database import Database, Fault, borrow
from cardea.service.kernel import Kernel, provision
from cardea.service.models import (
    RIGHTS,
    OperationTaskRequest,
    Permission,
    Principal,
    Provisioning,
    now,
)
from cardea.service.operation_runtime import OperationRuntime
from cardea.service.operations import (
    OperationContext,
    OperationDescriptor,
    OperationRejected,
    Reconciliation,
    Registry,
)


class FakeClient:
    def __init__(self, **kwargs: Any) -> None:
        pass

    def enqueue_in_transaction(self, *args: Any) -> None:
        pass

    def send_in_transaction(self, *args: Any, **kwargs: Any) -> None:
        pass

    def destroy(self) -> None:
        pass


class Auth:
    def runtime_proof(self) -> Proof:
        return Proof("spiffe", "test", "runtime", time.time() + 60)

    def validate(self, token: str, kind: str = "spiffe") -> Proof:
        if token not in {"supervisor", "outsider"}:
            raise Fault("invalid_credential", "Unknown test credential", 401)
        return Proof("spiffe", "test", token, time.time() + 60)


def descriptor(name: str = "mail.send", effect: str = "effect", **binding: Any) -> dict[str, Any]:
    return {
        "tool": {
            "name": name,
            "title": "Send one message",
            "inputSchema": {
                "type": "object",
                "properties": {"to": {"type": "string"}, "body": {"type": "string"}},
                "required": ["to", "body"],
                "additionalProperties": False,
            },
            "outputSchema": {
                "type": "object",
                "properties": {"message_id": {"type": "string"}},
                "required": ["message_id"],
            },
            "annotations": {"destructiveHint": False, "openWorldHint": True},
        },
        "cardea": {"version": 1, "effect": effect, "handler": "test.handler", **binding},
    }


class Handler:
    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, JsonValue], OperationContext]] = []
        self.behaviour = "ok"

    async def __call__(
        self, arguments: dict[str, JsonValue], context: OperationContext
    ) -> dict[str, JsonValue]:
        self.calls.append((arguments, context))
        if self.behaviour == "reject":
            raise OperationRejected("provider refused before sending")
        if self.behaviour == "crash":
            raise ConnectionError("provider connection reset after send")
        if self.behaviour == "bad_output":
            return {"unexpected": True}
        return {"message_id": f"<{context.receipt_id}@cardea>"}


GRANTS = (*RIGHTS, "mail.send")


def permissions(rights: tuple[str, ...] = GRANTS) -> tuple[Permission, ...]:
    return tuple(
        Permission(
            principal_id="supervisor",
            namespace="test",
            operation=right,
            delegate=True,
            expires_at=now() + timedelta(hours=1),
        )
        for right in rights
    )


def policy(rights: tuple[str, ...] = GRANTS) -> Provisioning:
    granted = permissions(rights)
    return Provisioning(
        principals=(),
        permissions=granted,
        platform=tuple(p.model_copy(update={"principal_id": "platform"}) for p in granted),
    )


@pytest.fixture
def handler() -> Handler:
    return Handler()


@pytest.fixture
def kernel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, handler: Handler) -> Iterator[Kernel]:
    monkeypatch.setattr(handlers, "DBOSClient", FakeClient)
    monkeypatch.setattr(operation_runtime, "LEASE_MARGIN", 0.3)
    monkeypatch.setattr(operation_runtime, "SETTLE_MARGIN", 0.1)
    registry = Registry(
        (OperationDescriptor.model_validate(descriptor(timeout_seconds=1)),),
        {"test.handler": handler},
    )
    db = Database(tmp_path / "data")
    db.migrate()
    principals = tuple(
        Principal(
            principal_id=p,
            kind="service" if p == "runtime" else "human",
            auth_kind="spiffe",
            issuer="test",
            subject=p,
        )
        for p in ("runtime", "supervisor", "outsider")
    ) + (Principal(principal_id="agent", kind="agent", runtime_id="runtime"),)
    provision(db, policy().model_copy(update={"principals": principals}), registry)
    instance = Kernel(db, cast(Authentication, Auth()), operations=registry)
    try:
        yield instance
    finally:
        db.close()


def request(**changes: Any) -> OperationTaskRequest:
    return OperationTaskRequest.model_validate(
        {
            "request_id": "send-one",
            "agent_id": "agent",
            "namespace": "test",
            "objective": "Send the daily recap",
            "operation": "mail.send",
            "arguments": {"to": "user@example.invalid", "body": "3 new listings"},
            "grants": list(GRANTS),
            **changes,
        }
    )


def submit(kernel: Kernel, **changes: Any) -> str:
    return kernel.submit(kernel.auth.validate("supervisor"), request(**changes)).task.task_id


def task_state(kernel: Kernel, task_id: str) -> TaskState:
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        return tx.get_task(task_id).state


# Registry and descriptor validation


@pytest.mark.parametrize(
    "change",
    [
        {"tool": {"name": "objects.read"}},
        {"tool": {"name": "host.mail"}},
        {"tool": {"name": "Mail.Send"}},
        {"tool": {"outputSchema": None}},
        {"tool": {"inputSchema": {"type": "array"}}},
        {"tool": {"inputSchema": {"type": "object", "properties": {"x": {"type": 5}}}}},
        {"cardea": {"effect": "maybe"}},
        {"cardea": {"timeout_seconds": 0}},
        {"cardea": {"requires": ["not.a.right"]}},
        {"extra": True},
    ],
)
def test_invalid_descriptors_are_refused(change: dict[str, Any]) -> None:
    document = descriptor()
    for key, value in change.items():
        if isinstance(value, dict):
            document[key] = {**document[key], **value}
        else:
            document[key] = value
    with pytest.raises(ValidationError):
        OperationDescriptor.model_validate(document)


def write_private(path: Path, document: dict[str, Any], mode: int = 0o600) -> None:
    path.write_text(json.dumps(document))
    os.chmod(path, mode)


def test_load_reads_private_descriptors_and_refuses_unsafe_or_duplicate(tmp_path: Path) -> None:
    write_private(tmp_path / "mail.json", descriptor())
    registry = Registry.load(tmp_path, resolve=False)
    assert "mail.send" in registry and registry.get("mail.send").cardea.effect == "effect"
    write_private(tmp_path / "copy.json", descriptor())
    with pytest.raises(Fault) as duplicate:
        Registry.load(tmp_path, resolve=False)
    assert duplicate.value.code == "operation_duplicate"
    (tmp_path / "copy.json").unlink()
    write_private(tmp_path / "loose.json", descriptor("mail.other"), mode=0o644)
    with pytest.raises(Fault) as unsafe:
        Registry.load(tmp_path, resolve=False)
    assert unsafe.value.code == "unsafe_file"


def test_descriptors_cannot_declare_outputs_a_receipt_cannot_record(tmp_path: Path) -> None:
    """A receipt records at most a 64 KiB document; larger declared outputs are refused."""
    assert OperationDescriptor.model_validate(descriptor(max_output_bytes=65536))
    write_private(tmp_path / "mail.json", descriptor(max_output_bytes=131072))
    with pytest.raises(Fault) as refused:
        Registry.load(tmp_path, resolve=False)
    assert refused.value.code == "operation_invalid"
    assert "max_output_bytes 131072 exceeds 65536" in refused.value.message
    assert "mail.json" in refused.value.message


def test_outputs_beyond_the_receipt_document_fail_cleanly() -> None:
    registry = Registry((OperationDescriptor.model_validate(descriptor()),))
    # The declared cap counts RFC 8785 bytes; the receipt counts its own ASCII-escaped
    # document, which is larger for non-ASCII text. Neither may exceed what is recorded.
    assert registry.check_output("mail.send", {"message_id": "a" * 65_000}) is not None
    assert registry.check_output("mail.send", {"message_id": "é" * 32_000}) is None


def test_load_requires_installed_entry_point_handlers(tmp_path: Path) -> None:
    write_private(tmp_path / "mail.json", descriptor())
    with pytest.raises(Fault) as missing:
        Registry.load(tmp_path)
    assert missing.value.code == "operation_handler_missing"


def test_digest_changes_with_descriptor() -> None:
    one = OperationDescriptor.model_validate(descriptor())
    two = OperationDescriptor.model_validate(descriptor(timeout_seconds=9))
    assert one.digest != two.digest


# Provisioning and admission


def test_provisioning_refuses_operations_that_are_not_installed(kernel: Kernel) -> None:
    with pytest.raises(Fault) as refused:
        provision(kernel.database, policy((*RIGHTS, "mail.unknown")), kernel.operations)
    assert refused.value.code == "operation_not_installed"
    with pytest.raises(Fault):
        provision(kernel.database, policy())


def test_admission_checks_installation_arguments_and_grants(kernel: Kernel) -> None:
    with pytest.raises(Fault) as unknown:
        submit(kernel, operation="mail.unknown", grants=[*RIGHTS, "mail.unknown"])
    assert unknown.value.code == "operation_not_installed"
    with pytest.raises(Fault) as arguments:
        submit(kernel, arguments={"to": "user@example.invalid"})
    assert arguments.value.code == "invalid_arguments"
    with pytest.raises(ValidationError):
        request(grants=list(RIGHTS))


def test_admission_requires_the_supervisor_ceiling(kernel: Kernel) -> None:
    provision(kernel.database, policy(RIGHTS), kernel.operations)
    with pytest.raises(Fault) as denied:
        submit(kernel)
    assert denied.value.code == "authority_denied"


def test_duplicate_request_returns_the_same_task(kernel: Kernel) -> None:
    task_id = submit(kernel)
    assert submit(kernel) == task_id
    with pytest.raises(Fault) as conflict:
        submit(kernel, arguments={"to": "other@example.invalid", "body": "x"})
    assert conflict.value.code == "request_conflict"


# Execution


def test_success_publishes_output_receipt_and_evidence(kernel: Kernel, handler: Handler) -> None:
    task_id = submit(kernel)
    assert OperationRuntime(kernel).execute(task_id) == "succeeded"
    assert len(handler.calls) == 1
    arguments, context = handler.calls[0]
    assert arguments == {"to": "user@example.invalid", "body": "3 new listings"}
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        receipt = tx.get_operation(task_id, "execute")
        assert receipt.state is OperationState.SUCCEEDED
        assert receipt.retry_class is RetryClass.RECONCILIATION_REQUIRED
        assert receipt.receipt_id == context.receipt_id
        assert (
            receipt.request.arguments["descriptor_sha256"]
            == kernel.operations.get("mail.send").digest
        )
        output = tx.latest_object(ResourceRef(namespace="test", resource_id=f"{task_id}.output"))
        assert json.loads(tx.read_object(output)) == {
            "message_id": f"<{context.receipt_id}@cardea>"
        }
        task = tx.get_task(task_id)
        assert task.verification is not None and task.verification.passed
    # A finished task is never dispatched again.
    assert OperationRuntime(kernel).execute(task_id) == "succeeded"
    assert len(handler.calls) == 1


def test_invalid_output_fails_verification(kernel: Kernel, handler: Handler) -> None:
    handler.behaviour = "bad_output"
    task_id = submit(kernel)
    assert OperationRuntime(kernel).execute(task_id) == "failed"
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        assert tx.get_operation(task_id, "execute").state is OperationState.FAILED
        assert cast(Any, tx.get_task(task_id).failure).code == "verification_failed"


def test_rejection_is_a_definite_failure(kernel: Kernel, handler: Handler) -> None:
    handler.behaviour = "reject"
    task_id = submit(kernel)
    assert OperationRuntime(kernel).execute(task_id) == "failed"
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        assert tx.get_operation(task_id, "execute").state is OperationState.FAILED
        assert cast(Any, tx.get_task(task_id).failure).code == "operation_rejected"


def test_effect_crash_is_outcome_unknown_and_never_replayed(
    kernel: Kernel, handler: Handler
) -> None:
    handler.behaviour = "crash"
    task_id = submit(kernel)
    assert OperationRuntime(kernel).execute(task_id) == "failed"
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        assert tx.get_operation(task_id, "execute").state is OperationState.OUTCOME_UNKNOWN
        assert cast(Any, tx.get_task(task_id).failure).code == "reconciliation_required"
    handler.behaviour = "ok"
    assert OperationRuntime(kernel).execute(task_id) == "failed"
    assert len(handler.calls) == 1


def test_read_failure_is_not_uncertain(tmp_path: Path, kernel: Kernel, handler: Handler) -> None:
    kernel.operations = Registry(
        (OperationDescriptor.model_validate(descriptor(effect="read", timeout_seconds=1)),),
        {"test.handler": handler},
    )
    handler.behaviour = "crash"
    task_id = submit(kernel)
    assert OperationRuntime(kernel).execute(task_id) == "failed"
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        receipt = tx.get_operation(task_id, "execute")
        assert receipt.state is OperationState.FAILED
        assert receipt.retry_class is RetryClass.READ_ONLY


def test_interrupted_intent_after_restart_is_fenced_not_replayed(
    kernel: Kernel, handler: Handler
) -> None:
    task_id = submit(kernel)

    class Crash(Exception):
        pass

    def crash(stage: str) -> None:
        if stage == "operation.intent_committed":
            raise Crash

    kernel.fault = crash
    with pytest.raises(Crash):
        OperationRuntime(kernel).execute(task_id)
    kernel.fault = lambda stage: None
    assert task_state(kernel, task_id) is TaskState.RUNNING
    assert OperationRuntime(kernel).execute(task_id) == "failed"
    assert handler.calls == []
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        assert tx.get_operation(task_id, "execute").state is OperationState.OUTCOME_UNKNOWN


def test_revocation_after_admission_stops_dispatch(kernel: Kernel, handler: Handler) -> None:
    task_id = submit(kernel)
    provision(kernel.database, policy(RIGHTS), kernel.operations)
    assert OperationRuntime(kernel).execute(task_id) == "failed"
    assert handler.calls == []


def test_uninstalled_operation_stops_dispatch(kernel: Kernel, handler: Handler) -> None:
    task_id = submit(kernel)
    kernel.operations = Registry((), {})
    assert OperationRuntime(kernel).execute(task_id) == "failed"
    assert handler.calls == []


def test_timeout_on_effect_is_outcome_unknown(kernel: Kernel, handler: Handler) -> None:
    async def slow(arguments: dict[str, JsonValue], context: OperationContext) -> Any:
        import asyncio

        await asyncio.sleep(5)

    kernel.operations.handlers["test.handler"] = slow
    task_id = submit(kernel)
    assert OperationRuntime(kernel).execute(task_id) == "failed"
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        assert tx.get_operation(task_id, "execute").state is OperationState.OUTCOME_UNKNOWN


# Discovery and HTTP admission


def test_discovery_lists_registered_operations_by_authority(kernel: Kernel) -> None:
    client = TestClient(create_app(kernel))
    listed = client.get(
        "/v0.2/operations",
        params={"namespace": "test"},
        headers={"Authorization": "Bearer supervisor", "X-Cardea-Auth": "spiffe"},
    ).json()
    assert "mail.send" in listed["operations"]
    (entry,) = listed["registered"]
    assert entry["effect"] == "effect" and entry["tool"]["name"] == "mail.send"
    outsider = client.get(
        "/v0.2/operations",
        params={"namespace": "test"},
        headers={"Authorization": "Bearer outsider", "X-Cardea-Auth": "spiffe"},
    ).json()
    assert outsider["registered"] == []


def test_operation_task_route_admits(kernel: Kernel) -> None:
    client = TestClient(create_app(kernel))
    response = client.post(
        "/v0.2/operation-tasks",
        json=request().model_dump(mode="json"),
        headers={"Authorization": "Bearer supervisor", "X-Cardea-Auth": "spiffe"},
    )
    assert response.status_code == 202, response.text
    assert response.json()["task"]["runtime_id"] == "operation-v1"


# Idempotency scopes and reconciliation (ADR-026)


class Reconciler:
    def __init__(self) -> None:
        self.applied: bool | None = True
        self.calls = 0
        self.contexts: list[OperationContext] = []

    async def __call__(
        self, arguments: dict[str, JsonValue], context: OperationContext
    ) -> Reconciliation:
        self.calls += 1
        self.contexts.append(context)
        if self.applied is None:
            raise ConnectionError("provider events API unavailable")
        if self.applied:
            return Reconciliation(applied=True, output={"message_id": f"<{context.effect_key}>"})
        return Reconciliation(applied=False)


@pytest.fixture
def scoped(kernel: Kernel, handler: Handler) -> Reconciler:
    reconciler = Reconciler()
    kernel.operations = Registry(
        (
            OperationDescriptor.model_validate(
                descriptor(
                    timeout_seconds=1, idempotency_scope=["/to"], reconciler="test.reconcile"
                )
            ),
        ),
        {"test.handler": handler, "test.reconcile": reconciler},
    )
    return reconciler


def slot_send(kernel: Kernel, request_id: str, body: str, context: str = "slot-0830") -> str:
    return submit(
        kernel,
        request_id=request_id,
        idempotency_context=context,
        arguments={"to": "user@example.invalid", "body": body},
    )


def test_read_operations_cannot_declare_scopes() -> None:
    with pytest.raises(ValidationError):
        OperationDescriptor.model_validate(descriptor(effect="read", idempotency_scope=["/to"]))


def test_same_scope_is_deduplicated_even_when_reworded(
    kernel: Kernel, handler: Handler, scoped: Reconciler
) -> None:
    first = slot_send(kernel, "attempt-1", "3 new listings")
    assert OperationRuntime(kernel).execute(first) == "succeeded"
    second = slot_send(kernel, "attempt-2", "Three new listings today")
    assert OperationRuntime(kernel).execute(second) == "succeeded"
    assert len(handler.calls) == 1
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        one = tx.get_operation(first, "execute")
        two = tx.get_operation(second, "execute")
        assert two.result == {"output": cast(Any, one.result)["output"], "deduplicated": True}
        evidence = tx.latest_object(
            ResourceRef(namespace="test", resource_id=f"{second}.verification")
        )
        assert json.loads(tx.read_object(evidence))["deduplicated"] is True
    # Another slot is another effect.
    third = slot_send(kernel, "attempt-3", "3 new listings", context="slot-next")
    assert OperationRuntime(kernel).execute(third) == "succeeded"
    assert len(handler.calls) == 2
    assert handler.calls[0][1].effect_key != handler.calls[1][1].effect_key


def test_uncertain_scope_blocks_retries_until_reconciled_not_applied(
    kernel: Kernel, handler: Handler, scoped: Reconciler
) -> None:
    handler.behaviour = "crash"
    first = slot_send(kernel, "attempt-1", "3 new listings")
    assert OperationRuntime(kernel).execute(first) == "failed"
    handler.behaviour = "ok"
    blocked = slot_send(kernel, "attempt-2", "3 new listings")
    assert OperationRuntime(kernel).execute(blocked) == "failed"
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        assert cast(Any, tx.get_task(blocked).failure).code == "effect_in_doubt"
    assert len(handler.calls) == 1
    scoped.applied = False
    assert OperationRuntime(kernel).reconcile(first, "execute") == "failed"
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        receipt = tx.get_operation(first, "execute")
        assert receipt.reconciliation_evidence
        assert cast(Any, receipt.failure).code == "effect_not_applied"
    retry = slot_send(kernel, "attempt-3", "3 new listings")
    assert OperationRuntime(kernel).execute(retry) == "succeeded"
    assert len(handler.calls) == 2
    # The retry reuses the same effect key, so a provider-side key also deduplicates.
    assert handler.calls[0][1].effect_key == handler.calls[1][1].effect_key


def test_reconciled_applied_effect_deduplicates_later_attempts(
    kernel: Kernel, handler: Handler, scoped: Reconciler
) -> None:
    handler.behaviour = "crash"
    first = slot_send(kernel, "attempt-1", "3 new listings")
    assert OperationRuntime(kernel).execute(first) == "failed"
    assert OperationRuntime(kernel).reconcile(first, "execute") == "succeeded"
    # The reconciler sees the caller context the handler saw; no agent made this call.
    seen, observed = handler.calls[0][1], scoped.contexts[0]
    assert observed.idempotency_context == seen.idempotency_context == "slot-0830"
    assert observed.agent_id is None and observed.slot_time is None
    handler.behaviour = "ok"
    later = slot_send(kernel, "attempt-2", "3 new listings")
    assert OperationRuntime(kernel).execute(later) == "succeeded"
    assert len(handler.calls) == 1
    with pytest.raises(Fault) as again:
        OperationRuntime(kernel).reconcile(first, "execute")
    assert again.value.code == "not_uncertain"


def test_unavailable_reconciler_leaves_the_effect_uncertain(
    kernel: Kernel, handler: Handler, scoped: Reconciler
) -> None:
    handler.behaviour = "crash"
    first = slot_send(kernel, "attempt-1", "3 new listings")
    assert OperationRuntime(kernel).execute(first) == "failed"
    scoped.applied = None
    assert OperationRuntime(kernel).reconcile_pending() == 0
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        assert tx.get_operation(first, "execute").state is OperationState.OUTCOME_UNKNOWN
    scoped.applied = True
    assert OperationRuntime(kernel).reconcile_pending() == 1
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        assert tx.get_operation(first, "execute").state is OperationState.SUCCEEDED


def test_reconcile_route_requires_task_authority(
    kernel: Kernel, handler: Handler, scoped: Reconciler
) -> None:
    handler.behaviour = "crash"
    first = slot_send(kernel, "attempt-1", "3 new listings")
    assert OperationRuntime(kernel).execute(first) == "failed"
    client = TestClient(create_app(kernel))
    path = f"/v0.2/tasks/{first}/operations/execute/reconcile"
    outsider = client.post(
        path, headers={"Authorization": "Bearer outsider", "X-Cardea-Auth": "spiffe"}
    )
    assert outsider.status_code == 404
    response = client.post(
        path, headers={"Authorization": "Bearer supervisor", "X-Cardea-Auth": "spiffe"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "succeeded"
