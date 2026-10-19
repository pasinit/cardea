"""Model allocations and effect reservations share the Cardea receipt transaction."""

import sqlalchemy as sa

from cardea.service.database import Fault

INPUT_RESERVE = 32768


def available(conn: sa.Connection, principal: str, namespace: str, tokens: int = 0) -> None:
    limit = conn.exec_driver_sql(
        "SELECT calls,tokens FROM model_budget_limits WHERE principal_id=? AND namespace=?",
        (principal, namespace),
    ).first()
    if limit is None:
        raise Fault("model_budget_missing", "Host owner must provision a model allocation")
    used = conn.exec_driver_sql(
        "SELECT COALESCE(SUM(calls),0),COALESCE(SUM(tokens),0) FROM agent_effects "
        "WHERE principal_id=? AND namespace=?",
        (principal, namespace),
    ).one()
    if used[0] + int(tokens > 0) > limit[0] or used[1] + tokens > limit[1]:
        raise Fault("model_budget_exhausted", "Model allocation has insufficient capacity", 429)
