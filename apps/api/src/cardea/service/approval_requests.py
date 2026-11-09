"""Approval requests and decisions for single-operation tasks (ADR-036 slice 2).

A call that an approval rule requires is frozen as a request (`approval_requests`) and the task
waits on the v0.1 ``EventWait`` for ``approval.<request_id>``. Deciding is an authenticated
call checked against current Casbin policy in the same SQLite transaction that records it,
like ``tasks.cancel``; the transaction appends the waking event, wakes the task and enqueues
its continuation under a deterministic DBOS workflow ID. The continuation re-checks the live
ceiling, the approver's right and the descriptor version before the stored intent runs as
``apr-NN`` through the shared invocation core (see ``operation_runtime``).

Approval only narrows: an operation outside the task's live ceiling is refused before any
request exists, and the ceiling is checked again at decision and at execution.
"""

import json
from collections.abc import Mapping
from datetime import datetime, timedelta
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

import rfc8785
import sqlalchemy as sa
from dbos import EnqueueOptions
from pydantic import Field, JsonValue

from cardea.kernel.models import Identifier, Task, TaskState
from cardea.kernel.store import KernelTransaction
from cardea.service.approvals import (
    ApprovalRequirement,
    HostApprovalRule,
    HostApprovals,
    Reason,
    requirement,
)
from cardea.service.auth import Proof
from cardea.service.database import Fault, borrow
from cardea.service.models import OperationTaskRequest, Wire, now
from cardea.service.operations import Registry
from cardea.service.policy import Policy
from cardea.service.profiles import APPROVAL_WORKFLOW, QUEUE

if TYPE_CHECKING:
    from cardea.service.kernel import Kernel

TERMINAL_TASK = {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED}
OPEN = ("pending", "approved")
# Why an approved or pending request became void; nothing ran.
VOID_REASONS = {
    "operation_not_installed": "operation_changed",
    "invalid_arguments": "operation_changed",
    "worker_profile_not_installed": "operation_changed",
    "operation_handler_missing": "operation_changed",
    "deadline_or_terminal": "deadline_exceeded",
}
Digest = Field(strict=True, pattern=r"^[0-9a-f]{64}$")


class DecisionRequest(Wire):
    """`POST /v0.2/approvals/{request_id}/decision`. `decision_id` is the idempotency key."""

    decision_id: Identifier
    decision: Literal["approve", "reject"]
    # The request's intent_digest as the approver saw it; a mismatch is refused.
    intent_digest: str = Digest
    reason: Reason | None = None


def argument_sha256(arguments: Mapping[str, JsonValue]) -> str:
    return sha256(rfc8785.dumps(dict(arguments))).hexdigest()


def intent_digest(
    *,
    task_id: str,
    namespace: str,
    operation: str,
    version: int,
    arguments_sha256: str,
    idempotency_context: str | None,
) -> str:
    """SHA-256 of the RFC 8785 encoding of exactly what an approver approves (ADR-036 item 3)."""
    return sha256(
        rfc8785.dumps(
            {
                "task_id": task_id,
                "namespace": namespace,
                "operation": operation,
                "version": version,
                "argument_sha256": arguments_sha256,
                "idempotency_context": idempotency_context,
            }
        )
    ).hexdigest()


def continuation_id(task_id: str, request_id: str) -> str:
    """Deterministic workflow ID: a decision enqueues its continuation at most once."""
    return f"cardea:{task_id}.approval.{request_id}"


def evaluate(
    conn: sa.Connection,
    registry: Registry,
    name: str,
    namespace: str,
    arguments: Mapping[str, JsonValue],
) -> ApprovalRequirement:
    """The descriptor default, replaced by a host rule for the namespace when one exists."""
    row = conn.exec_driver_sql(
        "SELECT rules_json FROM approval_rules WHERE namespace=? AND operation=?",
        (namespace, name),
    ).first()
    host = (
        HostApprovals(entries=(HostApprovalRule.model_validate_json(row[0]),))
        if row is not None
        else None
    )
    return requirement(
        operation=name,
        namespace=namespace,
        args=arguments,
        descriptor=registry.get(name).cardea.approval,
        host=host,
    )


