"""DBOS owns dispatch/history; trusted deterministic effects stay in datasource commits."""

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import rfc8785
import sqlalchemy as sa
from dbos import DBOS, SQLAlchemyDatasource
from sqlalchemy.orm import Session

from cardea.kernel.models import (
    Checkpoint,
    DispatchLease,
    EventWait,
    KernelFailure,
    OperationRequest,
    OperationState,
    ResourceRef,
    RetryClass,
    Task,
    TaskState,
    VerificationRecord,
)
from cardea.kernel.store import KernelTransaction
from cardea.service.agent_run_runtime import AgentRunRuntime
from cardea.service.agent_runtime import AgentRuntime
from cardea.service.database import Fault, borrow
from cardea.service.delegation_runtime import DelegationRuntime
from cardea.service.host_runtime import HostRuntime
from cardea.service.kernel import (
    AGENT_RUN_QUEUE,
    AGENT_RUN_WORKFLOW,
    AGENT_WORKFLOW,
    DELEGATION_QUEUE,
    DELEGATION_WORKFLOW,
    HOST_WORKFLOW,
    OPERATION_WORKFLOW,
    QUEUE,
    TERMINAL,
    WORKFLOW,
    Kernel,
)
from cardea.service.models import TaskRequest, now
from cardea.service.operation_runtime import OperationRuntime
from cardea.service.policy import Policy
from cardea.service.triggers import Scheduler

TRIGGER_WORKFLOW = "cardea.m6.triggers"
TRIGGER_SCHEDULE = "cardea-triggers"

log = logging.getLogger(__name__)


