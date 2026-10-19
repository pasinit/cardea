"""Delegation bindings and allocation checks in the owning SQLite transaction."""

from typing import TYPE_CHECKING

import sqlalchemy as sa

from cardea.kernel.models import Task, TaskState
from cardea.service.database import Fault, borrow
from cardea.service.models import (
    RIGHTS,
    BaseHostTaskRequest,
    DelegationTaskRequest,
    now,
)
from cardea.service.policy import Policy
from cardea.service.profiles import for_runtime

if TYPE_CHECKING:
    from cardea.service.kernel import Kernel

TERMINAL = {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED}


def active_chain(conn: sa.Connection, task: Task) -> None:
    """Lifecycle/expiry enforcement supplements, never caches, the live ACL ceiling."""
    with borrow(conn) as tx:
        current = task
        visited: set[str] = set()
        while True:
            if current.task_id in visited or len(visited) >= 32:
                raise Fault("invalid_provenance", "Invalid ancestor chain")
            visited.add(current.task_id)
            if (
                conn.exec_driver_sql(
                    "SELECT 1 FROM control_requests WHERE task_id=? "
                    "AND kind IN ('cancel','delegation.failure')",
                    (current.task_id,),
                ).first()
                or current.state is TaskState.CANCELLED
            ):
                raise Fault("cancel_requested", "Task or ancestor was cancelled")
            if current.state in TERMINAL or now() >= current.limits.deadline:
                raise Fault("ancestor_inactive", "Task or ancestor cannot execute")
            if not current.parent_task_id:
                break
            current = tx.get_task(current.parent_task_id)


def child_request(parent_id: str, request: DelegationTaskRequest, slot: int) -> BaseHostTaskRequest:
    if not 0 <= slot < len(request.children):
        raise Fault("invalid_slot", "Unknown delegation slot")
    child = request.children[slot]
    return BaseHostTaskRequest(
        request_id=f"{parent_id}.{slot}",
        agent_id=child.agent_id,
        namespace=request.namespace,
        objective=f"{child.operation} for {parent_id} slot {slot}",
        payload=child.payload,
        operation=child.operation,
        grants=tuple(
            r
            for r in (*RIGHTS, child.operation)
            if r in request.grants and r not in {"tasks.submit", "tasks.resume"}
        ),
        parent_task_id=parent_id,
        timeout_seconds=request.timeout_seconds,
    )


def authorize_child(
    conn: sa.Connection,
    principal: str,
    runtime: str,
    parent_id: str,
    slot: int,
    request: BaseHostTaskRequest,
) -> str:
    policy = Policy(conn)
    binding = policy.ceiling(parent_id, "tasks.delegate")
    if principal != runtime or binding["runtime_id"] != runtime:
        raise Fault("delegation_binding", "Only the bound coordinator runtime may delegate")
    with borrow(conn) as tx:
        parent = tx.get_task(parent_id)
        active_chain(conn, parent)
        if not for_runtime(parent.runtime_id).owns_children:
            raise Fault("delegation_binding", "Parent does not own this installed profile")
    original = DelegationTaskRequest.model_validate_json(binding["request_json"])
    if request != child_request(parent_id, original, slot):
        raise Fault("delegation_binding", "Message does not match the admitted objective")
    # Retry of an existing slot consumes no new allocation.
    if not conn.exec_driver_sql(
        "SELECT 1 FROM delegation_links WHERE parent_id=? AND slot=?", (parent_id, slot)
    ).first():
        count = conn.exec_driver_sql(
            "SELECT COUNT(*) FROM delegation_links WHERE parent_id=?", (parent_id,)
        ).scalar_one()
        if (
            count + 2 > parent.limits.max_steps
            or (count + 1) * 10000 > parent.limits.max_tool_milliseconds
            or (count + 1) * 65536 + 32768 > parent.limits.max_output_bytes
        ):
            raise Fault("parent_budget_exhausted", "Child allocation exceeds parent ceiling", 429)
    return str(binding["supervisor_id"])


def child_dispatch(conn: sa.Connection, task_id: str) -> None:
    """Reservations are shared across siblings; admission never creates new host funds."""
    with borrow(conn) as tx:
        task = tx.get_task(task_id)
        active_chain(conn, task)
        if not task.parent_task_id:
            return
        parent = tx.get_task(task.parent_task_id)
        if not conn.exec_driver_sql(
            "SELECT 1 FROM delegation_links WHERE child_id=? AND parent_id=?",
            (task_id, task.parent_task_id),
        ).first():
            raise Fault("delegation_binding", "Missing child allocation")
        used = conn.exec_driver_sql(
            "SELECT COUNT(*) FROM host_reservations r JOIN delegation_links l "
            "ON l.child_id=r.task_id WHERE l.parent_id=? AND r.settled=0 AND r.task_id<>?",
            (task.parent_task_id, task_id),
        ).scalar_one()
        if used >= parent.limits.max_concurrent_operations:
            raise Fault("parent_concurrency_exhausted", "Parent concurrency is fully reserved", 429)


def cancel_children(kernel: "Kernel", conn: sa.Connection, parent_id: str) -> None:
    """Trusted stop path needs no now-revoked execution authority; it can only restrict."""
    from cardea.service.kernel import workflow_id

    with borrow(conn) as tx:
        for (child_id,) in conn.exec_driver_sql(
            "SELECT child_id FROM delegation_links WHERE parent_id=?", (parent_id,)
        ).fetchall():
            task = tx.get_task(child_id)
            if task.state in TERMINAL:
                continue
            signal_id = f"cancel.{parent_id}"
            if conn.exec_driver_sql(
                "SELECT 1 FROM control_requests WHERE task_id=? AND request_id=?",
                (child_id, signal_id),
            ).first():
                continue
            binding = Policy(conn).binding(child_id)
            event = tx.append_event(
                namespace=binding["namespace"],
                event_type=f"cancel.{child_id}",
                producer_id=kernel.runtime_id,
                correlation_id=signal_id,
                task_id=child_id,
                at=now(),
            )
            conn.exec_driver_sql(
                "INSERT INTO control_requests VALUES (?,?,?,?)",
                (child_id, signal_id, "cancel", event.cursor),
            )
            if task.state is not TaskState.RUNNING:
                tx.change_task(
                    child_id, TaskState.CANCELLED, expected_revision=task.revision, at=now()
                )
            kernel.client.send_in_transaction(
                conn,
                workflow_id(child_id),
                "cancel",
                topic="control",
                idempotency_key=signal_id + child_id,
            )


def parent_usage(conn: sa.Connection, parent_id: str) -> dict[str, int]:
    row = conn.exec_driver_sql(
        "SELECT COUNT(*),COALESCE(SUM(r.tool_ms),0),COALESCE(SUM(r.output_bytes),0),"
        "COALESCE(SUM(CASE WHEN r.settled=0 THEN 1 ELSE 0 END),0) "
        "FROM delegation_links l LEFT JOIN host_reservations r ON l.child_id=r.task_id "
        "WHERE l.parent_id=?",
        (parent_id,),
    ).one()
    return dict(
        zip(
            ("children", "tool_milliseconds", "output_bytes", "active_operations"), row, strict=True
        )
    )
