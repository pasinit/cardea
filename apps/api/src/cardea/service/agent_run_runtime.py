"""One agent run: bounded sessions whose every tool call is a Cardea operation (ADR-027).

A session is an opaque unit. Cardea owns the attempt lease, the per-attempt tool endpoint,
each tool call's intent/receipt/scope, and the run's completion record. An orphaned session
cannot act after its lease is replaced, and a retried session receives the call journal.
"""

import asyncio
import json
import logging
import time
from contextlib import suppress
from datetime import timedelta
from typing import Any
from uuid import uuid4

import rfc8785
import sqlalchemy as sa
from mcp import types

from cardea.kernel.models import (
    Checkpoint,
    DispatchLease,
    KernelFailure,
    OperationReceipt,
    OperationState,
    ResourceRef,
    Task,
    TaskState,
    VerificationRecord,
)
from cardea.kernel.store import KernelTransaction
from cardea.runtime.agent_session import SessionRequest, SessionResult
from cardea.service import invocation
from cardea.service.confined import ConfinedAgentSession, WorkerFailed
from cardea.service.database import Fault, borrow
from cardea.service.kernel import TERMINAL, Kernel
from cardea.service.models import AgentRunRequest, now
from cardea.service.operations import OperationRejected
from cardea.service.policy import Policy
from cardea.service.tool_endpoint import ToolEndpoint, operation_name, tool_name

log = logging.getLogger(__name__)
LEASE_MARGIN = 30.0
SETTLE_MARGIN = 1.0
POLL_SECONDS = 1.0
MAX_TOOL_TEXT = 16384

SYSTEM_PROMPT = """You are carrying out one task inside Cardea, an execution kernel.
Your only tools are the ones provided; you have no shell, files or web access of your own.
Every tool call is recorded and checked, and Cardea may refuse one.
If a tool reports that an effect was already done, or that an earlier attempt is in doubt,
do not try to repeat that effect another way.
When you are finished, reply with a short plain summary of what you did."""


def _text(value: object) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    return text[:MAX_TOOL_TEXT]


def _error(code: str, detail: str = "") -> types.CallToolResult:
    message = f"{code}: {detail}" if detail else code
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=message)], is_error=True
    )


def attempt_lease_seconds(request: AgentRunRequest) -> float:
    """Lease for one attempt: the session budget plus time to settle and publish."""
    return request.attempt_timeout_seconds + LEASE_MARGIN


