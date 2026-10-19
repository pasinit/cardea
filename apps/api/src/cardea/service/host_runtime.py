"""One installed host effect: durable intent, live ceiling, bounded stop, checked output."""

import base64
import hashlib
import json
import logging
import time
from contextlib import suppress
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import rfc8785
import sqlalchemy as sa

from cardea.hosts import launcher
from cardea.kernel.models import (
    DispatchLease,
    KernelFailure,
    OperationRequest,
    OperationState,
    ResourceRef,
    ResourceUsage,
    RetryClass,
    Task,
    TaskState,
    VerificationRecord,
)
from cardea.kernel.store import KernelTransaction, StoreError
from cardea.service import host_budget, host_operations
from cardea.service.database import Fault, borrow
from cardea.service.kernel import TERMINAL, Kernel
from cardea.service.models import BaseHostTaskRequest, HostObjectTaskRequest, now
from cardea.service.policy import Policy

log = logging.getLogger(__name__)


class HostRuntime:
    def __init__(self, kernel: Kernel) -> None:
        self.kernel = kernel

    def guard(self, conn: sa.Connection, task: Task) -> BaseHostTaskRequest:
        runtime = self.kernel.auth.runtime_proof().resolve(conn)
        policy = Policy(conn)
        binding = policy.binding(task.task_id)
        data = json.loads(binding["request_json"])
        request = (
            HostObjectTaskRequest.model_validate(data)
            if "input_object" in data
            else BaseHostTaskRequest.model_validate(data)
        )
        policy.ceiling(task.task_id, request.operation)
        for right in ("objects.read", "objects.write"):
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
        from cardea.service.delegation import child_dispatch

        child_dispatch(conn, task.task_id)
        host_budget.available(conn, str(binding["supervisor_id"]), request.namespace, reserve=False)
        if isinstance(request, HostObjectTaskRequest):
            policy.object_read(str(binding["supervisor_id"]), request.input_object)
        return request

    @staticmethod
    def source(tx: KernelTransaction, request: BaseHostTaskRequest) -> bytes:
        try:
            content = (
                tx.read_object(request.input_object)
                if isinstance(request, HostObjectTaskRequest)
                else rfc8785.dumps(request.payload)
            )
        except StoreError as error:
            raise Fault(error.code, "Stored input is unavailable or corrupt", 409) from error
        if len(content) > 16384:
            raise Fault("input_too_large", "Host input exceeds 16384 bytes", 413)
        return content

    def execute(self, task_id: str) -> str:
        try:
            return self.effect(task_id)
        finally:
            try:
                self.cleanup()
            except Exception:
                log.exception("host cleanup failed", extra={"task_id": task_id})
            try:
                with self.kernel.database.engine.begin() as conn:
                    from cardea.service.kernel import workflow_id

                    binding = Policy(conn).binding(task_id)
                    if binding["parent_task_id"]:
                        self.kernel.client.send_in_transaction(
                            conn,
                            workflow_id(binding["parent_task_id"]),
                            task_id,
                            topic="control",
                            idempotency_key=f"child-finished:{task_id}",
                        )
            except Exception:
                log.exception("parent notification failed", extra={"task_id": task_id})

    def cleanup(self) -> None:
        if self.kernel.host is None:
            return
        with self.kernel.database.engine.begin() as conn:
            pending = conn.exec_driver_sql(
                "SELECT r.task_id FROM host_reservations r JOIN tasks t USING(task_id) "
                "WHERE r.cleaned=0 AND t.state IN ('succeeded','failed','cancelled') LIMIT 32"
            ).fetchall()
        for (identity,) in pending:
            try:
                job = str(identity).removeprefix("task-")
                if not self.kernel.host.stop(job).get("stopped"):
                    continue
                self.kernel.fault("host.cleanup_before_delete")
                if not self.kernel.host.cleanup(job).get("cleaned"):
                    continue
                self.kernel.fault("host.cleanup_after_delete")
                with self.kernel.database.engine.begin() as conn, borrow(conn) as tx:
                    changed = conn.exec_driver_sql(
                        "UPDATE host_reservations SET cleaned=1,settled=1 "
                        "WHERE task_id=? AND cleaned=0",
                        (identity,),
                    ).rowcount
                    if changed:
                        tx.append_event(
                            namespace="kernel.host",
                            event_type="host.cleaned",
                            producer_id=self.kernel.runtime_id,
                            correlation_id=identity,
                            task_id=identity,
                            at=now(),
                        )
            except Exception:
                # Durable pending rows are retried by maintenance, never reported as cleaned.
                log.warning("host cleanup deferred", exc_info=True, extra={"task_id": identity})
                continue

    def effect(self, task_id: str) -> str:
        host = self.kernel.host
        if host is None:
            raise Fault("unsupported_host", "No host configured", 503)
        job = task_id.removeprefix("task-")
        # Probe outside the write lock: it is a privileged subprocess call.
        probed: Fault | None = None
        try:
            host.probe()
        except Fault as error:
            probed = error
        # An existing running task is an interrupted intent, never permission to replay.
        with self.kernel.database.engine.begin() as conn, borrow(conn) as tx:
            task = tx.get_task(task_id)
            if task.state in TERMINAL:
                return task.state.value
            recovering = task.state is TaskState.RUNNING
            if not recovering:
                try:
                    request = self.guard(conn, task)
                    self.source(tx, request)
                    if probed is not None:
                        raise probed
                    binding = Policy(conn).binding(task_id)
                    host_budget.reserve(
                        conn, task_id, str(binding["supervisor_id"]), request.namespace
                    )
                except Fault as error:
                    tx.change_task(
                        task_id,
                        TaskState.FAILED,
                        expected_revision=task.revision,
                        at=now(),
                        failure=KernelFailure(code=error.code, message=error.message),
                    )
                    return "failed"
                at = now()
                task = tx.change_task(
                    task_id,
                    TaskState.RUNNING,
                    expected_revision=task.revision,
                    at=at,
                    lease=DispatchLease(
                        lease_id="lease-" + uuid4().hex,
                        worker_id=self.kernel.runtime_id,
                        acquired_at=at,
                        expires_at=min(task.limits.deadline, at + timedelta(seconds=40)),
                    ),
                )
                assert task.lease is not None
                # One intent reserves the task's entire installed-operation allocation.
                receipt = tx.admit_operation(
                    OperationRequest(
                        operation_id="execute",
                        task_id=task_id,
                        subject_id=task.agent_id,
                        name=request.operation,
                        resource=ResourceRef(namespace=request.namespace, resource_id=task_id),
                        arguments={
                            "payload": request.payload,
                            **(
                                {"input_object": request.input_object.model_dump(mode="json")}
                                if isinstance(request, HostObjectTaskRequest)
                                else {}
                            ),
                            "reserved_tool_milliseconds": 10000,
                            "reserved_output_bytes": 65536,
                            "host_profile": launcher.PROFILE,
                            "launcher_sha256": hashlib.sha256(
                                Path(launcher.__file__).read_bytes()
                            ).hexdigest(),
                            "policy_revision": conn.exec_driver_sql(
                                "SELECT value FROM service_meta WHERE name='policy_revision'"
                            ).scalar_one(),
                        },
                        deadline=task.limits.deadline,
                    ),
                    grant_ids=task.capability_ids,
                    retry_class=RetryClass.RECONCILIATION_REQUIRED,
                    expected_task_revision=task.revision,
                    lease_id=task.lease.lease_id,
                    at=now(),
                )
                tx.change_operation(
                    task_id,
                    "execute",
                    OperationState.RUNNING,
                    expected_revision=receipt.revision,
                    lease_id=task.lease.lease_id,
                    at=now(),
                )
        self.kernel.fault("host.intent_committed")
        if recovering:
            return self.uncertain(task_id, job)
        try:
            # Serialize launch against policy mutation. Intent is already durable.
            with self.kernel.database.engine.begin() as conn, borrow(conn) as tx:
                request = self.guard(conn, tx.get_task(task_id))
                self.kernel.fault("host.before_start")
                binding = Policy(conn).binding(task_id)
                expiry = conn.exec_driver_sql(
                    "SELECT MIN(expires_at) FROM policies WHERE namespace=? "
                    "AND operation IN (?,'objects.read','objects.write') "
                    "AND ((principal_id=? AND mode IN ('allow','delegate')) "
                    "OR (principal_id='platform' AND mode='allow'))",
                    (request.namespace, request.operation, binding["supervisor_id"]),
                ).scalar_one()
                deadline = min(float(expiry), tx.get_task(task_id).limits.deadline.timestamp())
                if isinstance(request, HostObjectTaskRequest):
                    deadline = min(
                        deadline,
                        Policy(conn).object_read(
                            str(binding["supervisor_id"]), request.input_object
                        ),
                    )
                if request.operation == "host.sha256":
                    host.start(job, self.source(tx, request), deadline)
                else:
                    host.start(job, self.source(tx, request), deadline, request.operation)
            self.kernel.fault("host.started")
            while True:
                try:
                    with self.kernel.database.engine.begin() as conn, borrow(conn) as tx:
                        self.guard(conn, tx.get_task(task_id))
                    observed = host.status(job)
                except Fault as error:
                    # Identity/host outages are not authority decisions: keep the bounded
                    # job and re-poll while the lease lasts. Settlement re-runs the guard.
                    if error.status != 503:
                        raise
                    if not self.lease_live(task_id):
                        return self.uncertain(task_id, job)
                    log.warning(
                        "transient fault while monitoring host job",
                        extra={"task_id": task_id, "code": error.code},
                    )
                    time.sleep(0.1)
                    continue
                if observed["state"] == "finished":
                    stopped = host.stop(job)
                    if not stopped.get("stopped"):
                        raise Fault("stop_unconfirmed", "Host stop was not confirmed", 503)
                    self.kernel.fault("host.before_settle")
                    return self.settle(task_id, observed)
                if observed["state"] != "running":
                    return self.uncertain(task_id, job)
                time.sleep(0.1)
        except Fault as error:
            if error.code == "host_rejected":
                log.warning(
                    "host rejected job before start",
                    extra={"task_id": task_id, "reason": error.message},
                )
                return self.settle(task_id, None, error.code, started=False)
            if error.code in {"host_unavailable", "stop_unconfirmed"}:
                return self.uncertain(task_id, job)
            try:
                stopped = host.stop(job)
                if not stopped.get("stopped"):
                    return self.uncertain(task_id, job)
                return self.settle(task_id, None, error.code)
            except (Fault, StoreError):
                return self.uncertain(task_id, job)

        except Exception:
            log.exception("unexpected host dispatch failure", extra={"task_id": task_id})
            return self.uncertain(task_id, job)

    def lease_live(self, task_id: str) -> bool:
        with self.kernel.database.engine.begin() as conn, borrow(conn) as tx:
            lease = tx.get_task(task_id).lease
        return lease is not None and now() < lease.expires_at

    def uncertain(self, task_id: str, job: str) -> str:
        assert self.kernel.host is not None
        # The independent host deadline remains active; never infer a result.
        with suppress(Fault):
            self.kernel.host.stop(job)
        while True:
            with self.kernel.database.engine.begin() as conn, borrow(conn) as tx:
                task = tx.get_task(task_id)
                if task.state in TERMINAL:
                    return task.state.value
                assert task.lease is not None
                if now() >= task.lease.expires_at:
                    host_budget.settle(conn, task_id, 10000, 65536)
                    return tx.recover_task(
                        task_id, expected_revision=task.revision, at=now()
                    ).state.value
                receipt = tx.get_operation(task_id, "execute")
                if receipt.state is OperationState.RUNNING:
                    tx.change_operation(
                        task_id,
                        "execute",
                        OperationState.OUTCOME_UNKNOWN,
                        expected_revision=receipt.revision,
                        lease_id=task.lease.lease_id,
                        at=now(),
                        failure=KernelFailure(
                            code="host_outcome_unknown",
                            message="Interrupted host intent; no replay",
                        ),
                        usage=ResourceUsage(steps=1, tool_milliseconds=10000),
                    )
            time.sleep(0.1)

    def settle(
        self,
        task_id: str,
        observed: dict[str, Any] | None,
        error: str = "",
        *,
        started: bool = True,
    ) -> str:
        with self.kernel.database.engine.begin() as conn, borrow(conn) as tx:
            task = tx.get_task(task_id)
            if task.state in TERMINAL:
                return task.state.value
            assert task.lease is not None
            if now() >= task.lease.expires_at:
                host_budget.settle(conn, task_id, 10000, 65536)
                return tx.recover_task(
                    task_id, expected_revision=task.revision, at=now()
                ).state.value
            request = None
            try:
                request = self.guard(conn, task)
                source = self.source(tx, request)
            except Fault as denied:
                error = denied.code
            content = b""
            expected: dict[str, Any] = {}
            if not error and observed is not None and request is not None:
                expected = host_operations.expected(request.operation, source)
                try:
                    content = base64.b64decode(observed["output"], validate=True)
                    actual = json.loads(content)
                    if (
                        observed["result"] != "success"
                        or observed["exit_status"] != "0"
                        or len(content) > 65536
                        or rfc8785.dumps(actual) != rfc8785.dumps(expected)
                    ):
                        error = "verification_failed"
                except (ValueError, KeyError, TypeError):
                    error = "verification_failed"
            elif not error:
                error = "host_failed"
            receipt = tx.get_operation(task_id, "execute")
            wall = observed.get("wall_ms") if observed else None
            measured = type(wall) is int and 0 <= wall <= 10000
            tool_ms = wall if isinstance(wall, int) and measured else 10000
            output_bytes = len(content) if measured and content else 65536
            if not started:
                tool_ms = output_bytes = 0
            host_budget.settle(conn, task_id, tool_ms, output_bytes)
            tx.change_operation(
                task_id,
                "execute",
                OperationState.CANCELLED
                if error == "cancel_requested"
                else OperationState.FAILED
                if error
                else OperationState.SUCCEEDED,
                expected_revision=receipt.revision,
                lease_id=task.lease.lease_id,
                at=now(),
                failure=KernelFailure(
                    code=error,
                    message="Host execution did not produce an authorized verified result",
                )
                if error and error != "cancel_requested"
                else None,
                result=None if error else {"host": observed or {}, "verified_output": expected},
                usage=ResourceUsage(steps=1, tool_milliseconds=tool_ms, output_bytes=output_bytes),
            )
            if error:
                target = TaskState.CANCELLED if error == "cancel_requested" else TaskState.FAILED
                tx.change_task(
                    task_id,
                    target,
                    expected_revision=task.revision,
                    lease_id=task.lease.lease_id,
                    at=now(),
                    failure=KernelFailure(code=error, message="Host task could not complete")
                    if target is TaskState.FAILED
                    else None,
                )
                return target.value
            assert request is not None
            output = tx.put_object(
                ResourceRef(namespace=request.namespace, resource_id=f"{task_id}.output"),
                rfc8785.dumps(expected),
                expected_version=0,
                producer_id=task.agent_id,
                task_id=task_id,
                lease_id=task.lease.lease_id,
                at=now(),
            )
            evidence = tx.put_object(
                ResourceRef(namespace=request.namespace, resource_id=f"{task_id}.verification"),
                rfc8785.dumps(
                    {
                        "passed": True,
                        "output_digest": output.content_digest,
                        "input_digest": hashlib.sha256(source).hexdigest(),
                    }
                ),
                expected_version=0,
                producer_id=task.verifier_id,
                task_id=task_id,
                lease_id=task.lease.lease_id,
                at=now(),
            )
            at = now()
            tx.change_task(
                task_id,
                TaskState.SUCCEEDED,
                expected_revision=task.revision,
                lease_id=task.lease.lease_id,
                at=at,
                verification=VerificationRecord(
                    verification_id=f"verification-{task_id}",
                    task_id=task_id,
                    verifier_id=task.verifier_id,
                    checked_at=at,
                    passed=True,
                    artifacts=(output,),
                    evidence=(
                        ResourceRef(namespace=evidence.namespace, resource_id=evidence.resource_id),
                    ),
                ),
            )
            self.kernel.fault("host.settle_before_commit")
            return "succeeded"
