"""Invocation of one installed operation inside a running task (ADR-023, ADR-026).

These functions run inside the caller's transaction and lease. They are shared by the
single-operation task and by any runtime that dispatches several operations in one task.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from hashlib import sha256

import jsonpointer  # type: ignore[import-untyped]  # ships no type information
import sqlalchemy as sa
from pydantic import JsonValue

from cardea.kernel.models import (
    MAX_DOCUMENT_BYTES,
    KernelFailure,
    ObjectVersion,
    OperationRequest,
    OperationState,
    ResourceRef,
    ResourceUsage,
    RetryClass,
    Task,
    canonical_json,
)
from cardea.kernel.store import KernelTransaction, StoreError
from cardea.service.confined import CallAbandoned, ExecutorBusy, WorkerFailed, WorkerNotStarted
from cardea.service.database import Fault
from cardea.service.models import now
from cardea.service.operations import (
    ContentRefused,
    OperationContext,
    OperationDescriptor,
    OperationRejected,
    Registry,
    http_status,
)
from cardea.service.policy import Policy

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
    # Content the gate refused (ADR-037): redacted from the receipt, never recorded.
    redact: tuple[str, ...] = field(default=(), repr=False, compare=False)

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
    if isinstance(error, ContentRefused):
        return Failure(error.code, "rejected", str(error), redact=error.refused)
    if isinstance(error, OperationRejected):
        return Failure(error.code, "rejected", str(error))
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


# Total bytes of declared object inputs one call may stage (ADR-037 change bundles).
MAX_INPUT_BYTES = 16 * 1024 * 1024


def _argument(arguments: dict[str, JsonValue], pointer: str) -> JsonValue:
    try:
        value: JsonValue = jsonpointer.resolve_pointer(arguments, pointer)
    except jsonpointer.JsonPointerException:
        return None
    return value


def _stage_inputs(
    conn: sa.Connection,
    tx: KernelTransaction,
    policy: Policy,
    task: Task,
    namespace: str,
    descriptor: OperationDescriptor,
    arguments: dict[str, JsonValue],
) -> dict[str, bytes]:
    """Read the objects a call's declared inputs name, under the caller's live ceiling.

    An input is one object version, or a confined operation's artifact listing
    (`{sha256, parts: [object versions]}`, as `code.session` returns its change bundle),
    whose parts are joined and checked against `sha256`. Only objects of the call's own
    namespace, at exact versions, are staged; the store verifies each part's digest.
    """
    staged: dict[str, bytes] = {}
    for pointer in descriptor.cardea.inputs or ():
        value = _argument(arguments, pointer)
        expected = value.get("sha256") if isinstance(value, dict) else None
        listed = value.get("parts") if isinstance(value, dict) and "parts" in value else [value]
        try:
            if not isinstance(listed, list) or not 1 <= len(listed) <= 16:
                raise ValueError("an input lists 1 to 16 parts")
            references = [ObjectVersion.model_validate(part) for part in listed]
        except ValueError:
            raise Fault(
                "invalid_arguments", f"Input {pointer} must name an object version", 422
            ) from None
        if any(reference.namespace != namespace for reference in references):
            raise Fault("input_namespace", "Inputs must be objects of the call's namespace", 403)
        policy.ceiling(task.task_id, "objects.read")
        try:
            content = b"".join(tx.read_object(reference) for reference in references)
        except StoreError as error:
            raise Fault("input_unavailable", f"Input {pointer}: {error.code}", 409) from None
        if expected is not None and sha256(content).hexdigest() != expected:
            raise Fault("input_digest_mismatch", f"Input {pointer} does not match its sha256", 409)
        staged[pointer] = content
        if sum(len(v) for v in staged.values()) > MAX_INPUT_BYTES:
            raise Fault("input_too_large", "Inputs exceed 16 MiB", 413)
    return staged


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
    approval: dict[str, JsonValue] | None = None,
) -> Admitted:
    """Commit the intent (or a deduplicated receipt) before any handler can run.

    Effect scopes use ``idempotency_context`` when set, else the task, so another task
    with the same context (e.g. a second task for one scheduled slot) cannot repeat an
    effect. ``executor`` pins the installed worker profile of a confined operation
    (ADR-028). ``agent_id`` and ``slot_time`` describe the caller to the handler.
    ``approval`` is the decision that let this intent run (ADR-036); it is recorded in the
    receipt's ``arguments.context.approval``.
    """
    descriptor = registry.get(name)
    registry.validate_arguments(name, arguments)
    policy = Policy(conn)
    if descriptor.cardea.resource is not None:
        # A resource-qualified grant, e.g. `repo:<alias>`, must be in the live ceiling.
        resource = descriptor.cardea.resource
        value = _argument(arguments, resource.argument)
        if not isinstance(value, str) or not value:
            raise Fault("invalid_arguments", "The resource argument must name a resource", 422)
        policy.ceiling(task.task_id, f"{resource.grant}:{value}")
    inputs = _stage_inputs(conn, tx, policy, task, namespace, descriptor, arguments)
    member = conn.exec_driver_sql(
        "SELECT group_name,pool FROM group_members WHERE principal_id=? AND namespace=?",
        (agent_id, namespace),
    ).first()
    group = str(member[0]) if agent_id is not None and member is not None else None
    pool = str(member[1]) if group is not None and member is not None else None
    key = registry.scope_key(name, namespace, idempotency_context or task.task_id, arguments)
    provenance: dict[str, JsonValue] = {
        k: v
        for k, v in (
            ("agent_id", agent_id),
            ("idempotency_context", idempotency_context),
            ("slot_time", slot_time),
            ("group", group),
        )
        if v is not None
    }
    if approval is not None:
        provenance["approval"] = approval
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
        group=group,
        pool=pool,
        inputs=inputs,
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
    if failure is not None and failure.code == "content_refused" and failure.redact:
        redact(conn, tx, task.task_id, operation_id, failure.redact)
    return failure, content


MARKER = "content_refused"
# Matched lines shorter than this are not used to find copies, so they cannot redact
# unrelated values; the refused inputs themselves are always redacted whole.
MIN_NEEDLE = 6


def scrub(value: JsonValue, refused: tuple[str, ...]) -> tuple[JsonValue, bool]:
    """`value` with every string that is, or contains, refused content replaced by a
    marker holding only its SHA-256 and length."""
    if isinstance(value, str):
        hit = any(value == needle for needle in refused) or any(
            len(needle) >= MIN_NEEDLE and needle in value for needle in refused
        )
        if not hit:
            return value, False
        data = value.encode()
        marked: JsonValue = {
            "redacted": MARKER,
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
        }
        return marked, True
    if isinstance(value, dict):
        changed = False
        out: dict[str, JsonValue] = {}
        for key, item in value.items():
            out[key], hit = scrub(item, refused)
            changed |= hit
        return out, changed
    if isinstance(value, list):
        items = [scrub(item, refused) for item in value]
        return [item for item, _ in items], any(hit for _, hit in items)
    return value, False


def redact(
    conn: sa.Connection,
    tx: KernelTransaction,
    task_id: str,
    operation_id: str,
    refused: tuple[str, ...],
) -> None:
    """Remove refused content from the receipt and the task's bound request (ADR-037).

    Both keep their digests, so identity and idempotency hold; a refused call is
    definite, so it is never replayed from these arguments.
    """
    needles = tuple(n for n in dict.fromkeys(refused) if n)
    receipt = tx.get_operation(task_id, operation_id)
    arguments, changed = scrub(dict(receipt.request.arguments), needles)
    if changed and isinstance(arguments, dict):
        tx.redact_refused_operation(task_id, operation_id, arguments, at=now())
    row = conn.exec_driver_sql(
        "SELECT request_json FROM task_bindings WHERE task_id=?", (task_id,)
    ).first()
    if row is not None:
        request, bound = scrub(json.loads(row[0]), needles)
        if bound:
            conn.exec_driver_sql(
                "UPDATE task_bindings SET request_json=? WHERE task_id=?",
                (json.dumps(request), task_id),
            )
    # A call held for approval and run as `apr-NN` (ADR-036) froze its arguments there.
    held = conn.exec_driver_sql(
        "SELECT arguments_json FROM approval_requests WHERE task_id=? AND operation_id=?",
        (task_id, operation_id),
    ).first()
    if held is not None:
        frozen, hit = scrub(json.loads(held[0]), needles)
        if hit:
            conn.exec_driver_sql(
                "UPDATE approval_requests SET arguments_json=? WHERE task_id=? AND operation_id=?",
                (json.dumps(frozen), task_id, operation_id),
            )


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
