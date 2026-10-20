"""Authenticated application handlers; all acknowledged changes share SQLite commits."""

import json
from collections.abc import Callable
from datetime import timedelta
from hashlib import sha256
from uuid import uuid4

import sqlalchemy as sa
from dbos import DBOSClient, EnqueueOptions
from pydantic import JsonValue

from cardea.hosts.debian import Host
from cardea.kernel.models import BudgetLimits, Task, TaskState
from cardea.kernel.store import KernelTransaction
from cardea.runtime.agent_session import AgentSession
from cardea.runtime.openrouter import OpenRouterModel
from cardea.service import agent_budget, host_budget
from cardea.service.auth import IdentityProvider, Proof
from cardea.service.confined import ConfinedAgentSession, Workers, confined_handler, job_id
from cardea.service.database import Database, Fault, borrow
from cardea.service.models import (
    DEFAULT_MAX_TASK_REQUEST_BYTES,
    AgentInput,
    AgentRunRequest,
    AgentTaskRequest,
    BaseHostTaskRequest,
    DelegationTaskRequest,
    HostObjectTaskRequest,
    ObjectPermission,
    OperationTaskRequest,
    Permission,
    Provisioning,
    Signal,
    TaskRequest,
    TaskView,
    TriggerSpec,
    now,
)
from cardea.service.operations import KERNEL_RIGHTS, Handler, Registry, check_context
from cardea.service.policy import Policy, object_key
from cardea.service.profiles import (
    AGENT_RUN_QUEUE,
    AGENT_RUN_WORKFLOW,
    AGENT_WORKFLOW,
    DELEGATION_QUEUE,
    DELEGATION_WORKFLOW,
    HOST_WORKFLOW,
    OPERATION_WORKFLOW,
    QUEUE,
    WORKFLOW,
    Profile,
    for_request,
    for_runtime,
    runtime_ids,
)
from cardea.service.triggers import check_size
from cardea.service.triggers import install as install_triggers

__all__ = [
    "AGENT_RUN_QUEUE",
    "AGENT_RUN_WORKFLOW",
    "AGENT_WORKFLOW",
    "DELEGATION_QUEUE",
    "DELEGATION_WORKFLOW",
    "HOST_WORKFLOW",
    "OPERATION_WORKFLOW",
    "QUEUE",
    "WORKFLOW",
]
TERMINAL = {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED}
# Limits a profile does not mention are zero (concurrency one), as before profiles existed.
UNLIMITED = {
    "max_model_calls": 0,
    "max_model_tokens": 0,
    "max_tool_milliseconds": 0,
    "max_output_bytes": 0,
    "max_concurrent_operations": 1,
}


def workflow_id(task_id: str) -> str:
    return f"cardea:{task_id}"


def view(conn: sa.Connection, task_id: str) -> TaskView:
    binding = Policy(conn).binding(task_id)
    with borrow(conn) as tx:
        task = tx.get_task(task_id)
    return TaskView(
        task=task,
        supervisor_id=binding["supervisor_id"],
        agent_id=binding["agent_id"],
        runtime_principal_id=binding["runtime_id"],
        namespace=binding["namespace"],
        grants=json.loads(binding["grants_json"]),
        request_digest=binding["request_digest"],
    )