class AgentRunRuntime:
    def __init__(self, kernel: Kernel) -> None:
        self.kernel = kernel

    # Authority

    def guard(self, conn: sa.Connection, task: Task) -> AgentRunRequest:
        runtime = self.kernel.auth.runtime_proof().resolve(conn)
        policy = Policy(conn)
        binding = policy.binding(task.task_id)
        request = AgentRunRequest.model_validate_json(binding["request_json"])
        for right in ("models.invoke", "objects.read", "objects.write"):
            policy.ceiling(task.task_id, right)
        for name in request.operations:
            policy.ceiling(task.task_id, name)
            for required in self.kernel.operations.get(name).cardea.requires:
                policy.ceiling(task.task_id, required)
        if runtime != binding["runtime_id"] or runtime != self.kernel.runtime_id:
            raise Fault("runtime_binding", "Runtime does not match task")
        if task.state in TERMINAL or now() >= task.limits.deadline:
            raise Fault("deadline_or_terminal", "Task cannot dispatch")
        if conn.exec_driver_sql(
            "SELECT 1 FROM control_requests WHERE task_id=? AND kind='cancel' LIMIT 1",
            (task.task_id,),
        ).first():
            raise Fault("cancel_requested", "Cancellation requested")
        return request

    # Attempt lifecycle

    def execute(self, task_id: str) -> str:
        while True:
            started = self._start_attempt(task_id)
            if isinstance(started, str):
                if started == "recover":
                    state = self._recover(task_id)
                    if state in {s.value for s in TERMINAL}:
                        return state
                    continue
                return started
            request, lease, attempt = started
            self.kernel.fault("agent_run.attempt_started")
            result = self._attempt(task_id, request, lease, attempt)
            self.kernel.fault("agent_run.before_finish")
            outcome = self._finish(task_id, request, lease, attempt, result)
            if outcome == "retry":
                continue
            if outcome == "uncertain":
                state = self._recover(task_id)
                if state in {s.value for s in TERMINAL}:
                    return state
                continue
            return outcome

    def _start_attempt(self, task_id: str) -> str | tuple[AgentRunRequest, DispatchLease, int]:
        with self.kernel.database.engine.begin() as conn, borrow(conn) as tx:
            task = tx.get_task(task_id)
            if task.state in TERMINAL:
                return task.state.value
            if task.state is TaskState.RUNNING:
                # An interrupted attempt: its lease must expire before anything else runs.
                return "recover"
            if task.state is TaskState.SUSPENDED:
                task = tx.change_task(
                    task_id, TaskState.QUEUED, expected_revision=task.revision, at=now()
                )
            recorded = task.checkpoint.state.get("attempt", 0) if task.checkpoint else 0
            attempt = (recorded if isinstance(recorded, int) else 0) + 1
            try:
                request = self.guard(conn, task)
                if attempt > request.max_attempts:
                    raise Fault("attempts_exhausted", "Every allowed session attempt was used")
            except Fault as error:
                tx.change_task(
                    task_id,
                    TaskState.FAILED,
                    expected_revision=task.revision,
                    at=now(),
                    failure=KernelFailure(code=error.code, message=error.message),
                )
                return "failed"
            # Record the attempt before it starts, so a crash cannot repeat its number and
            # recovery always finds a checkpoint.
            at = now()
            task = tx.change_task(
                task_id,
                TaskState.RUNNING,
                expected_revision=task.revision,
                at=at,
                lease=self._lease(at, task.limits.deadline, 1.0),
            )
            assert task.lease is not None
            at = now()
            task = tx.change_task(
                task_id,
                TaskState.SUSPENDED,
                expected_revision=task.revision,
                lease_id=task.lease.lease_id,
                at=at,
                checkpoint=Checkpoint(
                    checkpoint_id=f"checkpoint-{task_id}-{attempt}",
                    task_id=task_id,
                    saved_at=at,
                    state={"attempt": attempt},
                ),
            )
            task = tx.change_task(
                task_id, TaskState.QUEUED, expected_revision=task.revision, at=now()
            )
            at = now()
            task = tx.change_task(
                task_id,
                TaskState.RUNNING,
                expected_revision=task.revision,
                at=at,
                lease=self._lease(at, task.limits.deadline, attempt_lease_seconds(request)),
            )
            assert task.lease is not None
            return request, task.lease, attempt

    def _lease(self, at: Any, deadline: Any, seconds: float) -> DispatchLease:
        return DispatchLease(
            lease_id="lease-" + uuid4().hex,
            worker_id=self.kernel.runtime_id,
            acquired_at=at,
            expires_at=min(deadline, at + timedelta(seconds=seconds)),
        )

    def _recover(self, task_id: str) -> str:
        """Wait out an interrupted attempt's lease, then fence it through kernel recovery."""
        while True:
            with self.kernel.database.read() as conn:
                row = conn.exec_driver_sql(
                    "SELECT snapshot FROM tasks WHERE task_id=?", (task_id,)
                ).one()
            seen = Task.model_validate_json(row[0])
            if seen.lease is not None and now() >= seen.lease.expires_at:
                # Outside the write transaction: helper calls can take seconds.
                self._reap(task_id, seen)
            with self.kernel.database.engine.begin() as conn, borrow(conn) as tx:
                task = tx.get_task(task_id)
                if task.state is not TaskState.RUNNING:
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
            time.sleep(POLL_SECONDS)

    def _reap(self, task_id: str, task: Task) -> None:
        """Stop confined workers an interrupted attempt may have left running (ADR-028)."""
        self.kernel.reap_workers(task_id)
        session = self.kernel.session
        recorded = task.checkpoint.state.get("attempt") if task.checkpoint else None
        if isinstance(session, ConfinedAgentSession) and isinstance(recorded, int):
            session.reap(f"{task_id}:{recorded}")

    # One session

    def _journal(self, tx: KernelTransaction, task_id: str) -> list[OperationReceipt]:
        return [
            receipt
            for receipt in tx.list_operations(task_id)
            if receipt.request.operation_id.startswith("call-")
        ]

    def _prompt(
        self, request: AgentRunRequest, attempt: int, journal: list[OperationReceipt]
    ) -> str:
        instructions = request.instructions
        if request.expect:
            instructions += (
                "\n\nThis run is complete only with "
                + "; ".join(e.describe() for e in request.expect)
                + "."
            )
        if attempt == 1 or not journal:
            return instructions
        lines = []
        for receipt in journal:
            arguments = receipt.request.arguments.get("arguments")
            outcome = (
                receipt.result.get("output")
                if receipt.result
                else receipt.failure.code
                if receipt.failure
                else receipt.state.value
            )
            lines.append(
                f"- {tool_name(receipt.request.name)} {_text(arguments)[:400]} -> "
                f"{receipt.state.value}: {_text(outcome)[:800]}"
            )
        return (
            f"{instructions}\n\nThis is attempt {attempt}. An earlier attempt stopped "
            "before finishing. Its tool calls are listed below and their results are final; "
            "do not repeat effects that already succeeded.\n" + "\n".join(lines)
        )

    def _attempt(
        self, task_id: str, request: AgentRunRequest, lease: DispatchLease, attempt: int
    ) -> SessionResult:
        session = self.kernel.session
        if session is None:
            return SessionResult(text="", is_error=True, stop_reason="agent_disabled")
        with self.kernel.database.engine.begin() as conn, borrow(conn) as tx:
            journal = self._journal(tx, task_id)
        tools = []
        for name in request.operations:
            descriptor = self.kernel.operations.get(name)
            tool = types.Tool.model_validate(descriptor.tool)
            tools.append(tool.model_copy(update={"name": tool_name(name)}))

        async def call(tool: str, arguments: dict[str, Any]) -> types.CallToolResult:
            return await self.call_tool(
                task_id, lease.lease_id, request, operation_name(tool), arguments
            )

        timeout = min(
            float(request.attempt_timeout_seconds),
            (lease.expires_at - now()).total_seconds() - LEASE_MARGIN,
        )
        with ToolEndpoint(tools, call) as endpoint:
            session_request = SessionRequest(
                # Sessions have no clock of their own; scheduled work needs the date.
                system_prompt=f"{SYSTEM_PROMPT}\nThe current time is {now().isoformat()} (UTC).",
                prompt=self._prompt(request, attempt, journal),
                mcp_url=endpoint.url,
                mcp_token=endpoint.token,
                tool_names=tuple(tool.name for tool in tools),
                max_turns=request.max_turns,
                max_cost_usd=request.max_cost_usd,
                timeout_seconds=timeout,
                job_key=f"{task_id}:{attempt}",
            )
            return asyncio.run(self._supervise(task_id, session_request))

    async def _supervise(self, task_id: str, request: SessionRequest) -> SessionResult:
        session = self.kernel.session
        assert session is not None
        running = asyncio.ensure_future(session.run(request))
        deadline = time.monotonic() + max(0.0, request.timeout_seconds)
        try:
            while True:
                done, _ = await asyncio.wait({running}, timeout=POLL_SECONDS)
                if done:
                    return running.result()
                if time.monotonic() >= deadline:
                    return SessionResult(text="", is_error=True, stop_reason="attempt_timeout")
                if self._cancel_requested(task_id):
                    return SessionResult(text="", is_error=True, stop_reason="cancel_requested")
        except Exception as error:
            log.exception("agent session failed", extra={"task_id": task_id})
            return SessionResult(text="", is_error=True, stop_reason=type(error).__name__)
        finally:
            if not running.done():
                running.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await running

    def _cancel_requested(self, task_id: str) -> bool:
        with self.kernel.database.read() as conn:
            return (
                conn.exec_driver_sql(
                    "SELECT 1 FROM control_requests WHERE task_id=? AND kind='cancel' LIMIT 1",
                    (task_id,),
                ).first()
                is not None
            )

    # One tool call

    async def call_tool(
        self,
        task_id: str,
        lease_id: str,
        request: AgentRunRequest,
        name: str,
        arguments: dict[str, Any],
    ) -> types.CallToolResult:
        if name not in request.operations:
            return _error("tool_not_exposed")
        registry = self.kernel.operations
        try:
            with self.kernel.database.engine.begin() as conn, borrow(conn) as tx:
                task = tx.get_task(task_id)
                if (
                    task.state is not TaskState.RUNNING
                    or task.lease is None
                    or task.lease.lease_id != lease_id
                    or now() >= task.lease.expires_at
                ):
                    # The session outlived its attempt; it can no longer act.
                    return _error("stale_session", "this attempt has ended")
                self.guard(conn, task)
                count = sum(
                    1
                    for r in tx.list_operations(task_id)
                    if r.request.operation_id.startswith("call-")
                )
                if count >= request.max_tool_calls:
                    return _error("tool_budget_exhausted")
                descriptor = registry.get(name)
                handler = self.kernel.handler(name)
                executor = self.kernel.executor_record(name)
                admitted = invocation.admit(
                    conn,
                    tx,
                    registry,
                    task,
                    lease_id=lease_id,
                    operation_id=f"call-{count + 1:04d}",
                    name=name,
                    namespace=request.namespace,
                    arguments=arguments,
                    scope_context=request.idempotency_context or task_id,
                    deadline=task.limits.deadline,
                    executor=executor,
                )
                operation_id = f"call-{count + 1:04d}"
                timeout = invocation.timeout_for(
                    registry, name, task.lease.expires_at, SETTLE_MARGIN
                )
        except Fault as refused:
            return _error(refused.code, refused.message)
        if admitted.deduplicated is not None:
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text="Already done earlier in this scope; not repeated. "
                        + _text(admitted.deduplicated),
                    )
                ],
                structured_content=admitted.deduplicated,
            )
        started = time.monotonic()
        error = ""
        output: object = None
        try:
            if timeout <= 0:
                raise OperationRejected("no time left in this attempt")
            output = await asyncio.wait_for(handler(dict(arguments), admitted.context), timeout)
        except OperationRejected as rejected:
            error = "operation_rejected"
            output = str(rejected)
        except WorkerFailed as failed:
            # A confined worker started and gave no result (ADR-028).
            log.warning("confined worker failed: %s", failed.code, extra={"task_id": task_id})
            if descriptor.cardea.effect == "effect":
                self._mark_uncertain(task_id, lease_id, operation_id)
                return _error(
                    "outcome_unknown",
                    "the effect may or may not have happened; do not retry it, "
                    "Cardea will reconcile it",
                )
            error = failed.code
        except Exception:
            log.exception("installed operation failed", extra={"task_id": task_id})
            if descriptor.cardea.effect == "effect":
                self._mark_uncertain(task_id, lease_id, operation_id)
                return _error(
                    "outcome_unknown",
                    "the effect may or may not have happened; do not retry it, "
                    "Cardea will reconcile it",
                )
            error = "operation_failed"
        elapsed = max(0, int((time.monotonic() - started) * 1000))
        with self.kernel.database.engine.begin() as conn, borrow(conn) as tx:
            task = tx.get_task(task_id)
            if (
                task.lease is None
                or task.lease.lease_id != lease_id
                or now() >= task.lease.expires_at
            ):
                # Too late to settle; recovery fences the call.
                return _error("stale_session", "this attempt has ended")
            try:
                self.guard(conn, task)
            except Fault as denied:
                error = error or denied.code
            code, content = invocation.settle(
                conn,
                tx,
                registry,
                task,
                lease_id=lease_id,
                operation_id=operation_id,
                output=None if error else output,
                error=error,
                elapsed_ms=elapsed,
            )
        if code:
            return _error(code, _text(output) if code == "operation_rejected" else "")
        structured = json.loads(content)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=_text(structured))],
            structured_content=structured,
        )

    def _mark_uncertain(self, task_id: str, lease_id: str, operation_id: str) -> None:
        with self.kernel.database.engine.begin() as conn, borrow(conn) as tx:
            task = tx.get_task(task_id)
            if (
                task.lease is None
                or task.lease.lease_id != lease_id
                or now() >= task.lease.expires_at
            ):
                return
            invocation.uncertain(conn, tx, task, lease_id=lease_id, operation_id=operation_id)

    # Completion

    def _finish(
        self,
        task_id: str,
        request: AgentRunRequest,
        lease: DispatchLease,
        attempt: int,
        result: SessionResult,
    ) -> str:
        with self.kernel.database.engine.begin() as conn, borrow(conn) as tx:
            task = tx.get_task(task_id)
            if task.state in TERMINAL:
                return task.state.value
            if task.lease is None or task.lease.lease_id != lease.lease_id:
                return "uncertain"
            if now() >= task.lease.expires_at:
                return "uncertain"
            calls = self._journal(tx, task_id)
            if any(
                c.state in {OperationState.RUNNING, OperationState.OUTCOME_UNKNOWN} for c in calls
            ):
                # An effect may have happened: the run waits for its lease and is fenced.
                return "uncertain"
            if result.stop_reason == "cancel_requested":
                tx.change_task(
                    task_id,
                    TaskState.CANCELLED,
                    expected_revision=task.revision,
                    lease_id=lease.lease_id,
                    at=now(),
                )
                return "cancelled"
            unmet = self._unmet(request, calls)
            if any(code == "expectation_exceeded" for code, _ in unmet):
                tx.change_task(
                    task_id,
                    TaskState.FAILED,
                    expected_revision=task.revision,
                    lease_id=lease.lease_id,
                    at=now(),
                    failure=KernelFailure(code="expectation_exceeded", message=unmet[0][1]),
                )
                return "failed"
            if not result.is_error and result.text.strip() and not unmet:
                return self._publish(tx, task, request, lease, attempt, result, calls)
            if unmet and not result.is_error:
                result = SessionResult(
                    text=result.text, is_error=True, stop_reason="expectation_unmet"
                )
            if attempt < request.max_attempts and now() < task.limits.deadline:
                at = now()
                tx.change_task(
                    task_id,
                    TaskState.SUSPENDED,
                    expected_revision=task.revision,
                    lease_id=lease.lease_id,
                    at=at,
                    checkpoint=Checkpoint(
                        checkpoint_id=f"checkpoint-{task_id}-{attempt}-end",
                        task_id=task_id,
                        saved_at=at,
                        state={"attempt": attempt, "last_stop": result.stop_reason[:64]},
                    ),
                )
                return "retry"
            tx.change_task(
                task_id,
                TaskState.FAILED,
                expected_revision=task.revision,
                lease_id=lease.lease_id,
                at=now(),
                failure=KernelFailure(
                    code="expectation_unmet"
                    if result.stop_reason == "expectation_unmet"
                    else "session_failed",
                    message=f"Agent session did not finish: {result.stop_reason[:120]}",
                ),
            )
            return "failed"

    @staticmethod
    def _unmet(request: AgentRunRequest, calls: list[OperationReceipt]) -> list[tuple[str, str]]:
        problems = []
        for expectation in request.expect:
            count = sum(
                1
                for c in calls
                if c.request.name == expectation.operation and c.state is OperationState.SUCCEEDED
            )
            if count < expectation.min_calls:
                problems.append(("expectation_unmet", f"Needs {expectation.describe()}"))
            elif expectation.max_calls is not None and count > expectation.max_calls:
                problems.append(("expectation_exceeded", f"Exceeds {expectation.describe()}"))
        return problems

    def _publish(
        self,
        tx: KernelTransaction,
        task: Task,
        request: AgentRunRequest,
        lease: DispatchLease,
        attempt: int,
        result: SessionResult,
        calls: list[OperationReceipt],
    ) -> str:
        summary = [
            {
                "operation_id": c.request.operation_id,
                "operation": c.request.name,
                "state": c.state.value,
                "deduplicated": bool(c.result and c.result.get("deduplicated")),
            }
            for c in calls
        ]
        output = tx.put_object(
            ResourceRef(namespace=request.namespace, resource_id=f"{task.task_id}.output"),
            rfc8785.dumps(
                {
                    "text": result.text,
                    "attempts": attempt,
                    "turns": result.turns,
                    "cost_usd": result.cost_usd,
                    "calls": summary,
                }
            ),
            expected_version=0,
            producer_id=task.agent_id,
            task_id=task.task_id,
            lease_id=lease.lease_id,
            at=now(),
        )
        evidence = tx.put_object(
            ResourceRef(namespace=request.namespace, resource_id=f"{task.task_id}.verification"),
            rfc8785.dumps(
                {
                    "passed": True,
                    "check": "all_calls_settled",
                    "expectations": [e.describe() for e in request.expect],
                    "calls": len(calls),
                    "effects_succeeded": sum(
                        1
                        for c in calls
                        if c.state is OperationState.SUCCEEDED
                        and c.retry_class.value == "reconciliation_required"
                    ),
                    "output_digest": output.content_digest,
                }
            ),
            expected_version=0,
            producer_id=task.verifier_id,
            task_id=task.task_id,
            lease_id=lease.lease_id,
            at=now(),
        )
        at = now()
        tx.change_task(
            task.task_id,
            TaskState.SUCCEEDED,
            expected_revision=task.revision,
            lease_id=lease.lease_id,
            at=at,
            verification=VerificationRecord(
                verification_id=f"verification-{task.task_id}",
                task_id=task.task_id,
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
