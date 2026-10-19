"""One installed operation as a task: durable intent, live ceiling, checked output.

See ADR-023 (registry) and ADR-026 (idempotency scopes and reconciliation).
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
    DispatchLease,
    KernelFailure,
    OperationState,
    ResourceRef,
    Task,
    TaskState,
    VerificationRecord,
)
from cardea.kernel.store import KernelTransaction
from cardea.service import invocation
from cardea.service.confined import WorkerFailed
from cardea.service.database import Fault, borrow
from cardea.service.kernel import TERMINAL, Kernel
from cardea.service.models import OperationTaskRequest, now
from cardea.service.operations import (
    OperationContext,
    OperationDescriptor,
    OperationRejected,
)
from cardea.service.policy import Policy

log = logging.getLogger(__name__)
# Lease time beyond the handler timeout for settlement; settlement after expiry is fenced.
LEASE_MARGIN = 5.0
SETTLE_MARGIN = 1.0
EXECUTE = "execute"


class _Refused(Exception):
    def __init__(self, fault: Fault) -> None:
        self.fault = fault


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
            request.operation,
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

    def execute(self, task_id: str) -> str:
        """Workflow step entry point: a refusal before dispatch fails the task."""
        try:
            return self._execute(task_id)
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

    def _execute(self, task_id: str) -> str:
        # An existing running task is an interrupted intent, never permission to replay.
        with self.kernel.database.engine.begin() as conn, borrow(conn) as tx:
            task = tx.get_task(task_id)
            if task.state in TERMINAL:
                return task.state.value
            recovering = task.state is TaskState.RUNNING
            if not recovering:
                try:
                    request, descriptor = self.guard(conn, task)
                    handler = self.kernel.handler(request.operation)
                    executor = self.kernel.executor_record(request.operation)
                    at = now()
                    expires = min(
                        task.limits.deadline,
                        at + timedelta(seconds=descriptor.cardea.timeout_seconds + LEASE_MARGIN),
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
                    admitted = invocation.admit(
                        conn,
                        tx,
                        self.kernel.operations,
                        task,
                        lease_id=task.lease.lease_id,
                        operation_id=EXECUTE,
                        name=request.operation,
                        namespace=request.namespace,
                        arguments=request.arguments,
                        scope_context=request.idempotency_context or task_id,
                        deadline=task.limits.deadline,
                        executor=executor,
                    )
                except Fault as error:
                    # Raising rolls back the lease and intent: nothing was dispatched.
                    raise _Refused(error) from None
                timeout = invocation.timeout_for(
                    self.kernel.operations, request.operation, expires, SETTLE_MARGIN
                )
        self.kernel.fault("operation.intent_committed")
        if recovering:
            # A confined worker may outlive the service: stop its whole process tree.
            self.kernel.reap_workers(task_id)
            return self.uncertain(task_id)
        if admitted.deduplicated is not None:
            return self.complete(task_id, rfc8785.dumps(admitted.deduplicated), deduplicated=True)
        effect = descriptor.cardea.effect == "effect"
        started = time.monotonic()
        try:
            if timeout <= 0:
                raise OperationRejected("no time left to dispatch")
            output = asyncio.run(
                asyncio.wait_for(handler(dict(request.arguments), admitted.context), timeout)
            )
        except OperationRejected:
            return self.settle(task_id, None, "operation_rejected", started)
        except WorkerFailed as failed:
            # The confined worker started: an effect may have happened (ADR-028).
            log.warning("confined worker failed: %s", failed.code, extra={"task_id": task_id})
            if effect:
                return self.uncertain(task_id)
            return self.settle(task_id, None, failed.code, started)
        except Exception:
            log.exception("installed operation failed", extra={"task_id": task_id})
            if effect:
                return self.uncertain(task_id)
            return self.settle(task_id, None, "operation_failed", started)
        self.kernel.fault("operation.before_settle")
        return self.settle(task_id, output, "", started)

    def uncertain(self, task_id: str) -> str:
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
                    conn, tx, task, lease_id=task.lease.lease_id, operation_id=EXECUTE
                )
            time.sleep(0.1)

    def settle(self, task_id: str, output: object, error: str, started: float) -> str:
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
                error = error or denied.code
            error, content = invocation.settle(
                conn,
                tx,
                self.kernel.operations,
                task,
                lease_id=task.lease.lease_id,
                operation_id=EXECUTE,
                output=output,
                error=error,
                elapsed_ms=elapsed,
            )
            if error:
                target = TaskState.CANCELLED if error == "cancel_requested" else TaskState.FAILED
                tx.change_task(
                    task_id,
                    target,
                    expected_revision=task.revision,
                    lease_id=task.lease.lease_id,
                    at=now(),
                    failure=KernelFailure(code=error, message="Operation task could not complete")
                    if target is TaskState.FAILED
                    else None,
                )
                return target.value
            return self._publish(conn, tx, task, content, deduplicated=False)

    def complete(self, task_id: str, content: bytes, *, deduplicated: bool) -> str:
        with self.kernel.database.engine.begin() as conn, borrow(conn) as tx:
            task = tx.get_task(task_id)
            if task.state in TERMINAL:
                return task.state.value
            return self._publish(conn, tx, task, content, deduplicated=deduplicated)

    def _publish(
        self,
        conn: sa.Connection,
        tx: KernelTransaction,
        task: Task,
        content: bytes,
        *,
        deduplicated: bool,
    ) -> str:
        assert task.lease is not None
        request = OperationTaskRequest.model_validate_json(
            Policy(conn).binding(task.task_id)["request_json"]
        )
        receipt = tx.get_operation(task.task_id, EXECUTE)
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
            reconciler = self.kernel.operations.reconciler(name)
            if reconciler is None:
                raise Fault("reconciler_missing", "Operation declares no reconciler", 409)
            recorded = receipt.request.arguments
            arguments = recorded["arguments"]
            assert isinstance(arguments, dict)
            scope_key = recorded.get("scope_key")
            context = OperationContext(
                task_id=task_id,
                namespace=receipt.request.resource.namespace,
                receipt_id=receipt.receipt_id,
                deadline=receipt.request.deadline,
                effect_key=str(scope_key) if scope_key else receipt.receipt_id,
            )
            timeout = float(self.kernel.operations.get(name).cardea.timeout_seconds)
        observed = asyncio.run(asyncio.wait_for(reconciler(dict(arguments), context), timeout))
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
