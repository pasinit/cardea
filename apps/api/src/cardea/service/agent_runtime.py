"""Durable bounded incident agent; DBOS owns scheduling, SQLite owns all effect intents."""

import asyncio
import base64
import json
import logging
import time
from contextlib import suppress
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import rfc8785
import sqlalchemy as sa
from mcp import Client
from mcp.server import MCPServer

from cardea.kernel.models import (
    Checkpoint,
    DispatchLease,
    EventWait,
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
from cardea.runtime.incidents import IncidentArtifacts, IncidentBatch, verify
from cardea.runtime.mcp_tools import MCPTools
from cardea.runtime.openrouter import ModelFailure, Proposal, Usage
from cardea.service import agent_budget, host_budget
from cardea.service.database import Fault, borrow
from cardea.service.kernel import TERMINAL, Kernel
from cardea.service.models import AgentTaskRequest, now
from cardea.service.policy import Policy

log = logging.getLogger(__name__)

LEASE_SECONDS = 90

SCHEMA = json.loads((Path(__file__).parents[1] / "runtime/incident-tool-schema.json").read_text())


class AgentRuntime:
    def __init__(self, kernel: Kernel) -> None:
        self.kernel = kernel

    def guard(self, conn: sa.Connection, task: Task) -> AgentTaskRequest:
        policy = Policy(conn)
        binding = policy.binding(task.task_id)
        for right in (
            "objects.read",
            "objects.write",
            "models.invoke",
            "models.disclose",
            "tools.incidents",
            "events.read",
            "inputs.append",
        ):
            policy.ceiling(task.task_id, right)
        runtime = self.kernel.auth.runtime_proof().resolve(conn)
        if runtime != binding["runtime_id"] or runtime != self.kernel.runtime_id:
            raise Fault("runtime_binding", "Runtime binding changed")
        if task.state in TERMINAL or now() >= task.limits.deadline:
            raise Fault("deadline_or_terminal", "Agent task cannot continue")
        if conn.exec_driver_sql(
            "SELECT 1 FROM control_requests WHERE task_id=? AND kind='cancel'", (task.task_id,)
        ).first():
            raise Fault("cancel_requested", "Cancellation requested")
        host_budget.available(
            conn, str(binding["supervisor_id"]), str(binding["namespace"]), reserve=False
        )
        agent_budget.available(conn, str(binding["supervisor_id"]), str(binding["namespace"]))
        return AgentTaskRequest.model_validate_json(binding["request_json"])

    def check(self, task_id: str) -> None:
        with self.kernel.database.engine.begin() as conn, borrow(conn) as tx:
            self.guard(conn, tx.get_task(task_id))

    async def monitor(self, task_id: str, awaitable: Any) -> Any:
        action = asyncio.create_task(awaitable)
        try:
            while not action.done():
                self.check(task_id)
                await asyncio.wait({action}, timeout=0.1)
            self.check(task_id)
            return await action
        finally:
            if not action.done():
                action.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await action

    def intent(self, task_id: str, kind: str, arguments: dict[str, Any]) -> str | None:
        with self.kernel.database.engine.begin() as conn, borrow(conn) as tx:
            task = tx.get_task(task_id)
            request = self.guard(conn, task)
            binding = Policy(conn).binding(task_id)
            round_id = conn.exec_driver_sql(
                "SELECT round FROM agent_runs WHERE task_id=?", (task_id,)
            ).scalar_one()
            identity = f"{round_id}.{kind}"
            if conn.exec_driver_sql(
                "SELECT 1 FROM agent_effects WHERE task_id=? AND operation_id=?",
                (task_id, identity),
            ).first():
                raise Fault("agent_effect_exists", "An existing intent cannot be dispatched again")
            tokens, job = 0, None
            if kind == "model":
                tokens = agent_budget.INPUT_RESERVE + request.max_output_tokens
                agent_budget.available(
                    conn, str(binding["supervisor_id"]), request.namespace, tokens
                )
            else:
                host_budget.available(
                    conn, str(binding["supervisor_id"]), request.namespace, reserve=True
                )
                job = uuid4().hex
            conn.exec_driver_sql(
                "INSERT INTO agent_effects VALUES (?,?,?,?,?,?,?,?,?,?,0,0)",
                (
                    task_id,
                    identity,
                    binding["supervisor_id"],
                    request.namespace,
                    kind,
                    job,
                    int(kind == "model"),
                    tokens,
                    10000 if job else 0,
                    65536 if job else 0,
                ),
            )
            assert task.lease
            receipt = tx.admit_operation(
                OperationRequest(
                    operation_id=identity,
                    task_id=task_id,
                    subject_id=task.agent_id,
                    name="models.invoke" if kind == "model" else "tools.incidents",
                    resource=ResourceRef(namespace=request.namespace, resource_id=task_id),
                    arguments=arguments,
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
                identity,
                OperationState.RUNNING,
                expected_revision=receipt.revision,
                lease_id=task.lease.lease_id,
                at=now(),
            )
        self.kernel.fault(f"agent.{kind}.intent_committed")
        return job

    def model_settle(self, task_id: str, proposal: Proposal) -> None:
        with self.kernel.database.engine.begin() as conn, borrow(conn) as tx:
            task = tx.get_task(task_id)
            request = self.guard(conn, task)
            run = (
                conn.exec_driver_sql("SELECT * FROM agent_runs WHERE task_id=?", (task_id,))
                .mappings()
                .one()
            )
            identity = f"{run['round']}.model"
            usage = proposal.usage
            # Record observed overage, then fail further work; never clamp observed consumption.
            conn.exec_driver_sql(
                "UPDATE agent_effects SET tokens=?,settled=1 WHERE task_id=? AND operation_id=?",
                (usage.total_tokens, task_id, identity),
            )
            encoded = json.dumps(asdict(proposal))
            conn.exec_driver_sql(
                "UPDATE agent_runs SET proposal=?,stage='model_done' WHERE task_id=?",
                (encoded, task_id),
            )
            assert task.lease
            receipt = tx.get_operation(task_id, identity)
            tx.change_operation(
                task_id,
                identity,
                OperationState.SUCCEEDED,
                expected_revision=receipt.revision,
                lease_id=task.lease.lease_id,
                at=now(),
                result={
                    "response_id": proposal.response_id,
                    "model": proposal.model,
                    "call_id": proposal.call_id,
                    "tool": proposal.tool,
                    "arguments": proposal.arguments,
                    "usage": asdict(usage),
                },
                usage=ResourceUsage(steps=1, model_calls=1, model_tokens=usage.total_tokens),
            )
            if usage.total_tokens <= agent_budget.INPUT_RESERVE + request.max_output_tokens:
                at = now()
                tx.change_task(
                    task_id,
                    TaskState.SUSPENDED,
                    expected_revision=task.revision,
                    lease_id=task.lease.lease_id,
                    at=at,
                    checkpoint=Checkpoint(
                        checkpoint_id=f"checkpoint-{task_id}-{run['round']}-model",
                        task_id=task_id,
                        saved_at=at,
                        state={"stage": "model_done"},
                        event_cursor=tx.latest_cursor(),
                    ),
                )
            self.kernel.fault("agent.model.settle_before_commit")
            if usage.total_tokens > agent_budget.INPUT_RESERVE + request.max_output_tokens:
                # Failure is committed with measured usage; caller will observe terminal state.
                tx.change_task(
                    task_id,
                    TaskState.FAILED,
                    expected_revision=task.revision,
                    lease_id=task.lease.lease_id,
                    at=now(),
                    failure=KernelFailure(
                        code="model_budget_breach", message="Provider exceeded reserve"
                    ),
                )
        self.kernel.fault("agent.model.settle_committed")

    def server(self, task_id: str, job: str) -> MCPServer[Any]:
        server: MCPServer[Any] = MCPServer("cardea-incidents", version="1", log_level="ERROR")
        host = self.kernel.host
        assert host

        @server.tool()
        async def compile_incidents(batch: IncidentBatch) -> IncidentArtifacts:
            """Build a sorted index and a Markdown summary of the supplied incident records."""
            with self.kernel.database.engine.begin() as conn, borrow(conn) as tx:
                task = tx.get_task(task_id)
                request = self.guard(conn, task)
                binding = Policy(conn).binding(task_id)
                expiry = conn.exec_driver_sql(
                    "SELECT MIN(expires_at) FROM policies WHERE namespace=? AND "
                    "((principal_id=? AND mode IN ('allow','delegate')) "
                    "OR principal_id='platform')",
                    (request.namespace, binding["supervisor_id"]),
                ).scalar_one()
                host.start(
                    job,
                    rfc8785.dumps(batch.model_dump()),
                    min(float(expiry), task.limits.deadline.timestamp()),
                    "tools.incidents",
                )
            self.kernel.fault("agent.tool.started")
            while True:
                self.check(task_id)
                observed = host.status(job)
                if observed["state"] == "finished":
                    if not host.stop(job).get("stopped"):
                        raise Fault("stop_unconfirmed", "Tool stop not confirmed")
                    if observed["result"] != "success" or observed["exit_status"] != "0":
                        raise Fault("tool_failed", "Worker failed")
                    content = base64.b64decode(observed["output"], validate=True)
                    if len(content) > 65536:
                        raise Fault("tool_output_too_large", "Worker output exceeds limit")
                    artifacts = IncidentArtifacts.model_validate_json(content)
                    verify(batch, artifacts)
                    # Metrics are broker observations, not tool-supplied claims in the artifacts.
                    self.observed = observed
                    return artifacts
                if observed["state"] != "running":
                    raise Fault("tool_outcome_unknown", "Worker outcome is unknown")
                await asyncio.sleep(0.1)

        return server

    def publish(self, task_id: str, artifacts: IncidentArtifacts, batch: IncidentBatch) -> str:
        with self.kernel.database.engine.begin() as conn, borrow(conn) as tx:
            task = tx.get_task(task_id)
            request = self.guard(conn, task)
            run = (
                conn.exec_driver_sql("SELECT * FROM agent_runs WHERE task_id=?", (task_id,))
                .mappings()
                .one()
            )
            round_id = int(run["round"])
            identity = f"{round_id}.tool"
            verify(batch, artifacts)
            observed = self.observed
            wall = observed.get("wall_ms")
            duration = wall if type(wall) is int and 0 <= wall <= 10000 else 10000
            size = len(base64.b64decode(observed["output"], validate=True))
            conn.exec_driver_sql(
                "UPDATE agent_effects SET tool_ms=?,output_bytes=?,settled=1 "
                "WHERE task_id=? AND operation_id=?",
                (duration, size, task_id, identity),
            )
            assert task.lease
            receipt = tx.get_operation(task_id, identity)
            tx.change_operation(
                task_id,
                identity,
                OperationState.SUCCEEDED,
                expected_revision=receipt.revision,
                lease_id=task.lease.lease_id,
                at=now(),
                result={"tool": "compile_incidents", "protocol": "2026-07-28"},
                usage=ResourceUsage(steps=1, tool_milliseconds=duration, output_bytes=size),
            )
            output = tx.put_object(
                ResourceRef(namespace=request.namespace, resource_id=f"{task_id}.output"),
                rfc8785.dumps(artifacts.model_dump()),
                expected_version=round_id,
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
                        "input": batch.model_dump(),
                        "output": output.model_dump(mode="json"),
                    }
                ),
                expected_version=round_id,
                producer_id=task.verifier_id,
                task_id=task_id,
                lease_id=task.lease.lease_id,
                at=now(),
            )
            cursor = conn.exec_driver_sql(
                "SELECT cursor FROM agent_inputs WHERE task_id=? AND round=?", (task_id, round_id)
            ).scalar_one()
            at = now()
            if round_id == 0:
                tx.change_task(
                    task_id,
                    TaskState.WAITING,
                    expected_revision=task.revision,
                    lease_id=task.lease.lease_id,
                    at=at,
                    checkpoint=Checkpoint(
                        checkpoint_id=f"checkpoint-{task_id}-0",
                        task_id=task_id,
                        saved_at=at,
                        state={"round": 0},
                        input_versions=(output,),
                        event_cursor=cursor,
                    ),
                    wait=EventWait(
                        namespace=request.namespace,
                        event_type=f"input.{task_id}",
                        after_cursor=cursor,
                    ),
                )
                stage = "waiting"
            else:
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
                            ResourceRef(
                                namespace=evidence.namespace, resource_id=evidence.resource_id
                            ),
                        ),
                    ),
                )
                stage = "succeeded"
            conn.exec_driver_sql(
                "UPDATE agent_runs SET stage=?,result=? WHERE task_id=?",
                (stage, artifacts.model_dump_json(), task_id),
            )
            self.kernel.fault("agent.publish_before_commit")
        self.kernel.fault("agent.publish_committed")
        return stage

    def fail(self, task_id: str, code: str, usage: Usage | None = None) -> str:
        uncertain = False
        with self.kernel.database.engine.begin() as conn, borrow(conn) as tx:
            task = tx.get_task(task_id)
            if task.state in TERMINAL:
                return task.state.value
            pending = (
                conn.exec_driver_sql(
                    "SELECT * FROM agent_effects WHERE task_id=? AND settled=0", (task_id,)
                )
                .mappings()
                .all()
            )
            if task.lease and now() >= task.lease.expires_at:
                task = tx.recover_task(task_id, expected_revision=task.revision, at=now())
            elif task.lease:
                uncertain = (
                    conn.exec_driver_sql(
                        "SELECT 1 FROM operations WHERE task_id=? AND state='outcome_unknown'",
                        (task_id,),
                    ).first()
                    is not None
                )
                for row in pending:
                    receipt = tx.get_operation(task_id, str(row["operation_id"]))
                    if receipt.state is OperationState.RUNNING:
                        unknown = usage is None
                        target = (
                            OperationState.OUTCOME_UNKNOWN if unknown else OperationState.FAILED
                        )
                        tx.change_operation(
                            task_id,
                            str(row["operation_id"]),
                            target,
                            expected_revision=receipt.revision,
                            lease_id=task.lease.lease_id,
                            at=now(),
                            failure=KernelFailure(
                                code=code, message="Effect did not complete with a verified result"
                            ),
                            usage=ResourceUsage(
                                steps=1,
                                model_calls=row["calls"],
                                model_tokens=usage.total_tokens
                                if usage and row["kind"] == "model"
                                else row["tokens"],
                                tool_milliseconds=row["tool_ms"],
                                output_bytes=row["output_bytes"],
                            ),
                        )
                        uncertain |= unknown
            if usage:
                conn.exec_driver_sql(
                    "UPDATE agent_effects SET tokens=? WHERE task_id=? "
                    "AND kind='model' AND settled=0",
                    (usage.total_tokens, task_id),
                )
            conn.exec_driver_sql("UPDATE agent_effects SET settled=1 WHERE task_id=?", (task_id,))
            if task.state not in TERMINAL and not uncertain:
                target_task = (
                    TaskState.CANCELLED if code == "cancel_requested" else TaskState.FAILED
                )
                task = tx.change_task(
                    task_id,
                    target_task,
                    expected_revision=task.revision,
                    lease_id=task.lease.lease_id if task.lease else None,
                    at=now(),
                    failure=KernelFailure(code=code, message="Agent task could not continue")
                    if target_task is TaskState.FAILED
                    else None,
                )
        # Stop/retire the local tool before waiting for the existing lease-recovery fence.
        self.cleanup()
        while uncertain:
            with self.kernel.database.engine.begin() as conn, borrow(conn) as tx:
                task = tx.get_task(task_id)
                if task.state in TERMINAL:
                    return task.state.value
                assert task.lease
                if now() >= task.lease.expires_at:
                    return tx.recover_task(
                        task_id, expected_revision=task.revision, at=now()
                    ).state.value
            time.sleep(0.1)
        return task.state.value

    def prepare(
        self, task_id: str
    ) -> tuple[AgentTaskRequest, IncidentBatch, str, str | None] | str:
        with self.kernel.database.engine.begin() as conn, borrow(conn) as tx:
            task = tx.get_task(task_id)
            if task.state in TERMINAL:
                return task.state.value
            request = self.guard(conn, task)
            run = (
                conn.exec_driver_sql("SELECT * FROM agent_runs WHERE task_id=?", (task_id,))
                .mappings()
                .one()
            )
            if task.state is TaskState.WAITING:
                return "waiting"
            if conn.exec_driver_sql(
                "SELECT 1 FROM operations WHERE task_id=? "
                "AND state IN ('accepted','running','outcome_unknown')",
                (task_id,),
            ).first():
                return "uncertain"
            if task.lease and now() >= task.lease.expires_at:
                task = tx.recover_task(task_id, expected_revision=task.revision, at=now())
            if task.state is TaskState.SUSPENDED:
                task = tx.change_task(
                    task_id, TaskState.QUEUED, expected_revision=task.revision, at=now()
                )
            if task.state is TaskState.QUEUED:
                at = now()
                task = tx.change_task(
                    task_id,
                    TaskState.RUNNING,
                    expected_revision=task.revision,
                    at=at,
                    lease=DispatchLease(
                        lease_id=f"lease-{uuid4().hex}",
                        worker_id=self.kernel.runtime_id,
                        acquired_at=at,
                        expires_at=min(at + timedelta(seconds=LEASE_SECONDS), task.limits.deadline),
                    ),
                )
            batch = IncidentBatch.model_validate_json(
                conn.exec_driver_sql(
                    "SELECT content FROM agent_inputs WHERE task_id=? AND round=?",
                    (task_id, run["round"]),
                ).scalar_one()
            )
            return request, batch, str(run["stage"]), run["proposal"]

    async def round(self, task_id: str) -> str:
        try:
            prepared = self.prepare(task_id)
            if isinstance(prepared, str):
                return (
                    self.fail(task_id, "agent_outcome_unknown")
                    if prepared == "uncertain"
                    else prepared
                )
            request, batch, stage, stored = prepared
            assert self.kernel.model is not None
            if stage == "ready":
                # Discovery is an installed trusted server; no user bytes or credentials disclosed.
                from cardea.runtime.incident_tool import server as reference_server

                async with Client(reference_server, cache=None) as client:
                    tools = await MCPTools(client, {"compile_incidents": SCHEMA}).discover()
                self.intent(
                    task_id,
                    "model",
                    {
                        "model": self.kernel.model.model,
                        "input": batch.model_dump(),
                        "tool": "compile_incidents",
                    },
                )
                proposal = await self.monitor(
                    task_id,
                    self.kernel.model.propose(
                        request.objective, batch.model_dump(), tools, request.max_output_tokens
                    ),
                )
                self.model_settle(task_id, proposal)
                self.prepare(task_id)
            else:
                data = json.loads(stored or "{}")
                proposal = Proposal(**(data | {"usage": Usage(**data["usage"])}))
            self.check(task_id)
            proposed = IncidentBatch.model_validate(proposal.arguments.get("batch"))
            if proposal.tool != "compile_incidents" or proposed != batch:
                raise Fault(
                    "model_input_substitution", "Proposed tool input differs from authorized input"
                )
            job = self.intent(
                task_id, "tool", {"call_id": proposal.call_id, "arguments": proposal.arguments}
            )
            assert job is not None
            async with Client(
                self.server(task_id, job), cache=None, read_timeout_seconds=15
            ) as client:
                adapter = MCPTools(client, {"compile_incidents": SCHEMA})
                tools = await adapter.discover()
                result = await self.monitor(task_id, adapter.call(tools[0], proposal.arguments))
                artifacts = IncidentArtifacts.model_validate(result.structured_content)
            return self.publish(task_id, artifacts, batch)
        except Exception as error:
            code = error.code if isinstance(error, (Fault, ModelFailure)) else "agent_effect_failed"
            if code == "agent_effect_failed":
                log.exception("unexpected agent effect failure", extra={"task_id": task_id})
            return self.fail(
                task_id, code, error.usage if isinstance(error, ModelFailure) else None
            )
        finally:
            self.cleanup()

    def wake(self, task_id: str) -> str:
        try:
            with self.kernel.database.engine.begin() as conn, borrow(conn) as tx:
                task = tx.get_task(task_id)
                if task.state in TERMINAL:
                    return task.state.value
                self.guard(conn, task)
                stage = conn.exec_driver_sql(
                    "SELECT stage FROM agent_runs WHERE task_id=?", (task_id,)
                ).scalar_one()
                if stage != "waiting":
                    return task.state.value
                if not conn.exec_driver_sql(
                    "SELECT 1 FROM agent_inputs WHERE task_id=? AND round=1", (task_id,)
                ).first():
                    return "waiting"
                if task.state is TaskState.WAITING:
                    tx.change_task(
                        task_id, TaskState.QUEUED, expected_revision=task.revision, at=now()
                    )
                conn.exec_driver_sql(
                    "UPDATE agent_runs SET round=1,stage='ready',proposal=NULL,result=NULL "
                    "WHERE task_id=?",
                    (task_id,),
                )
                return "queued"
        except Fault as error:
            return self.fail(task_id, error.code)

    def cleanup(self) -> None:
        host = self.kernel.host
        if host is None:
            return
        with self.kernel.database.engine.begin() as conn:
            rows = conn.exec_driver_sql(
                "SELECT e.task_id,e.operation_id,e.job FROM agent_effects e "
                "JOIN tasks t USING(task_id) WHERE e.kind='tool' AND e.cleaned=0 "
                "AND (e.settled=1 OR t.state IN ('succeeded','failed','cancelled')) LIMIT 32"
            ).all()
        for task_id, identity, job in rows:
            try:
                if host.stop(job).get("stopped") and host.cleanup(job).get("cleaned"):
                    with self.kernel.database.engine.begin() as conn:
                        conn.exec_driver_sql(
                            "UPDATE agent_effects SET cleaned=1 WHERE task_id=? AND operation_id=?",
                            (task_id, identity),
                        )
            except Exception:
                log.warning("tool cleanup deferred", exc_info=True, extra={"task_id": task_id})
                continue
