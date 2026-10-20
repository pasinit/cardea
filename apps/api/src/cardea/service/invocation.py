"""Invocation of one installed operation inside a running task (ADR-023, ADR-026).

These functions run inside the caller's transaction and lease. They are shared by the
single-operation task and by any runtime that dispatches several operations in one task.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

import sqlalchemy as sa
from pydantic import JsonValue

from cardea.kernel.models import (
    KernelFailure,
    OperationRequest,
    OperationState,
    ResourceRef,
    ResourceUsage,
    RetryClass,
    Task,
)
from cardea.kernel.store import KernelTransaction
from cardea.service.database import Fault
from cardea.service.models import now
from cardea.service.operations import OperationContext, Registry


@dataclass(frozen=True)
class Admitted:
    context: OperationContext
    # Output of an earlier invocation in the same effect scope; the handler must not run.
    deduplicated: dict[str, JsonValue] | None


def admit(
    conn: sa.Connection,
    tx: KernelTransaction,
    registry: Registry,
    task: Task,
    *,
    lease_id: str,
    operation_id: str,
    name: str,
    namespace: str,
    arguments: dict[str, JsonValue],
    idempotency_context: str | None,
    deadline: datetime,
    executor: dict[str, JsonValue] | None = None,
    agent_id: str | None = None,
    slot_time: str | None = None,
) -> Admitted:
    """Commit the intent (or a deduplicated receipt) before any handler can run.

    Effect scopes use ``idempotency_context`` when set, else the task, so another task
    with the same context (e.g. a second task for one scheduled slot) cannot repeat an
    effect. ``executor`` pins the installed worker profile of a confined operation
    (ADR-028). ``agent_id`` and ``slot_time`` describe the caller to the handler.
    """
    descriptor = registry.get(name)
    registry.validate_arguments(name, arguments)
    key = registry.scope_key(name, namespace, idempotency_context or task.task_id, arguments)
    provenance: dict[str, JsonValue] = {
        k: v
        for k, v in (
            ("agent_id", agent_id),
            ("idempotency_context", idempotency_context),
            ("slot_time", slot_time),
        )
        if v is not None
    }
    prior: dict[str, JsonValue] | None = None
    if key is not None:
        row = (
            conn.exec_driver_sql(
                "SELECT task_id,operation_id,state FROM effect_scopes WHERE scope_key=?", (key,)
            )
            .mappings()
            .first()
        )
        if row is not None and row["state"] == "succeeded":
            earlier = tx.get_operation(str(row["task_id"]), str(row["operation_id"]))
            assert earlier.result is not None
            prior = earlier.result["output"]  # type: ignore[assignment]
        elif row is not None and row["state"] in {"running", "unknown"}:
            # Either in flight or interrupted: a second dispatch could repeat the effect.
            raise Fault(
                "effect_in_doubt", "An earlier invocation of this effect is unresolved", 409
            )
    receipt = tx.admit_operation(
        OperationRequest(
            operation_id=operation_id,
            task_id=task.task_id,
            subject_id=task.agent_id,
            name=name,
            resource=ResourceRef(namespace=namespace, resource_id=task.task_id),
            arguments={
                "arguments": arguments,
                "operation_version": descriptor.cardea.version,
                "descriptor_sha256": descriptor.digest,
                "scope_key": key,
                "policy_revision": conn.exec_driver_sql(
                    "SELECT value FROM service_meta WHERE name='policy_revision'"
                ).scalar_one(),
                **({"executor": executor} if executor is not None else {}),
                # Recorded so a reconciler sees the context the handler saw.
                **({"context": provenance} if provenance else {}),
            },
            deadline=deadline,
        ),
        grant_ids=task.capability_ids,
        retry_class=RetryClass.READ_ONLY
        if descriptor.cardea.effect == "read"
        else RetryClass.RECONCILIATION_REQUIRED,
        expected_task_revision=task.revision,
        lease_id=lease_id,
        at=now(),
    )
    receipt = tx.change_operation(
        task.task_id,
        operation_id,
        OperationState.RUNNING,
        expected_revision=receipt.revision,
        lease_id=lease_id,
        at=now(),
    )
    context = OperationContext(
        task_id=task.task_id,
        namespace=namespace,
        receipt_id=receipt.receipt_id,
        deadline=deadline,
        effect_key=key or receipt.receipt_id,
        lease_id=lease_id,
        agent_id=agent_id,
        idempotency_context=idempotency_context,
        slot_time=slot_time,
    )
    if prior is not None:
        tx.change_operation(
            task.task_id,
            operation_id,
            OperationState.SUCCEEDED,
            expected_revision=receipt.revision,
            lease_id=lease_id,
            at=now(),
            result={"output": prior, "deduplicated": True},
            usage=ResourceUsage(steps=1),
        )
        return Admitted(context, prior)
    if key is not None:
        conn.exec_driver_sql(
            "INSERT INTO effect_scopes VALUES (?,?,?,?,?,'running',?) ON CONFLICT(scope_key) "
            "DO UPDATE SET task_id=excluded.task_id,operation_id=excluded.operation_id,"
            "state='running',updated_at=excluded.updated_at",
            (key, namespace, name, task.task_id, operation_id, now().isoformat()),
        )
    return Admitted(context, None)


def _scope(conn: sa.Connection, task_id: str, operation_id: str, state: str) -> None:
    conn.exec_driver_sql(
        "UPDATE effect_scopes SET state=?,updated_at=? WHERE task_id=? AND operation_id=?",
        (state, now().isoformat(), task_id, operation_id),
    )


def settle(
    conn: sa.Connection,
    tx: KernelTransaction,
    registry: Registry,
    task: Task,
    *,
    lease_id: str,
    operation_id: str,
    output: object,
    error: str,
    elapsed_ms: int,
) -> tuple[str, bytes]:
    """Record a definite outcome. Returns the error code (empty on success) and output."""
    receipt = tx.get_operation(task.task_id, operation_id)
    content = b""
    if not error:
        checked = registry.check_output(receipt.request.name, output)
        if checked is None:
            error = "verification_failed"
        else:
            content = checked
    state = (
        OperationState.CANCELLED
        if error == "cancel_requested"
        else OperationState.FAILED
        if error
        else OperationState.SUCCEEDED
    )
    tx.change_operation(
        task.task_id,
        operation_id,
        state,
        expected_revision=receipt.revision,
        lease_id=lease_id,
        at=now(),
        failure=KernelFailure(code=error, message="Operation did not produce a verified result")
        if state is OperationState.FAILED
        else None,
        result={"output": json.loads(content)} if state is OperationState.SUCCEEDED else None,
        usage=ResourceUsage(steps=1, tool_milliseconds=elapsed_ms, output_bytes=len(content)),
    )
    _scope(conn, task.task_id, operation_id, "succeeded" if not error else "failed")
    return error, content


def uncertain(
    conn: sa.Connection, tx: KernelTransaction, task: Task, *, lease_id: str, operation_id: str
) -> None:
    """Record that an effect may have happened; it is never dispatched again."""
    receipt = tx.get_operation(task.task_id, operation_id)
    if receipt.state is OperationState.RUNNING:
        tx.change_operation(
            task.task_id,
            operation_id,
            OperationState.OUTCOME_UNKNOWN,
            expected_revision=receipt.revision,
            lease_id=lease_id,
            at=now(),
            failure=KernelFailure(
                code="operation_outcome_unknown", message="Interrupted operation; no replay"
            ),
            usage=ResourceUsage(steps=1),
        )
    _scope(conn, task.task_id, operation_id, "unknown")


def timeout_for(registry: Registry, name: str, lease_expires: datetime, margin: float) -> float:
    return min(
        float(registry.get(name).cardea.timeout_seconds),
        (lease_expires - now() - timedelta(seconds=margin)).total_seconds(),
    )
