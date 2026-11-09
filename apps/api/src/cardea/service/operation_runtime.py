"""One installed operation as a task: durable intent, live ceiling, checked output.

See ADR-023 (registry), ADR-026 (idempotency scopes and reconciliation) and ADR-036
(approval waits): a call an approval rule requires is frozen and the task waits; after an
approval the stored intent runs as `apr-01` through the same invocation core.
"""

import asyncio
import hashlib
import json
import logging
import time
from datetime import timedelta
from uuid import uuid4

import rfc8785
import sqlalchemy as sa
from pydantic import JsonValue

from cardea.kernel.models import (
    Checkpoint,
    DispatchLease,
    EventWait,
    KernelFailure,
    OperationState,
    ResourceRef,
    Task,
    TaskState,
    VerificationRecord,
)
from cardea.kernel.store import KernelTransaction
from cardea.service import approval_requests, invocation, mailbox
from cardea.service.approvals import ApprovalRequirement
from cardea.service.confined import WorkerFailed
from cardea.service.database import Fault, borrow
from cardea.service.kernel import TERMINAL, Kernel
from cardea.service.models import RECORD_OPERATIONS, OperationTaskRequest, now
from cardea.service.operations import (
    OperationContext,
    OperationDescriptor,
    OperationRejected,
    Reconciliation,
)
from cardea.service.policy import Policy

log = logging.getLogger(__name__)
# Lease time beyond the handler timeout for settlement; settlement after expiry is fenced.
LEASE_MARGIN = 5.0
SETTLE_MARGIN = 1.0
EXECUTE = "execute"
# A single-operation task has at most one approval request, so its effect is `apr-01`.
APPROVED = "apr-01"
# Receipt context keys that are also OperationContext fields.
CONTEXT_FIELDS = frozenset({"agent_id", "idempotency_context", "slot_time"})
# Refusals before an approved intent runs that mean the live ceiling no longer holds.
AUTHORITY_CODES = frozenset(
    {
        "authority_denied",
        "task_grant_denied",
        "principal_disabled",
        "restored_scope_sealed",
        "runtime_binding",
        "invalid_provenance",
    }
)


class _Refused(Exception):
    def __init__(self, fault: Fault) -> None:
        self.fault = fault


class _Void(Exception):
    """An approved intent that must not run; the request becomes void with this reason."""

    def __init__(self, reason: str) -> None:
        self.reason = reason


def void_reason_for(fault: Fault) -> str:
    """Why an approved intent cannot run, from the refusal that stopped it."""
    if fault.code in approval_requests.VOID_REASONS:
        return approval_requests.VOID_REASONS[fault.code]
    return "authority_lost" if fault.code in AUTHORITY_CODES else fault.code