def get(conn: sa.Connection, request_id: str) -> sa.RowMapping:
    row = (
        conn.exec_driver_sql("SELECT * FROM approval_requests WHERE request_id=?", (request_id,))
        .mappings()
        .first()
    )
    if row is None:
        raise Fault("approval_not_found", "No accessible approval request", 404)
    return row


def for_task(conn: sa.Connection, task_id: str) -> sa.RowMapping | None:
    """The task's latest request; a single-operation task has at most one."""
    return (
        conn.exec_driver_sql(
            "SELECT * FROM approval_requests WHERE task_id=? ORDER BY created_at DESC LIMIT 1",
            (task_id,),
        )
        .mappings()
        .first()
    )


def decision_for(conn: sa.Connection, request_id: str) -> sa.RowMapping | None:
    return (
        conn.exec_driver_sql("SELECT * FROM approval_decisions WHERE request_id=?", (request_id,))
        .mappings()
        .first()
    )


def waiting_for(conn: sa.Connection, task_id: str) -> dict[str, str] | None:
    row = conn.exec_driver_sql(
        "SELECT request_id FROM approval_requests WHERE task_id=? AND state='pending'",
        (task_id,),
    ).first()
    return {"approval": str(row[0])} if row else None


def _payload(row: sa.RowMapping, **extra: JsonValue) -> dict[str, JsonValue]:
    """Event payload: identifiers and digests only, never arguments (ADR-036 item 8)."""
    return {
        "request_id": row["request_id"],
        "task_id": row["task_id"],
        "operation": row["operation"],
        "namespace": row["namespace"],
        "intent_digest": row["intent_digest"],
        "expires_at": row["expires_at"],
        "rule_ids": [rule["rule_id"] for rule in json.loads(row["rules_json"])],
        **extra,
    }


def propose(
    conn: sa.Connection,
    tx: KernelTransaction,
    task: Task,
    binding: sa.RowMapping,
    request: OperationTaskRequest,
    registry: Registry,
    needed: ApprovalRequirement,
    *,
    operation_id: str,
    at: datetime,
) -> sa.RowMapping:
    """Freeze the intent and append `approval.requested` in the caller's transaction."""
    descriptor = registry.get(request.operation)
    arguments = dict(request.arguments)
    digest = argument_sha256(arguments)
    deadline = task.limits.deadline
    expires = (
        min(deadline, at + timedelta(seconds=needed.expires_after_seconds))
        if needed.expires_after_seconds is not None
        else deadline
    )
    request_id = "req-" + uuid4().hex
    conn.exec_driver_sql(
        "INSERT INTO approval_requests (request_id,task_id,namespace,supervisor_id,agent_id,"
        "runtime_id,operation,operation_version,operation_id,arguments_json,argument_sha256,"
        "idempotency_context,scope_key,intent_digest,rules_json,rules_digest,"
        "distinct_from_supervisor,created_at,expires_at,on_expiry,state,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?)",
        (
            request_id,
            task.task_id,
            request.namespace,
            binding["supervisor_id"],
            binding["agent_id"],
            binding["runtime_id"],
            request.operation,
            descriptor.cardea.version,
            operation_id,
            json.dumps(arguments),
            digest,
            request.idempotency_context,
            registry.scope_key(
                request.operation,
                request.namespace,
                request.idempotency_context or task.task_id,
                arguments,
            ),
            intent_digest(
                task_id=task.task_id,
                namespace=request.namespace,
                operation=request.operation,
                version=descriptor.cardea.version,
                arguments_sha256=digest,
                idempotency_context=request.idempotency_context,
            ),
            json.dumps(
                [
                    {
                        "source": rule.source,
                        "rule_id": rule.rule_id,
                        "reason": rule.reason,
                        "error": rule.error,
                    }
                    for rule in needed.rules
                ]
            ),
            needed.rules_digest,
            int(needed.distinct_from_supervisor),
            at.isoformat(),
            expires.isoformat(),
            needed.resolved_on_expiry(agent_run=False),
            at.isoformat(),
        ),
    )
    row = get(conn, request_id)
    tx.append_event(
        namespace=request.namespace,
        event_type="approval.requested",
        producer_id=str(binding["runtime_id"]),
        correlation_id=request_id,
        task_id=task.task_id,
        at=at,
        payload=_payload(row),
    )
    return row