class Runtime:
    def __init__(self, kernel: Kernel) -> None:
        self.kernel = kernel
        self.datasource = SQLAlchemyDatasource.create(
            kernel.database.url, engine=kernel.database.engine
        )
        ds = self.datasource

        @sa.event.listens_for(Session, "before_commit")
        def before_commit(session: Session) -> None:
            if "cardea_phase" in session.info:
                kernel.fault(session.info["cardea_phase"] + ".before_commit")

        @sa.event.listens_for(Session, "after_commit")
        def after_commit(session: Session) -> None:
            if "cardea_phase" in session.info:
                kernel.fault(session.info["cardea_phase"] + ".after_commit")

        @ds.transaction()
        def publish(task_id: str) -> bool:
            return self.publish(ds.sql_session(), task_id)

        @ds.transaction()
        def wake(task_id: str) -> str:
            return self.wake(ds.sql_session(), task_id)

        @ds.transaction()
        def finish(task_id: str) -> str:
            return self.finish(ds.sql_session(), task_id)

        @DBOS.workflow(name=WORKFLOW)
        def execute(task_id: str, wait: bool, deadline: float) -> str:
            try:
                kernel.fault("workflow.before_publish")
                if not publish(task_id):
                    return self.state(task_id)
                if wait:
                    while True:
                        DBOS.recv(topic="control", timeout_seconds=max(0.0, deadline - time.time()))
                        if wake(task_id) != "waiting":
                            break
                return finish(task_id)
            except Exception:
                log.exception("deterministic runtime failed", extra={"task_id": task_id})
                # Every installed effect is local/transactional. No remote outcome is inferred.
                with kernel.database.engine.begin() as conn, borrow(conn) as tx:
                    task = tx.get_task(task_id)
                    self.fail(tx, task, "runtime_failed")
                return self.state(task_id)

        @DBOS.step(name="cardea.m2.host.effect", retries_allowed=False)
        def host_effect(task_id: str) -> str:
            return HostRuntime(kernel).execute(task_id)

        @DBOS.workflow(name=HOST_WORKFLOW)
        def execute_host(task_id: str, wait: bool, deadline: float) -> str:
            return host_effect(task_id)

        @DBOS.step(name="cardea.m3.agent.round", retries_allowed=False)
        def agent_round(task_id: str) -> str:
            return asyncio.run(AgentRuntime(kernel).round(task_id))

        @DBOS.step(name="cardea.m3.agent.wake", retries_allowed=False)
        def agent_wake(task_id: str) -> str:
            return AgentRuntime(kernel).wake(task_id)

        @DBOS.workflow(name=AGENT_WORKFLOW)
        def execute_agent(task_id: str, wait: bool, deadline: float) -> str:
            state = agent_round(task_id)
            while state == "waiting":
                DBOS.recv(topic="control", timeout_seconds=max(0.0, deadline - time.time()))
                state = agent_wake(task_id)
                if state == "queued":
                    state = agent_round(task_id)
            return state

        @DBOS.step(name="cardea.m4.delegation.tick", retries_allowed=False)
        def delegation_tick(task_id: str) -> str:
            return asyncio.run(DelegationRuntime(kernel).tick(task_id))

        @DBOS.workflow(name=DELEGATION_WORKFLOW)
        def execute_delegation(task_id: str, wait: bool, deadline: float) -> str:
            while True:
                state = delegation_tick(task_id)
                if state != "waiting":
                    return state
                DBOS.recv(topic="control", timeout_seconds=2)

        @DBOS.step(name="cardea.m6.operation.effect", retries_allowed=False)
        def operation_effect(task_id: str) -> str:
            return OperationRuntime(kernel).execute(task_id)

        @DBOS.workflow(name=OPERATION_WORKFLOW)
        def execute_operation(task_id: str, wait: bool, deadline: float) -> str:
            return operation_effect(task_id)

        @DBOS.step(name="cardea.m6.triggers.tick", retries_allowed=False)
        def trigger_tick() -> dict[str, str]:
            return Scheduler(kernel).tick()

        # DBOS owns the clock (ADR-024): one schedule, no second scheduler or queue.
        # Slot identity, misfire and timezone semantics live in `triggers`/`schedule`.
        @DBOS.workflow(name=TRIGGER_WORKFLOW)
        def tick_triggers(scheduled_at: datetime, context: Any) -> None:
            trigger_tick()

        self.tick_triggers = tick_triggers

        @DBOS.step(name="cardea.m6.agent_run.attempts", retries_allowed=False)
        def agent_run_attempts(task_id: str) -> str:
            return AgentRunRuntime(kernel).execute(task_id)

        @DBOS.workflow(name=AGENT_RUN_WORKFLOW)
        def execute_agent_run(task_id: str, wait: bool, deadline: float) -> str:
            return agent_run_attempts(task_id)

        self.execute_agent_run = execute_agent_run
        self.execute_operation = execute_operation
        self.execute_delegation = execute_delegation
        self.execute_agent = execute_agent
        self.execute_host = execute_host
        self.execute = execute

    def launch(self) -> None:
        DBOS.launch()
        # Idempotent replace of the one clock schedule; it fires at second 0 of each minute
        # in UTC. Missed ticks are not backfilled: each tick evaluates every slot since
        # the trigger's last decided slot, under that trigger's misfire policy.
        DBOS.apply_schedules(
            [
                {
                    "schedule_name": TRIGGER_SCHEDULE,
                    "workflow_fn": self.tick_triggers,
                    "schedule": "0 * * * * *",
                }
            ]
        )
        self.kernel.client.register_queue(QUEUE, worker_concurrency=4, polling_interval_sec=0.05)
        self.kernel.client.register_queue(
            DELEGATION_QUEUE, worker_concurrency=2, polling_interval_sec=0.05
        )
        sessions = self.kernel.session_limit
        self.kernel.client.register_queue(
            AGENT_RUN_QUEUE, worker_concurrency=sessions, polling_interval_sec=0.5
        )

    def state(self, task_id: str) -> str:
        with self.kernel.database.engine.begin() as conn, borrow(conn) as tx:
            return tx.get_task(task_id).state.value

    def fail(self, tx: KernelTransaction, task: Task, code: str) -> None:
        if task.state in TERMINAL:
            return
        at = now()
        if task.lease and at >= task.lease.expires_at:
            task = tx.recover_task(task.task_id, expected_revision=task.revision, at=at)
            if task.state in TERMINAL:
                return
        tx.change_task(
            task.task_id,
            TaskState.FAILED,
            expected_revision=task.revision,
            lease_id=task.lease.lease_id if task.lease else None,
            at=at,
            failure=KernelFailure(
                code=code, message="Task could not continue under current constraints"
            ),
        )

    def guard(self, session: Session, tx: KernelTransaction, task: Task) -> bool:
        if task.state in TERMINAL:
            return False
        try:
            conn = session.connection()
            runtime = self.kernel.auth.runtime_proof().resolve(conn)
            policy = Policy(conn)
            binding = policy.ceiling(task.task_id, "objects.write")
            policy.ceiling(task.task_id, "objects.read")
            if runtime != binding["runtime_id"] or runtime != self.kernel.runtime_id:
                raise Fault("runtime_binding", "Runtime binding changed")
            if now() >= task.limits.deadline:
                raise Fault("deadline_exceeded", "Execution deadline elapsed")
        except Fault as error:
            self.fail(tx, task, error.code)
            return False
        return True

    def claim(self, tx: KernelTransaction, task: Task) -> Task:
        if task.state is TaskState.SUSPENDED:
            task = tx.change_task(
                task.task_id, TaskState.QUEUED, expected_revision=task.revision, at=now()
            )
        at = now()
        lease = DispatchLease(
            lease_id=f"lease-{uuid4().hex}",
            worker_id=self.kernel.runtime_id,
            acquired_at=at,
            expires_at=min(at + timedelta(seconds=10), task.limits.deadline),
        )
        return tx.change_task(
            task.task_id, TaskState.RUNNING, expected_revision=task.revision, at=at, lease=lease
        )

    def publish(self, session: Session, task_id: str) -> bool:
        session.info["cardea_phase"] = "publish"
        with borrow(session) as tx:
            task = tx.get_task(task_id)
            if not self.guard(session, tx, task):
                return False
            binding = Policy(session.connection()).binding(task_id)
            request = TaskRequest.model_validate_json(binding["request_json"])
            task = self.claim(tx, task)
            assert task.lease is not None
            lease = task.lease.lease_id
            content = rfc8785.dumps(request.payload)
            reference = ResourceRef(namespace=request.namespace, resource_id=f"{task_id}.output")
            operation = OperationRequest(
                operation_id="publish",
                task_id=task_id,
                subject_id=task.agent_id,
                name="objects.write",
                resource=reference,
                arguments={"expected_version": 0, "content": request.payload},
                deadline=task.limits.deadline,
            )
            receipt = tx.admit_operation(
                operation,
                grant_ids=task.capability_ids,
                retry_class=RetryClass.IDEMPOTENT,
                expected_task_revision=task.revision,
                lease_id=lease,
                at=now(),
            )
            receipt = tx.change_operation(
                task_id,
                "publish",
                OperationState.RUNNING,
                expected_revision=receipt.revision,
                lease_id=lease,
                at=now(),
            )
            output = tx.put_object(
                reference,
                content,
                expected_version=0,
                producer_id=task.agent_id,
                task_id=task_id,
                lease_id=lease,
                at=now(),
            )
            tx.change_operation(
                task_id,
                "publish",
                OperationState.SUCCEEDED,
                expected_revision=receipt.revision,
                lease_id=lease,
                at=now(),
                result={"artifact": output.model_dump(mode="json")},
            )
            at = now()
            checkpoint = Checkpoint(
                checkpoint_id=f"checkpoint-{task_id}",
                task_id=task_id,
                saved_at=at,
                state={"stage": "published"},
                input_versions=(output,),
                event_cursor=binding["admission_cursor"],
            )
            tx.change_task(
                task_id,
                TaskState.WAITING if request.wait_for_event else TaskState.SUSPENDED,
                expected_revision=task.revision,
                lease_id=lease,
                at=at,
                checkpoint=checkpoint,
                wait=EventWait(
                    namespace=request.namespace,
                    event_type=f"resume.{task_id}",
                    after_cursor=checkpoint.event_cursor,
                )
                if request.wait_for_event
                else None,
            )
            return True

    def wake(self, session: Session, task_id: str) -> str:
        with borrow(session) as tx:
            task = tx.get_task(task_id)
            if not self.guard(session, tx, task):
                return tx.get_task(task_id).state.value
            if task.state is TaskState.WAITING:
                assert task.wait is not None
                conn = session.connection()
                binding = Policy(conn).binding(task_id)
                event = conn.exec_driver_sql(
                    "SELECT 1 FROM events WHERE namespace=? AND event_type=? AND cursor>? LIMIT 1",
                    (binding["namespace"], f"resume.{task_id}", binding["admission_cursor"]),
                ).first()
                if event:
                    task = tx.wake_task(task_id, expected_revision=task.revision, at=now())
            return task.state.value

    def finish(self, session: Session, task_id: str) -> str:
        session.info["cardea_phase"] = "finish"
        with borrow(session) as tx:
            task = tx.get_task(task_id)
            if not self.guard(session, tx, task):
                return tx.get_task(task_id).state.value
            binding = Policy(session.connection()).binding(task_id)
            request = TaskRequest.model_validate_json(binding["request_json"])
            task = self.claim(tx, task)
            assert task.lease is not None and task.checkpoint is not None
            output = tx.latest_object(
                ResourceRef(namespace=request.namespace, resource_id=f"{task_id}.output")
            )
            passed = task.checkpoint.input_versions == (output,) and tx.read_object(
                output
            ) == rfc8785.dumps(request.payload)
            evidence = tx.put_object(
                ResourceRef(namespace=request.namespace, resource_id=f"{task_id}.verification"),
                rfc8785.dumps({"passed": passed, "output_digest": output.content_digest}),
                expected_version=0,
                producer_id=task.verifier_id,
                task_id=task_id,
                lease_id=task.lease.lease_id,
                at=now(),
            )
            if not passed:
                self.fail(tx, task, "verification_failed")
                return "failed"
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
            return "succeeded"