class OperationRuntime:
    def __init__(self, kernel: Kernel) -> None:
        self.kernel = kernel

    def guard(
        self, conn: sa.Connection, task: Task
    ) -> tuple[OperationTaskRequest, OperationDescriptor]:
        runtime = self.kernel.auth.runtime_proof().resolve(conn)
        policy = Policy(conn)
        binding = policy.binding(task.task_id)
        request = OperationTaskRequest.model_validate_json(binding["request_json"])
        descriptor = self.kernel.operations.get(request.operation)
        for right in (
            # Record operations are authorized per call by their record grant (ADR-034).
            *(() if request.operation in RECORD_OPERATIONS else (request.operation,)),
            "objects.read",
            "objects.write",
            *descriptor.cardea.requires,
        ):
            policy.ceiling(task.task_id, right)
        if runtime != binding["runtime_id"] or runtime != self.kernel.runtime_id:
            raise Fault("runtime_binding", "Runtime does not match task")
        if task.state in TERMINAL or now() >= task.limits.deadline:
            raise Fault("deadline_or_terminal", "Task cannot dispatch")
        if conn.exec_driver_sql(
            "SELECT 1 FROM control_requests WHERE task_id=? AND kind='cancel' LIMIT 1",
            (task.task_id,),
        ).first():
            raise Fault("cancel_requested", "Cancellation requested")
        # The descriptor may have changed since admission; arguments must still fit it.
        self.kernel.operations.validate_arguments(request.operation, request.arguments)
        return request, descriptor

    def execute(self, task_id: str, approval: str | None = None) -> str:
        """Workflow step entry point: a refusal before dispatch fails the task.

        ``approval`` names the request whose decision this continuation runs (ADR-036).
        """
        try:
            return self._execute(task_id, approval)
        except _Refused as refused:
            with self.kernel.database.engine.begin() as conn, borrow(conn) as tx:
                task = tx.get_task(task_id)
                if task.state in TERMINAL:
                    return task.state.value
                tx.change_task(
                    task_id,
                    TaskState.FAILED,
                    expected_revision=task.revision,
                    at=now(),
                    failure=KernelFailure(code=refused.fault.code, message=refused.fault.message),
                )
                return "failed"
        except _Void as void:
            assert approval is not None
            with self.kernel.database.engine.begin() as conn, borrow(conn) as tx:
                task = tx.get_task(task_id)
                if task.state in TERMINAL:
                    return task.state.value
                row = approval_requests.get(conn, approval)
                approval_requests.mark_void(
                    tx, conn, row, void.reason, self.kernel.runtime_id, now()
                )
                return self._fail_closed(tx, task, "approval_void", row, reason=void.reason)

    def _fail_closed(
        self,
        tx: KernelTransaction,
        task: Task,
        code: str,
        row: sa.RowMapping,
        **details: JsonValue,
    ) -> str:
        """Fail a task whose approval did not lead to execution; nothing was dispatched."""
        tx.change_task(
            task.task_id,
            TaskState.FAILED,
            expected_revision=task.revision,
            lease_id=task.lease.lease_id if task.lease else None,
            at=now(),
            failure=KernelFailure(
                code=code,
                message="Approval did not allow the operation to run",
                details={"request_id": row["request_id"], **details},
            ),
        )
        return "failed"

    def _closed(
        self, conn: sa.Connection, tx: KernelTransaction, task: Task, row: sa.RowMapping
    ) -> str | None:
        """The outcome of a continuation whose request will not run; None when approved."""
        state = row["state"]
        if state == "approved":
            return None
        if state == "rejected":
            decision = approval_requests.decision_for(conn, str(row["request_id"]))
            return self._fail_closed(
                tx,
                task,
                "approval_rejected",
                row,
                decision_id=decision["decision_id"] if decision else None,
            )
        if state == "void":
            return self._fail_closed(tx, task, "approval_void", row, reason=row["void_reason"])
        return task.state.value

    def _approved(
        self, conn: sa.Connection, row: sa.RowMapping, request: OperationTaskRequest
    ) -> dict[str, JsonValue]:
        """Execution checks (ADR-036 item 6) beyond the guard's live ceiling."""
        descriptor = self.kernel.operations.get(request.operation)
        if descriptor.cardea.version != row["operation_version"]:
            raise _Void("operation_changed")
        digest = approval_requests.argument_sha256(request.arguments)
        intent = approval_requests.intent_digest(
            task_id=str(row["task_id"]),
            namespace=request.namespace,
            operation=request.operation,
            version=int(row["operation_version"]),
            arguments_sha256=digest,
            idempotency_context=request.idempotency_context,
        )
        if digest != row["argument_sha256"] or intent != row["intent_digest"]:
            raise _Void("intent_changed")
        decision = approval_requests.decision_for(conn, str(row["request_id"]))
        assert decision is not None
        if not approval_requests.holds_approve(Policy(conn), str(decision["approver_id"]), row):
            raise _Void("approver_authority_lost")
        return approval_requests.receipt_block(conn, row)

    def _park(
        self,
        conn: sa.Connection,
        tx: KernelTransaction,
        task: Task,
        request: OperationTaskRequest,
        needed: ApprovalRequirement,
    ) -> str:
        """Freeze the intent and move RUNNING -> WAITING in the same transaction."""
        assert task.lease is not None
        row = approval_requests.propose(
            conn,
            tx,
            task,
            Policy(conn).binding(task.task_id),
            request,
            self.kernel.operations,
            needed,
            operation_id=APPROVED,
            at=now(),
        )
        at = now()
        checkpoint = Checkpoint(
            checkpoint_id=f"checkpoint-{task.task_id}",
            task_id=task.task_id,
            saved_at=at,
            state={"stage": "awaiting_approval", "approval_request_id": row["request_id"]},
            event_cursor=tx.latest_cursor(),
        )
        tx.change_task(
            task.task_id,
            TaskState.WAITING,
            expected_revision=task.revision,
            lease_id=task.lease.lease_id,
            at=at,
            checkpoint=checkpoint,
            wait=EventWait(
                namespace=request.namespace,
                event_type=f"approval.{row['request_id']}",
                after_cursor=checkpoint.event_cursor,
            ),
        )
        return "waiting"

    def _execute(self, task_id: str, approval: str | None = None) -> str:
        # An existing running task is an interrupted intent, never permission to replay.
        with self.kernel.database.engine.begin() as conn, borrow(conn) as tx:
            task = tx.get_task(task_id)
            if task.state in TERMINAL:
                return task.state.value
            row = approval_requests.for_task(conn, task_id)
            if approval is None:
                if row is not None:
                    # The proposal committed; the decision's continuation owns the task now.
                    return task.state.value
                operation_id = EXECUTE
            else:
                row = approval_requests.get(conn, approval)
                if row["task_id"] != task_id:
                    raise Fault("approval_binding", "Approval request belongs to another task")
                operation_id = str(row["operation_id"])
                if task.state is TaskState.WAITING:
                    return task.state.value
            recovering = task.state is TaskState.RUNNING
            if not recovering:
                if row is not None:
                    closed = self._closed(conn, tx, task, row)
                    if closed is not None:
                        return closed
                try:
                    request, descriptor = self.guard(conn, task)
                    block = self._approved(conn, row, request) if row is not None else None
                    handler = self.kernel.handler(request.operation)
                    executor = self.kernel.executor_record(request.operation)
                    queue = self.kernel.queue_allowance(request.operation)
                    # Approval is an extra conjunct inside the live ceiling the guard checked.
                    needed = (
                        approval_requests.evaluate(
                            conn,
                            self.kernel.operations,
                            request.operation,
                            request.namespace,
                            request.arguments,
                        )
                        if row is None
                        else None
                    )
                    at = now()
                    expires = min(
                        task.limits.deadline,
                        at
                        + timedelta(
                            seconds=descriptor.cardea.timeout_seconds + queue + LEASE_MARGIN
                        ),
                    )
                    task = tx.change_task(
                        task_id,
                        TaskState.RUNNING,
                        expected_revision=task.revision,
                        at=at,
                        lease=DispatchLease(
                            lease_id="lease-" + uuid4().hex,
                            worker_id=self.kernel.runtime_id,
                            acquired_at=at,
                            expires_at=expires,
                        ),
                    )
                    assert task.lease is not None
                    if needed is not None and needed.required:
                        return self._park(conn, tx, task, request, needed)
                    admitted = invocation.admit(
                        conn,
                        tx,
                        self.kernel.operations,
                        task,
                        lease_id=task.lease.lease_id,
                        operation_id=operation_id,
                        name=request.operation,
                        namespace=request.namespace,
                        arguments=request.arguments,
                        idempotency_context=request.idempotency_context,
                        deadline=task.limits.deadline,
                        executor=executor,
                        slot_time=request.slot_time.isoformat() if request.slot_time else None,
                        approval=block,
                    )
                    if row is not None:
                        # The intent is committed and never replayed: the request is executed.
                        conn.exec_driver_sql(
                            "UPDATE approval_requests SET state='executed',receipt_id=?,"
                            "updated_at=? WHERE request_id=? AND state='approved'",
                            (admitted.context.receipt_id, now().isoformat(), row["request_id"]),
                        )
                except Fault as error:
                    # Raising rolls back the lease and intent: nothing was dispatched.
                    if row is not None:
                        raise _Void(void_reason_for(error)) from None
                    raise _Refused(error) from None
                timeout = invocation.timeout_for(
                    self.kernel.operations, request.operation, expires, SETTLE_MARGIN, queue
                )
        self.kernel.fault("operation.intent_committed")
        if recovering:
            # A confined worker may outlive the service: stop its whole process tree.
            self.kernel.reap_workers(task_id)
            return self.uncertain(task_id, operation_id)
        if admitted.deduplicated is not None:
            return self.complete(
                task_id, rfc8785.dumps(admitted.deduplicated), operation_id, deduplicated=True
            )
        effect = descriptor.cardea.effect == "effect"
        started = time.monotonic()
        try:
            if timeout <= 0:
                raise OperationRejected("no time left to dispatch")
            output = asyncio.run(
                asyncio.wait_for(handler(dict(request.arguments), admitted.context), timeout)
            )
        except OperationRejected as rejected:
            # Nothing happened: the handler refused, the helper refused or no slot freed up.
            return self.settle(
                task_id, None, invocation.classify(rejected, timeout), started, operation_id
            )
        except WorkerFailed as failed:
            # The confined worker started: an effect may have happened (ADR-028).
            log.warning("confined worker failed: %s", failed.code, extra={"task_id": task_id})
            if effect:
                return self.uncertain(task_id, operation_id)
            return self.settle(
                task_id, None, invocation.classify(failed, timeout), started, operation_id
            )
        except Exception as error:
            log.exception("installed operation failed", extra={"task_id": task_id})
            if effect:
                return self.uncertain(task_id, operation_id)
            return self.settle(
                task_id, None, invocation.classify(error, timeout), started, operation_id
            )
        self.kernel.fault("operation.before_settle")
        return self.settle(task_id, output, "", started, operation_id)

    def uncertain(self, task_id: str, operation_id: str = EXECUTE) -> str:
        # Never infer a result: fence the lease, then let recovery require reconciliation.
        while True:
            with self.kernel.database.engine.begin() as conn, borrow(conn) as tx:
                task = tx.get_task(task_id)
                if task.state in TERMINAL:
                    return task.state.value
                assert task.lease is not None
                if now() >= task.lease.expires_at:
                    conn.exec_driver_sql(
                        "UPDATE effect_scopes SET state='unknown' "
                        "WHERE task_id=? AND state='running'",
                        (task_id,),
                    )
                    return tx.recover_task(
                        task_id, expected_revision=task.revision, at=now()
                    ).state.value
                invocation.uncertain(
                    conn, tx, task, lease_id=task.lease.lease_id, operation_id=operation_id
                )
            time.sleep(0.1)

    def settle(
        self,
        task_id: str,
        output: object,
        error: str | invocation.Failure,
        started: float,
        operation_id: str = EXECUTE,
    ) -> str:
        elapsed = max(0, int((time.monotonic() - started) * 1000))
        with self.kernel.database.engine.begin() as conn, borrow(conn) as tx:
            task = tx.get_task(task_id)
            if task.state in TERMINAL:
                return task.state.value
            assert task.lease is not None
            if now() >= task.lease.expires_at:
                return tx.recover_task(
                    task_id, expected_revision=task.revision, at=now()
                ).state.value
            try:
                self.guard(conn, task)
            except Fault as denied:
                error = error or invocation.Failure(denied.code, "denied", denied.message)
            failure, content = invocation.settle(
                conn,
                tx,
                self.kernel.operations,
                task,
                lease_id=task.lease.lease_id,
                operation_id=operation_id,
                output=output,
                error=error,
                elapsed_ms=elapsed,
            )
            if failure is not None:
                target = (
                    TaskState.CANCELLED if failure.code == "cancel_requested" else TaskState.FAILED
                )
                tx.change_task(
                    task_id,
                    target,
                    expected_revision=task.revision,
                    lease_id=task.lease.lease_id,
                    at=now(),
                    failure=KernelFailure(
                        code=failure.code,
                        message="Operation task could not complete",
                        retryable=failure.retryable,
                        details=failure.details(),
                    )
                    if target is TaskState.FAILED
                    else None,
                )
                return target.value
            return self._publish(conn, tx, task, content, operation_id, deduplicated=False)

    def complete(
        self, task_id: str, content: bytes, operation_id: str = EXECUTE, *, deduplicated: bool
    ) -> str:
        with self.kernel.database.engine.begin() as conn, borrow(conn) as tx:
            task = tx.get_task(task_id)
            if task.state in TERMINAL:
                return task.state.value
            return self._publish(conn, tx, task, content, operation_id, deduplicated=deduplicated)

    def _publish(
        self,
        conn: sa.Connection,
        tx: KernelTransaction,
        task: Task,
        content: bytes,
        operation_id: str,
        *,
        deduplicated: bool,
    ) -> str:
        assert task.lease is not None
        request = OperationTaskRequest.model_validate_json(
            Policy(conn).binding(task.task_id)["request_json"]
        )
        receipt = tx.get_operation(task.task_id, operation_id)
        stored = tx.put_object(
            ResourceRef(namespace=request.namespace, resource_id=f"{task.task_id}.output"),
            content,
            expected_version=0,
            producer_id=task.agent_id,
            task_id=task.task_id,
            lease_id=task.lease.lease_id,
            at=now(),
        )
        evidence = tx.put_object(
            ResourceRef(namespace=request.namespace, resource_id=f"{task.task_id}.verification"),
            rfc8785.dumps(
                {
                    "passed": True,
                    "check": "output_schema",
                    "deduplicated": deduplicated,
                    "scope_key": receipt.request.arguments["scope_key"],
                    "output_digest": stored.content_digest,
                    "descriptor_sha256": receipt.request.arguments["descriptor_sha256"],
                    "arguments_sha256": hashlib.sha256(
                        rfc8785.dumps(request.arguments)
                    ).hexdigest(),
                }
            ),
            expected_version=0,
            producer_id=task.verifier_id,
            task_id=task.task_id,
            lease_id=task.lease.lease_id,
            at=now(),
        )
        at = now()
        tx.change_task(
            task.task_id,
            TaskState.SUCCEEDED,
            expected_revision=task.revision,
            lease_id=task.lease.lease_id,
            at=at,
            verification=VerificationRecord(
                verification_id=f"verification-{task.task_id}",
                task_id=task.task_id,
                verifier_id=task.verifier_id,
                checked_at=at,
                passed=True,
                artifacts=(stored,),
                evidence=(
                    ResourceRef(namespace=evidence.namespace, resource_id=evidence.resource_id),
                ),
            ),
        )
        self.kernel.fault("operation.settle_before_commit")
        return "succeeded"

    def reconcile(self, task_id: str, operation_id: str) -> str:
        """Settle an uncertain effect from observed destination state; never re-dispatch."""
        with self.kernel.database.engine.begin() as conn, borrow(conn) as tx:
            receipt = tx.get_operation(task_id, operation_id)
            if receipt.state is not OperationState.OUTCOME_UNKNOWN:
                raise Fault("not_uncertain", "Only uncertain operations can be reconciled", 409)
            name = receipt.request.name
            observed: Reconciliation | None = None
            if name == mailbox.SEND:
                # A built-in send commits with its receipt; its table is the destination.
                observed = mailbox.reconcile_send(conn, receipt)
            reconciler = None if name == mailbox.SEND else self.kernel.reconciler(name)
            if reconciler is None and name != mailbox.SEND:
                raise Fault("reconciler_missing", "Operation declares no reconciler", 409)
            recorded = receipt.request.arguments
            arguments = recorded["arguments"]
            assert isinstance(arguments, dict)
            scope_key = recorded.get("scope_key")
            caller = recorded.get("context")
            provenance = caller if isinstance(caller, dict) else {}
            context = OperationContext(
                task_id=task_id,
                namespace=receipt.request.resource.namespace,
                receipt_id=receipt.receipt_id,
                deadline=receipt.request.deadline,
                effect_key=str(scope_key) if scope_key else receipt.receipt_id,
                **{
                    k: str(v)
                    for k, v in provenance.items()
                    if k in CONTEXT_FIELDS and v is not None
                },
            )
            timeout = float(self.kernel.operations.get(name).cardea.timeout_seconds)
        if reconciler is not None:
            observed = asyncio.run(asyncio.wait_for(reconciler(dict(arguments), context), timeout))
        assert observed is not None
        with self.kernel.database.engine.begin() as conn, borrow(conn) as tx:
            receipt = tx.get_operation(task_id, operation_id)
            if receipt.state is not OperationState.OUTCOME_UNKNOWN:
                return receipt.state.value
            output: JsonValue = None
            if observed.applied:
                checked = self.kernel.operations.check_output(name, observed.output)
                if checked is None:
                    raise Fault("verification_failed", "Reconciled output is invalid", 409)
                output = json.loads(checked)
            evidence = tx.put_object(
                ResourceRef(
                    namespace=receipt.request.resource.namespace,
                    resource_id=f"{task_id}.{operation_id}.reconciliation",
                ),
                rfc8785.dumps(
                    {
                        "applied": observed.applied,
                        "output": output,
                        "observed_by": self.kernel.operations.get(name).cardea.reconciler,
                    }
                ),
                expected_version=0,
                producer_id=self.kernel.runtime_id,
                at=now(),
            )
            settled = tx.reconcile_operation(
                task_id,
                operation_id,
                OperationState.SUCCEEDED if observed.applied else OperationState.FAILED,
                expected_revision=receipt.revision,
                at=now(),
                evidence=(
                    ResourceRef(namespace=evidence.namespace, resource_id=evidence.resource_id),
                ),
                result={"output": output} if observed.applied else None,
                failure=None
                if observed.applied
                else KernelFailure(
                    code="effect_not_applied", message="Destination shows no effect"
                ),
            )
            conn.exec_driver_sql(
                "UPDATE effect_scopes SET state=?,updated_at=? WHERE task_id=? AND operation_id=?",
                (
                    "succeeded" if observed.applied else "failed",
                    now().isoformat(),
                    task_id,
                    operation_id,
                ),
            )
            return settled.state.value

    def reconcile_pending(self, limit: int = 16) -> int:
        """Try every uncertain operation whose descriptor names a reconciler."""
        with self.kernel.database.read() as conn:
            rows = conn.exec_driver_sql(
                "SELECT task_id,operation_id,json_extract(snapshot,'$.request.name') "
                "FROM operations WHERE state='outcome_unknown' LIMIT ?",
                (limit * 4,),
            ).fetchall()
        settled = 0
        for task_id, operation_id, name in rows:
            if settled >= limit or name not in self.kernel.operations:
                continue
            if self.kernel.operations.get(str(name)).cardea.reconciler is None:
                continue
            try:
                self.reconcile(str(task_id), str(operation_id))
                settled += 1
            except Exception:
                log.warning("reconciliation deferred", exc_info=True, extra={"task_id": task_id})
        return settled