# Who may approve and read


def _requesters(policy: Policy, task_id: str) -> set[str]:
    """The task's agent and runtime and those of every ancestor task."""
    excluded: set[str] = set()
    current: str | None = task_id
    for _ in range(32):
        if current is None:
            return excluded
        binding = policy.binding(current)
        excluded |= {str(binding["agent_id"]), str(binding["runtime_id"])}
        current = binding["parent_task_id"]
    raise Fault("invalid_provenance", "Task provenance cannot be established")


def holds_approve(policy: Policy, principal: str, row: sa.RowMapping) -> bool:
    """The live `approve` right: an enabled principal and matching principal and platform rows."""
    try:
        policy.principal(principal)
        policy.require(principal, row["namespace"], row["operation"], "approve")
        policy.require("platform", row["namespace"], row["operation"], "approve")
    except Fault:
        return False
    return True


def check_approver(policy: Policy, approver: str, row: sa.RowMapping) -> None:
    """ADR-036 item 2. Refusals write nothing."""
    policy.principal(approver)
    if approver in _requesters(policy, str(row["task_id"])):
        raise Fault("self_approval", "The requesting agent or runtime cannot approve", 403)
    if row["distinct_from_supervisor"] and approver == row["supervisor_id"]:
        raise Fault(
            "four_eyes_required", "This request needs an approver other than the supervisor"
        )
    if not holds_approve(policy, approver, row):
        raise Fault("approver_denied", "Current authority does not permit this approval", 403)


def readable(policy: Policy, principal: str, row: sa.RowMapping) -> bool:
    """The `approve` right for the operation, or `tasks.inspect` as the task's supervisor."""
    if principal == row["supervisor_id"]:
        try:
            policy.access(principal, str(row["task_id"]), "tasks.inspect")
            return True
        except Fault:
            pass
    return holds_approve(policy, principal, row)


def ceiling_rights(registry: Registry, row: sa.RowMapping) -> tuple[str, ...]:
    """The rights an operation task's live ceiling must hold to run the operation."""
    descriptor = registry.get(str(row["operation"]))
    return (str(row["operation"]), "objects.read", "objects.write", *descriptor.cardea.requires)


def void_reason(policy: Policy, registry: Registry, row: sa.RowMapping) -> str | None:
    """Why the request can no longer run now, if it cannot: descriptor version, live ceiling."""
    try:
        if registry.get(str(row["operation"])).cardea.version != row["operation_version"]:
            return "operation_changed"
        for right in ceiling_rights(registry, row):
            policy.ceiling(str(row["task_id"]), right)
    except Fault as error:
        return VOID_REASONS.get(error.code, "authority_lost")
    return None


# Views


def _decision_view(decision: sa.RowMapping | None) -> dict[str, Any] | None:
    if decision is None:
        return None
    return {
        "decision_id": decision["decision_id"],
        "approver_id": decision["approver_id"],
        "decision": decision["decision"],
        "reason": decision["reason"],
        "decided_at": decision["decided_at"],
    }


def summary(conn: sa.Connection, row: sa.RowMapping) -> dict[str, Any]:
    return {
        **{
            key: row[key]
            for key in (
                "request_id",
                "task_id",
                "namespace",
                "supervisor_id",
                "agent_id",
                "runtime_id",
                "operation",
                "operation_version",
                "operation_id",
                "argument_sha256",
                "idempotency_context",
                "scope_key",
                "intent_digest",
                "rules_digest",
                "created_at",
                "expires_at",
                "on_expiry",
                "state",
                "void_reason",
                "receipt_id",
                "updated_at",
            )
        },
        "distinct_from_supervisor": bool(row["distinct_from_supervisor"]),
        "rules": json.loads(row["rules_json"]),
        "decision": _decision_view(decision_for(conn, str(row["request_id"]))),
    }