def provision(
    database: Database,
    document: Provisioning,
    operations: Registry | None = None,
    max_task_request_bytes: int = DEFAULT_MAX_TASK_REQUEST_BYTES,
) -> None:
    """Host-owner administration, intentionally separate from authenticated agent routes."""
    database.check_version()
    installed = operations or Registry()
    for rule in (*document.permissions, *document.platform):
        if rule.operation not in KERNEL_RIGHTS and rule.operation not in installed:
            raise Fault(
                "operation_not_installed", "Policy names an operation that is not installed"
            )
    client: DBOSClient | None = None
    try:
        with database.engine.begin() as conn, borrow(conn) as tx:
            # Services/humans first, then logical agents whose runtime FK must exist.
            for principal in sorted(document.principals, key=lambda p: p.kind == "agent"):
                old = (
                    conn.exec_driver_sql(
                        "SELECT * FROM principals WHERE principal_id=?", (principal.principal_id,)
                    )
                    .mappings()
                    .first()
                )
                immutable = ("kind", "auth_kind", "issuer", "subject", "runtime_id")
                if old and any(old[key] != getattr(principal, key) for key in immutable):
                    raise Fault(
                        "identity_immutable",
                        "Provision a new principal instead of reassigning identity",
                    )
                conn.exec_driver_sql(
                    "INSERT INTO principals VALUES (?,?,?,?,?,?,?) ON CONFLICT(principal_id) "
                    "DO UPDATE SET disabled=excluded.disabled",
                    (
                        principal.principal_id,
                        principal.kind,
                        int(principal.disabled),
                        principal.auth_kind,
                        principal.issuer,
                        principal.subject,
                        principal.runtime_id,
                    ),
                )
            # This is an explicit replacement of current policy, never task provenance/grants.
            conn.exec_driver_sql("DELETE FROM policies")
            policy_groups: tuple[tuple[bool, tuple[Permission | ObjectPermission, ...]], ...] = (
                (False, (*document.permissions, *document.object_permissions)),
                (True, (*document.platform, *document.object_platform)),
            )
            for platform, policies in policy_groups:
                for permission in policies:
                    subject = "platform" if platform else permission.principal_id
                    if platform and permission.principal_id != "platform":
                        raise Fault("invalid_platform_policy", "Platform policy must name platform")
                    if not platform:
                        row = conn.exec_driver_sql(
                            "SELECT 1 FROM principals WHERE principal_id=?", (subject,)
                        ).first()
                        if not row:
                            raise Fault("unknown_principal", "Policy principal is not provisioned")
                    for mode in ("allow", "delegate") if permission.delegate else ("allow",):
                        conn.exec_driver_sql(
                            "INSERT INTO policies VALUES (?,?,?,?,?)",
                            (
                                subject,
                                object_key(permission.object)
                                if isinstance(permission, ObjectPermission)
                                else permission.namespace,
                                permission.operation,
                                mode,
                                permission.expires_at.timestamp(),
                            ),
                        )
            conn.exec_driver_sql("DELETE FROM host_budget_limits")
            for budget in document.host_budgets:
                if not conn.exec_driver_sql(
                    "SELECT 1 FROM principals WHERE principal_id=?", (budget.principal_id,)
                ).first():
                    raise Fault("unknown_principal", "Budget principal is not provisioned")
                conn.exec_driver_sql(
                    "INSERT INTO host_budget_limits VALUES (?,?,?,?,?)",
                    (
                        budget.principal_id,
                        budget.namespace,
                        budget.tool_milliseconds,
                        budget.output_bytes,
                        budget.concurrent_operations,
                    ),
                )
            conn.exec_driver_sql("DELETE FROM model_budget_limits")
            for allocation in document.model_budgets:
                if not conn.exec_driver_sql(
                    "SELECT 1 FROM principals WHERE principal_id=?", (allocation.principal_id,)
                ).first():
                    raise Fault("unknown_principal", "Budget principal is not provisioned")
                conn.exec_driver_sql(
                    "INSERT INTO model_budget_limits VALUES (?,?,?,?)",
                    (
                        allocation.principal_id,
                        allocation.namespace,
                        allocation.calls,
                        allocation.tokens,
                    ),
                )
            install_triggers(conn, document.triggers, installed, now(), max_task_request_bytes)
            revision = (
                int(
                    conn.exec_driver_sql(
                        "SELECT value FROM service_meta WHERE name='policy_revision'"
                    ).scalar_one()
                )
                + 1
            )
            conn.exec_driver_sql(
                "UPDATE service_meta SET value=? WHERE name='policy_revision'", (str(revision),)
            )
            tx.append_event(
                namespace="kernel.authority",
                event_type="policy.updated",
                producer_id="host-admin",
                correlation_id=f"policy-{revision}",
                at=now(),
                payload={"revision": revision},
            )
            pending = conn.exec_driver_sql(
                "SELECT b.task_id FROM task_bindings b JOIN tasks t USING(task_id) "
                "WHERE t.state NOT IN ('succeeded','failed','cancelled')"
            ).fetchall()
            if pending:
                client = DBOSClient(system_database_url=database.url, use_listen_notify=False)
                for row in pending:
                    client.send_in_transaction(
                        conn,
                        workflow_id(row[0]),
                        "policy",
                        topic="control",
                        idempotency_key=f"policy:{revision}:{row[0]}",
                    )
    finally:
        if client:
            client.destroy()


