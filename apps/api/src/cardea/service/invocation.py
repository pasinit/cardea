"""Invocation of one installed operation inside a running task (ADR-023, ADR-026).

These functions run inside the caller's transaction and lease. They are shared by the
single-operation task and by any runtime that dispatches several operations in one task.
"""

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

import sqlalchemy as sa
from pydantic import JsonValue

from cardea.kernel.models import (
    MAX_DOCUMENT_BYTES,
    KernelFailure,
    OperationRequest,
    OperationState,
    ResourceRef,
    ResourceUsage,
    RetryClass,
    Task,
    canonical_json,
)
from cardea.kernel.store import KernelTransaction
from cardea.service.confined import CallAbandoned, ExecutorBusy, WorkerFailed, WorkerNotStarted
from cardea.service.database import Fault
from cardea.service.models import now
from cardea.service.operations import (
    OperationContext,
    OperationRejected,
    Registry,
    http_status,
)

# Why a call failed, recorded in the receipt's failure details and told to the agent.
REASONS = frozenset(
    {
        "timeout",  # no result within the operation's timeout
        "queue_timeout",  # no worker slot within the profile's queue timeout; nothing ran
        "worker_exit",  # the confined worker exited, was killed or stopped without a result
        "worker_refused",  # the helper refused to start the worker; nothing ran
        "abandoned",  # the caller gave up while the call waited for a slot; nothing ran
        "upstream_http_error",  # the handler failed on an HTTP error from a remote service
        "invalid_output",  # the result did not match the operation's output schema or size
        "rejected",  # the operation itself refused the call (handler's own message)
        "denied",  # authority changed before the result could be recorded
        "error",  # any other handler exception (class name only)
    }
)
MAX_SUMMARY = 200
FAILURE_MESSAGE = "Operation did not produce a verified result"


def bounded(text: str) -> str:
    """One printable line of at most 200 characters."""
    return re.sub(r"\s+", " ", re.sub(r"[^\x20-\x7e\s]", "", text)).strip()[:MAX_SUMMARY]


@dataclass(frozen=True)
class Failure:
    """A definite failure: the receipt code plus a bounded, sanitized reason."""

    code: str
    reason: str
    summary: str = ""
    retryable: bool = False

    def details(self) -> dict[str, JsonValue]:
        return {"reason": self.reason, "summary": bounded(self.summary)}

    def describe(self) -> str:
        return f"{self.reason}: {bounded(self.summary)}" if self.summary else self.reason


def classify(error: BaseException, timeout: float) -> Failure:
    """Describe a handler failure without exception text that could carry secrets.

    Only messages written for the agent (``OperationRejected``) are kept; for any other
    exception the summary is its class name and, for HTTP errors, the status code.
    """
    if isinstance(error, ExecutorBusy):
        return Failure("executor_busy", "queue_timeout", str(error), retryable=True)
    if isinstance(error, CallAbandoned):
        return Failure("call_abandoned", "abandoned", str(error), retryable=True)
    if isinstance(error, WorkerNotStarted):
        return Failure("operation_rejected", "worker_refused", str(error))
    if isinstance(error, OperationRejected):
        return Failure("operation_rejected", "rejected", str(error))
    if isinstance(error, WorkerFailed):
        return Failure(error.code, error.reason, error.summary)
    if isinstance(error, TimeoutError):
        return Failure("operation_failed", "timeout", f"no result within {timeout:.0f} s")
    status = http_status(error)
    if status is not None:
        return Failure(
            "operation_failed", "upstream_http_error", f"{type(error).__name__} (HTTP {status})"
        )
    return Failure("operation_failed", "error", type(error).__name__)


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
    recorded: dict[str, JsonValue] = {
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
    }
    if len(canonical_json(recorded)) > MAX_DOCUMENT_BYTES:
        # A receipt document holds at most 64 KiB; refuse before any intent exists.
        raise Fault("arguments_too_large", "Arguments exceed the receipt's 64 KiB limit", 413)
    receipt = tx.admit_operation(
        OperationRequest(
            operation_id=operation_id,
            task_id=task.task_id,
            subject_id=task.agent_id,
            name=name,
            resource=ResourceRef(namespace=namespace, resource_id=task.task_id),
            arguments=recorded,
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
    error: str | Failure,
    elapsed_ms: int,
) -> tuple[Failure | None, bytes]:
    """Record a definite outcome. Returns the failure (None on success) and output."""
    receipt = tx.get_operation(task.task_id, operation_id)
    content = b""
    failure = (
        error
        if isinstance(error, Failure)
        else Failure(error, "denied" if error.endswith("_denied") else "error", error)
        if error
        else None
    )
    if failure is None:
        checked = registry.check_output(receipt.request.name, output)
        if checked is None:
            failure = Failure(
                "verification_failed",
                "invalid_output",
                "result does not match the operation's output schema or size limit "
                "(at most 64 KiB is recorded)",
            )
        else:
            content = checked
    error = failure.code if failure else ""
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
        failure=KernelFailure(
            code=error,
            message=FAILURE_MESSAGE,
            retryable=failure.retryable,
            details=failure.details(),
        )
        if state is OperationState.FAILED and failure is not None
        else None,
        result={"output": json.loads(content)} if state is OperationState.SUCCEEDED else None,
        usage=ResourceUsage(steps=1, tool_milliseconds=elapsed_ms, output_bytes=len(content)),
    )
    _scope(conn, task.task_id, operation_id, "succeeded" if not error else "failed")
    return failure, content


def uncertain(
    conn: sa.Connection,
    tx: KernelTransaction,
    task: Task,
    *,
    lease_id: str,
    operation_id: str,
    failure: Failure | None = None,
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
                code="operation_outcome_unknown",
                message="Interrupted operation; no replay",
                details=failure.details() if failure else {},
            ),
            usage=ResourceUsage(steps=1),
        )
    _scope(conn, task.task_id, operation_id, "unknown")


def timeout_for(
    registry: Registry, name: str, lease_expires: datetime, margin: float, queue: float = 0.0
) -> float:
    """The operation's timeout plus any time it may wait for a worker slot, within the lease."""
    return min(
        float(registry.get(name).cardea.timeout_seconds) + queue,
        (lease_expires - now() - timedelta(seconds=margin)).total_seconds(),
    )