def inspect(kernel: "Kernel", proof: Proof, request_id: str) -> dict[str, Any]:
    with kernel.database.read() as conn:
        principal = proof.resolve(conn)
        row = get(conn, request_id)
        if not readable(Policy(conn), principal, row):
            raise Fault("approval_not_found", "No accessible approval request", 404)
        return {
            "schema_version": "0.2",
            **summary(conn, row),
            "arguments": json.loads(row["arguments_json"]),
        }


def listing(
    kernel: "Kernel", proof: Proof, namespace: str, state: str, after: str, limit: int
) -> dict[str, Any]:
    """Requests in a namespace the caller may decide or supervises, without arguments."""
    with kernel.database.read() as conn:
        principal = proof.resolve(conn)
        policy = Policy(conn)
        listed: list[dict[str, Any]] = []
        cursor = after
        more = False
        rows = conn.exec_driver_sql(
            "SELECT * FROM approval_requests WHERE namespace=? AND state=? AND request_id>? "
            "ORDER BY request_id",
            (namespace, state, after),
        ).mappings()
        for row in rows:
            if len(listed) == limit:
                more = True
                break
            cursor = str(row["request_id"])
            if readable(policy, principal, row):
                listed.append(summary(conn, row))
    return {"schema_version": "0.2", "approvals": listed, "next": cursor if more else None}


# Deciding


def _body_sha256(approver: str, request_id: str, body: DecisionRequest) -> str:
    return sha256(
        rfc8785.dumps(
            {
                "approver_id": approver,
                "request_id": request_id,
                "decision": body.decision,
                "intent_digest": body.intent_digest,
                "reason": body.reason,
            }
        )
    ).hexdigest()


def _decided(conn: sa.Connection, request_id: str) -> dict[str, Any]:
    row = get(conn, request_id)
    return {
        "schema_version": "0.2",
        "request_id": request_id,
        "task_id": row["task_id"],
        "state": row["state"],
        "void_reason": row["void_reason"],
        "decision": _decision_view(decision_for(conn, request_id)),
    }


def wake(
    kernel: "Kernel",
    conn: sa.Connection,
    tx: KernelTransaction,
    row: sa.RowMapping,
    producer: str,
    at: datetime,
    **payload: JsonValue,
) -> None:
    """Append `approval.<id>`, wake the waiting task and enqueue its continuation once."""
    task_id, request_id = str(row["task_id"]), str(row["request_id"])
    tx.append_event(
        namespace=str(row["namespace"]),
        event_type=f"approval.{request_id}",
        producer_id=producer,
        correlation_id=request_id,
        task_id=task_id,
        at=at,
        payload=_payload(row, **payload),
    )
    task = tx.get_task(task_id)
    if task.state is TaskState.WAITING:
        tx.wake_task(task_id, expected_revision=task.revision, at=at)
    options: EnqueueOptions = {
        "workflow_name": APPROVAL_WORKFLOW,
        "queue_name": QUEUE,
        "workflow_id": continuation_id(task_id, request_id),
    }
    kernel.client.enqueue_in_transaction(conn, options, task_id, request_id)


def mark_void(
    tx: KernelTransaction,
    conn: sa.Connection,
    row: sa.RowMapping,
    reason: str,
    producer: str,
    at: datetime,
) -> None:
    """End an open request with nothing run, and say why in `approval.void`."""
    updated = conn.exec_driver_sql(
        "UPDATE approval_requests SET state='void',void_reason=?,updated_at=? "
        "WHERE request_id=? AND state IN ('pending','approved')",
        (reason, at.isoformat(), row["request_id"]),
    ).rowcount
    if updated:
        tx.append_event(
            namespace=str(row["namespace"]),
            event_type="approval.void",
            producer_id=producer,
            correlation_id=str(row["request_id"]),
            task_id=str(row["task_id"]),
            at=at,
            payload=_payload(row, reason=reason),
        )