class Kernel:
    def __init__(
        self,
        database: Database,
        auth: IdentityProvider,
        fault: Callable[[str], None] = lambda stage: None,
        host: Host | None = None,
        model: OpenRouterModel | None = None,
        operations: Registry | None = None,
        session: AgentSession | None = None,
        workers: Workers | None = None,
        max_task_request_bytes: int = DEFAULT_MAX_TASK_REQUEST_BYTES,
    ) -> None:
        self.database, self.auth, self.fault = database, auth, fault
        self.max_task_request_bytes = max_task_request_bytes
        self.host, self.model = host, model
        self.operations = operations or Registry(handlers={})
        self.session = session
        # Confined operation workers under host-owner profiles (ADR-028).
        self.workers = workers
        # Concurrent agent sessions; each holds a model session and its tool endpoint.
        self.session_limit = 1
        # Unfinished tasks of a profile whose adapters are missing could never recover.
        missing: tuple[tuple[str, str, Callable[[Profile], bool]], ...] = (
            (
                "host_required_for_recovery",
                "Enable the host adapter to recover unfinished tasks",
                lambda p: host is None and p.requires_host and not p.requires_model,
            ),
            (
                "session_required_for_recovery",
                "Configure the agent session to recover unfinished agent runs",
                lambda p: p.requires_session and session is None,
            ),
            (
                "agent_required_for_recovery",
                "Enable agent and host adapters",
                lambda p: p.requires_model and (model is None or host is None),
            ),
        )
        with database.engine.begin() as conn:
            for code, message, unavailable in missing:
                blocked = runtime_ids(unavailable)
                if (
                    blocked
                    and conn.exec_driver_sql(
                        "SELECT 1 FROM tasks WHERE state NOT IN ('succeeded','failed','cancelled') "
                        "AND json_extract(snapshot,'$.runtime_id') IN "
                        f"({','.join('?' * len(blocked))}) LIMIT 1",
                        blocked,
                    ).first()
                ):
                    raise Fault(code, message, 503)
        self.client = DBOSClient(system_database_url=database.url, use_listen_notify=False)
        with database.engine.begin() as conn:
            self.runtime_id = auth.runtime_proof().resolve(conn)
            if Policy(conn).principal(self.runtime_id)["kind"] != "service":
                raise Fault(
                    "runtime_binding",
                    "Kernel runtime requires a provisioned service principal",
                    500,
                )

    def submit(
        self, proof: Proof, request: TaskRequest, *, delegation: tuple[str, int] | None = None
    ) -> TaskView:
        from cardea.service.delegation import authorize_child

        profile = self._precheck(request, delegation)
        with self.database.engine.begin() as conn, borrow(conn) as tx:
            supervisor = proof.resolve(conn)
            if delegation is not None:
                if not isinstance(request, BaseHostTaskRequest):
                    raise Fault("delegation_binding", "Unsupported child profile")
                supervisor = authorize_child(
                    conn, supervisor, self.runtime_id, delegation[0], delegation[1], request
                )
            result = self._admit(conn, tx, supervisor, request, profile, delegation)
        self.fault("admission.after_commit")
        return result

    def schedule(self, proof: Proof, spec: TriggerSpec) -> dict[str, str]:
        """A supervisor installs or updates its own trigger (ADR-029).

        The caller must be the trigger's supervisor and hold `tasks.schedule` in the
        namespace. The template must pass admission against the caller's live ceiling now;
        every slot still re-runs full admission, so later revocation stops it.
        """
        from cardea.service.schedule import slot_key
        from cardea.service.triggers import upsert

        with self.database.engine.begin() as conn:
            principal = proof.resolve(conn)
            if spec.supervisor_id != principal:
                raise Fault("trigger_supervisor", "Supervisors schedule only their own work", 403)
            request = spec.request(f"{spec.trigger_id}.{slot_key(now())}")
            policy = Policy(conn)
            policy.require(principal, request.namespace, "tasks.schedule")
            policy.require("platform", request.namespace, "tasks.schedule")
            self._precheck(request, None)
            policy.admission(principal, request, self.runtime_id)
            kind = upsert(
                conn,
                spec,
                self.operations,
                now(),
                managed_by="supervisor",
                max_task_request_bytes=self.max_task_request_bytes,
            )
        return {"trigger_id": spec.trigger_id, "change": kind}

    def unschedule(self, proof: Proof, trigger_id: str) -> dict[str, str]:
        from cardea.service.triggers import remove

        with self.database.engine.begin() as conn:
            principal = proof.resolve(conn)
            row = (
                conn.exec_driver_sql(
                    "SELECT supervisor_id,managed_by,removed FROM triggers WHERE trigger_id=?",
                    (trigger_id,),
                )
                .mappings()
                .first()
            )
            if row is None or row["supervisor_id"] != principal:
                raise Fault("trigger_not_found", "No accessible trigger", 404)
            if row["managed_by"] != "supervisor":
                raise Fault("trigger_owner", "Host-owned triggers change only by provisioning", 409)
            if not row["removed"]:
                remove(conn, trigger_id, now())
        return {"trigger_id": trigger_id, "change": "removed"}

    def admit_scheduled(
        self,
        conn: sa.Connection,
        tx: KernelTransaction,
        supervisor: str,
        request: TaskRequest,
        provenance: dict[str, str],
    ) -> TaskView:
        """Trigger admission (ADR-024) inside the caller's transaction.

        The scheduler holds no credential of the supervisor. It authenticates as this
        kernel's runtime service, and the host-owner-installed trigger names the
        supervisor. Every check of `submit`, including `policy.admission` against the
        supervisor's live ceiling, still runs; nothing here widens authority.
        """
        if request.parent_task_id:
            raise Fault("delegation_binding", "Triggers cannot admit child tasks")
        profile = self._precheck(request, None)
        if self.auth.runtime_proof().resolve(conn) != self.runtime_id:
            raise Fault("runtime_binding", "Scheduler is not the installed kernel runtime", 500)
        return self._admit(conn, tx, supervisor, request, profile, None, provenance)

    # Confined operations (ADR-028)

    def executor_record(self, name: str) -> dict[str, JsonValue] | None:
        """Installed profile an operation runs under; refuses one the host lacks."""
        executor = self.operations.get(name).cardea.executor
        if executor is None:
            return None
        if self.workers is None:
            raise Fault(
                "worker_profile_not_installed", "Confined workers are not enabled on this host", 409
            )
        return self.workers.require(executor.profile, executor.worker)

    def queue_allowance(self, name: str) -> float:
        """Seconds an operation may wait for a confined-worker slot before it runs (ADR-028)."""
        executor = self.operations.get(name).cardea.executor
        if executor is None or self.workers is None:
            return 0.0
        return self.workers.queue_allowance(executor.profile)

    def handler(self, name: str) -> Handler:
        descriptor = self.operations.get(name)
        executor = descriptor.cardea.executor
        if executor is None:
            return self.operations.handler(name)
        self.executor_record(name)
        assert self.workers is not None
        return confined_handler(
            self.workers, executor, descriptor.cardea.timeout_seconds, self.worker_watch
        )

    def worker_watch(self, task_id: str, lease_id: str) -> str | None:
        """Why a running confined worker must stop now, if it must."""
        with self.database.read() as conn:
            row = conn.exec_driver_sql(
                "SELECT snapshot FROM tasks WHERE task_id=?", (task_id,)
            ).first()
            cancelled = conn.exec_driver_sql(
                "SELECT 1 FROM control_requests WHERE task_id=? AND kind='cancel' LIMIT 1",
                (task_id,),
            ).first()
        if cancelled is not None:
            return "cancel_requested"
        task = Task.model_validate_json(row[0]) if row is not None else None
        if (
            task is None
            or task.state is not TaskState.RUNNING
            or task.lease is None
            or task.lease.lease_id != lease_id
            or now() >= task.lease.expires_at
        ):
            return "lease_lost"
        return None

    def reap_workers(self, task_id: str) -> None:
        """Stop and clean up confined workers of a task's interrupted operations."""
        if self.workers is None:
            return
        with self.database.read() as conn:
            rows = conn.exec_driver_sql(
                "SELECT receipt_id FROM operations WHERE task_id=? "
                "AND state IN ('running','outcome_unknown') "
                "AND json_extract(snapshot,'$.request.arguments.executor') IS NOT NULL",
                (task_id,),
            ).fetchall()
        for (receipt_id,) in rows:
            self.workers.reap(job_id("operation", str(receipt_id)))

    def _precheck(self, request: TaskRequest, delegation: tuple[str, int] | None) -> Profile:
        profile = for_request(request)
        check_size(request, self.max_task_request_bytes)
        self.operations.check_grants(request.grants)
        if isinstance(request, OperationTaskRequest):
            descriptor = self.operations.get(request.operation)
            if not set(descriptor.cardea.requires) <= set(request.grants):
                raise Fault("task_grant_denied", "Operation requires additional explicit grants")
            self.operations.validate_arguments(request.operation, request.arguments)
            self.executor_record(request.operation)
        is_host = isinstance(request, BaseHostTaskRequest)
        if request.parent_task_id and (is_host or profile.owns_children) and delegation is None:
            raise Fault("delegation_binding", "Child admission requires a bound peer request")
        if profile.requires_model and (self.model is None or self.host is None):
            raise Fault("agent_disabled", "Agent execution requires model and host adapters", 503)
        if profile.requires_session and self.session is None:
            raise Fault("agent_disabled", "Agent runs require a configured agent session", 503)
        if isinstance(request, AgentRunRequest):
            for name in request.operations:
                self.executor_record(name)
            for call in request.context:
                check_context(self.operations, call.operation)
                self.operations.validate_arguments(call.operation, call.arguments)
                self.executor_record(call.operation)
            if isinstance(self.session, ConfinedAgentSession):
                self.session.check()
        if profile.requires_host and self.host is None:
            raise Fault(
                "unsupported_host", f"{profile.runtime_id} requires the installed host", 503
            )
        return profile

    def _admit(
        self,
        conn: sa.Connection,
        tx: KernelTransaction,
        supervisor: str,
        request: TaskRequest,
        profile: Profile,
        delegation: tuple[str, int] | None,
        provenance: dict[str, str] | None = None,
    ) -> TaskView:
        policy = Policy(conn)
        digest = request.digest(supervisor)
        existing = conn.exec_driver_sql(
            "SELECT task_id,request_digest FROM task_bindings "
            "WHERE supervisor_id=? AND request_id=?",
            (supervisor, request.request_id),
        ).first()
        if existing:
            policy.access(supervisor, existing[0], "tasks.inspect")
            if existing[1] != digest:
                raise Fault("request_conflict", "Request ID already has different arguments", 409)
            return view(conn, existing[0])
        if request.parent_task_id and delegation is None:
            ancestor = request.parent_task_id
            for _ in range(32):
                parent_task = tx.get_task(ancestor)
                if for_runtime(parent_task.runtime_id).owns_children:
                    raise Fault("delegation_binding", "Use the installed peer admission path")
                if not parent_task.parent_task_id:
                    break
                ancestor = parent_task.parent_task_id
            else:
                raise Fault("invalid_provenance", "Ancestor chain exceeds the supported bound")
        policy.admission(supervisor, request, self.runtime_id)
        if isinstance(request, AgentTaskRequest):
            agent_budget.available(
                conn,
                supervisor,
                request.namespace,
                agent_budget.INPUT_RESERVE + request.max_output_tokens,
            )
        if profile.reserves_host_budget:
            host_budget.available(conn, supervisor, request.namespace, reserve=True)
        if (
            isinstance(request, HostObjectTaskRequest)
            and len(tx.read_object(request.input_object)) > 16384
        ):
            raise Fault("input_too_large", "Host input exceeds 16384 bytes", 413)
        active = conn.exec_driver_sql(
            "SELECT COUNT(*) FROM tasks WHERE state NOT IN ('succeeded','failed','cancelled')"
        ).scalar_one()
        if active >= 100:
            raise Fault("capacity_exceeded", "M1 profile permits at most 100 active tasks", 429)
        task_id = "task-" + uuid4().hex
        at = now()
        task = Task(
            task_id=task_id,
            agent_id=request.agent_id,
            objective=request.objective,
            runtime_id=profile.runtime_id,
            verifier_id=profile.verifier_id,
            capability_ids=(f"grant-{task_id}",),
            budget_account_id=f"budget-{task_id}",
            parent_task_id=request.parent_task_id,
            limits=BudgetLimits.model_validate(
                UNLIMITED
                | profile.limits(request)
                | {
                    "deadline": min(
                        at + timedelta(seconds=request.timeout_seconds),
                        tx.get_task(request.parent_task_id).limits.deadline,
                    )
                    if request.parent_task_id
                    else at + timedelta(seconds=request.timeout_seconds)
                }
            ),
            created_at=at,
            updated_at=at,
        )
        tx.submit_task(task)
        conn.exec_driver_sql(
            "INSERT INTO task_bindings VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                task_id,
                supervisor,
                request.agent_id,
                self.runtime_id,
                request.namespace,
                request.request_id,
                digest,
                request.model_dump_json(),
                json.dumps(request.grants),
                tx.latest_cursor(),
                request.parent_task_id,
            ),
        )
        if delegation is not None:
            conn.exec_driver_sql(
                "INSERT INTO delegation_links VALUES (?,?,?,?)",
                (delegation[0], delegation[1], task_id, request.request_id),
            )
        if isinstance(request, DelegationTaskRequest):
            for child in request.children:
                peer = policy.principal(child.agent_id)
                if peer["kind"] != "agent" or peer["runtime_id"] != self.runtime_id:
                    raise Fault("peer_binding", "Peer is not installed in this trust domain")
        if isinstance(request, AgentTaskRequest):
            content = request.inputs.model_dump_json()
            conn.exec_driver_sql("INSERT INTO agent_runs(task_id) VALUES (?)", (task_id,))
            conn.exec_driver_sql(
                "INSERT INTO agent_inputs VALUES (?,0,?,?,?,?)",
                (
                    task_id,
                    request.request_id,
                    sha256(content.encode()).hexdigest(),
                    content,
                    tx.latest_cursor(),
                ),
            )
        # Provenance is immutable; this event contains IDs/digest, never identity proofs.
        tx.append_event(
            namespace="kernel.authority",
            event_type="task.bound",
            producer_id=supervisor,
            correlation_id=task_id,
            task_id=task_id,
            at=at,
            payload={
                "supervisor": supervisor,
                "runtime": self.runtime_id,
                "request_digest": digest,
                "schema_version": "0.2",
                **(provenance or {}),
            },
        )
        options: EnqueueOptions = {
            "workflow_name": profile.workflow,
            "queue_name": profile.queue,
            "workflow_id": workflow_id(task_id),
        }
        self.client.enqueue_in_transaction(
            conn, options, task_id, request.wait_for_event, task.limits.deadline.timestamp()
        )
        self.fault("admission.before_commit")
        return view(conn, task_id)

    def inspect(self, proof: Proof, task_id: str) -> TaskView:
        with self.database.read() as conn:
            principal = proof.resolve(conn)
            Policy(conn).access(principal, task_id, "tasks.inspect")
            return view(conn, task_id)

    def control(self, proof: Proof, task_id: str, signal: Signal, kind: str) -> TaskView:
        operation = "tasks.cancel" if kind == "cancel" else "tasks.resume"
        with self.database.engine.begin() as conn, borrow(conn) as tx:
            principal = proof.resolve(conn)
            binding = Policy(conn).access(principal, task_id, operation)
            task = tx.get_task(task_id)
            previous = conn.exec_driver_sql(
                "SELECT kind FROM control_requests WHERE task_id=? AND request_id=?",
                (task_id, signal.request_id),
            ).first()
            if previous:
                if previous[0] != kind:
                    raise Fault(
                        "request_conflict", "Control request ID was used for another action", 409
                    )
                return view(conn, task_id)
            if task.state in TERMINAL:
                if kind == "cancel":
                    return view(conn, task_id)
                raise Fault("task_terminal", "A terminal task cannot be resumed", 409)
            at = now()
            profile = for_runtime(task.runtime_id)
            if (
                kind == "cancel"
                and not profile.runtime_cancels
                and not (profile.runtime_cancels_running and task.state is TaskState.RUNNING)
            ):
                tx.change_task(
                    task_id,
                    TaskState.CANCELLED,
                    expected_revision=task.revision,
                    lease_id=task.lease.lease_id if task.lease else None,
                    at=at,
                )
            event = tx.append_event(
                namespace=binding["namespace"],
                event_type=f"{kind}.{task_id}",
                producer_id=principal,
                correlation_id=signal.request_id,
                task_id=task_id,
                at=at,
            )
            conn.exec_driver_sql(
                "INSERT INTO control_requests VALUES (?,?,?,?)",
                (task_id, signal.request_id, kind, event.cursor),
            )
            self.client.send_in_transaction(
                conn,
                workflow_id(task_id),
                kind,
                topic="control",
                idempotency_key=f"control:{task_id}:{signal.request_id}",
            )
            if kind == "cancel" and profile.owns_children:
                from cardea.service.delegation import cancel_children

                cancel_children(self, conn, task_id)
            self.fault("control.before_commit")
            result = view(conn, task_id)
        self.fault("control.after_commit")
        return result

    def append_input(self, proof: Proof, task_id: str, request: AgentInput) -> TaskView:
        with self.database.engine.begin() as conn, borrow(conn) as tx:
            principal = proof.resolve(conn)
            binding = Policy(conn).access(principal, task_id, "inputs.append")
            content = request.inputs.model_dump_json()
            digest = sha256(content.encode()).hexdigest()
            previous = conn.exec_driver_sql(
                "SELECT digest FROM agent_inputs WHERE task_id=? AND request_id=?",
                (task_id, request.request_id),
            ).first()
            if previous:
                if previous[0] != digest:
                    raise Fault("request_conflict", "Input request ID has different bytes", 409)
                return view(conn, task_id)
            task = tx.get_task(task_id)
            if not for_runtime(task.runtime_id).accepts_input or task.state in TERMINAL:
                raise Fault("input_denied", "Task cannot accept this input", 409)
            if conn.exec_driver_sql(
                "SELECT 1 FROM agent_inputs WHERE task_id=? AND round=1", (task_id,)
            ).first():
                raise Fault("input_limit", "This profile accepts one additive update", 409)
            original = AgentTaskRequest.model_validate_json(binding["request_json"])
            initial = {r.id: r for r in original.inputs.records}
            updated = {r.id: r for r in request.inputs.records}
            if len(updated) != len(initial) + 1 or any(
                updated.get(k) != v for k, v in initial.items()
            ):
                raise Fault(
                    "input_not_additive", "Update must preserve inputs and add one record", 409
                )
            event = tx.append_event(
                namespace=binding["namespace"],
                event_type=f"input.{task_id}",
                producer_id=principal,
                correlation_id=request.request_id,
                task_id=task_id,
                at=now(),
                payload={"digest": digest},
            )
            conn.exec_driver_sql(
                "INSERT INTO agent_inputs VALUES (?,1,?,?,?,?)",
                (task_id, request.request_id, digest, content, event.cursor),
            )
            self.client.send_in_transaction(
                conn,
                workflow_id(task_id),
                "input",
                topic="control",
                idempotency_key=f"input:{task_id}:{request.request_id}",
            )
            return view(conn, task_id)

    def close(self) -> None:
        self.client.destroy()
