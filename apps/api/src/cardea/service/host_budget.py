"""Cumulative supervisor allocations in the same SQLite transaction as host receipts."""

import sqlalchemy as sa

from cardea.service.database import Fault

TOOL_MS = 10000
OUTPUT_BYTES = 65536


def available(conn: sa.Connection, principal: str, namespace: str, *, reserve: bool) -> None:
    limit = conn.exec_driver_sql(
        "SELECT tool_ms,output_bytes,concurrency FROM host_budget_limits "
        "WHERE principal_id=? AND namespace=?",
        (principal, namespace),
    ).first()
    if limit is None:
        raise Fault("host_budget_missing", "Host owner must provision an execution allocation")
    used = conn.exec_driver_sql(
        "SELECT COALESCE(SUM(tool_ms),0),COALESCE(SUM(output_bytes),0),"
        "COALESCE(SUM(1-settled),0) FROM host_reservations WHERE principal_id=? AND namespace=?",
        (principal, namespace),
    ).one()
    agent = conn.exec_driver_sql(
        "SELECT COALESCE(SUM(tool_ms),0),COALESCE(SUM(output_bytes),0),"
        "COALESCE(SUM(1-settled),0) FROM agent_effects "
        "WHERE principal_id=? AND namespace=? AND kind='tool'",
        (principal, namespace),
    ).one()
    combined = tuple(used[i] + agent[i] for i in range(3))
    extra = (TOOL_MS, OUTPUT_BYTES, 1) if reserve else (0, 0, 0)
    if any(combined[i] + extra[i] > limit[i] for i in range(3)):
        raise Fault("host_budget_exhausted", "Host allocation has insufficient capacity", 429)


def reserve(conn: sa.Connection, task_id: str, principal: str, namespace: str) -> None:
    if conn.exec_driver_sql("SELECT 1 FROM host_reservations WHERE task_id=?", (task_id,)).first():
        raise Fault("host_already_reserved", "Existing host intent cannot be dispatched again")
    available(conn, principal, namespace, reserve=True)
    conn.exec_driver_sql(
        "INSERT INTO host_reservations VALUES (?,?,?,?,?,0,0)",
        (task_id, principal, namespace, TOOL_MS, OUTPUT_BYTES),
    )


def settle(conn: sa.Connection, task_id: str, tool_ms: int, output_bytes: int) -> None:
    if not (0 <= tool_ms <= TOOL_MS and 0 <= output_bytes <= OUTPUT_BYTES):
        raise Fault("invalid_host_usage", "Host usage exceeds its reserved allocation", 500)
    conn.exec_driver_sql(
        "UPDATE host_reservations SET tool_ms=?,output_bytes=?,settled=1 "
        "WHERE task_id=? AND settled=0",
        (tool_ms, output_bytes, task_id),
    )