def decide(
    kernel: "Kernel", proof: Proof, request_id: str, body: DecisionRequest
) -> dict[str, Any]:
    """The decision transaction (ADR-036 item 5): first commit wins, idempotent by ID."""
    voided: Fault | None = None
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        approver = proof.resolve(conn)
        policy = Policy(conn)
        row = get(conn, request_id)
        digest = _body_sha256(approver, request_id, body)
        prior = (
            conn.exec_driver_sql(
                "SELECT request_id,body_sha256 FROM approval_decisions WHERE decision_id=?",
                (body.decision_id,),
            )
            .mappings()
            .first()
        )
        if prior is not None:
            if prior["request_id"] != request_id or prior["body_sha256"] != digest:
                raise Fault(
                    "decision_conflict", "Decision ID was already used with another body", 409
                )
            return _decided(conn, request_id)
        check_approver(policy, approver, row)
        if row["state"] != "pending":
            raise Fault(
                "approval_closed",
                "Approval request is no longer pending",
                409,
                details={"state": row["state"]},
            )
        at = now()
        if tx.get_task(str(row["task_id"])).state in TERMINAL_TASK:
            raise Fault("task_terminal", "The task has ended; the request cannot be decided", 409)
        if at >= datetime.fromisoformat(row["expires_at"]):
            raise Fault("approval_expired", "Approval request has expired", 409)
        if body.intent_digest != row["intent_digest"]:
            raise Fault("intent_mismatch", "intent_digest does not match the stored intent", 409)
        reason = void_reason(policy, kernel.operations, row)
        if reason is not None:
            # Nothing can run: the request ends, the task learns it, no decision is recorded.
            mark_void(tx, conn, row, reason, approver, at)
            wake(kernel, conn, tx, row, approver, at, state="void")
            voided = Fault(
                "approval_void", "The request can no longer run", 409, details={"reason": reason}
            )
        else:
            conn.exec_driver_sql(
                "INSERT INTO approval_decisions VALUES (?,?,?,?,?,?,?,?)",
                (
                    body.decision_id,
                    request_id,
                    approver,
                    body.decision,
                    body.reason,
                    body.intent_digest,
                    digest,
                    at.isoformat(),
                ),
            )
            state = "approved" if body.decision == "approve" else "rejected"
            conn.exec_driver_sql(
                "UPDATE approval_requests SET state=?,updated_at=? "
                "WHERE request_id=? AND state='pending'",
                (state, at.isoformat(), request_id),
            )
            tx.append_event(
                namespace=str(row["namespace"]),
                event_type="approval.decided",
                producer_id=approver,
                correlation_id=request_id,
                task_id=str(row["task_id"]),
                at=at,
                payload=_payload(
                    row, decision_id=body.decision_id, decision=body.decision, approver_id=approver
                ),
            )
            wake(kernel, conn, tx, row, approver, at, state=state)
        kernel.fault("approval.decision_before_commit")
        result = _decided(conn, request_id)
    kernel.fault("approval.decision_after_commit")
    if voided is not None:
        raise voided
    return result


def receipt_block(conn: sa.Connection, row: sa.RowMapping) -> dict[str, JsonValue]:
    """`arguments.context.approval` of the executed receipt (ADR-036 item 6)."""
    decision = decision_for(conn, str(row["request_id"]))
    assert decision is not None
    return {
        "request_id": row["request_id"],
        "decision_id": decision["decision_id"],
        "approver_id": decision["approver_id"],
        "decision": decision["decision"],
        "decided_at": decision["decided_at"],
        "argument_sha256": row["argument_sha256"],
        "intent_digest": row["intent_digest"],
        "rule_ids": [rule["rule_id"] for rule in json.loads(row["rules_json"])],
        "rules_digest": row["rules_digest"],
    }
