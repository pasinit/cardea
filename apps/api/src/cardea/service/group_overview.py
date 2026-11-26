"""A supervisor's overview of one agent group (ADR-034 amendment, 2026-11-16).

Read-only and generic: it aggregates what groups, records, claims, mailboxes, approvals,
spend accounts and the journal already store, in one read transaction so every count is
taken at the same moment. The rights are those of `GET /v0.2/groups/{name}`. Every list
is bounded and reports how many entries it left out.
"""

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa

from cardea.service import spend
from cardea.service.auth import Proof
from cardea.service.journal import APPROVALS
from cardea.service.models import GroupSpec, now
from cardea.service.policy import Policy

if TYPE_CHECKING:
    from cardea.service.kernel import Kernel

TERMINAL = ("succeeded", "failed", "cancelled")
MAX_CLAIMS_PER_MEMBER = 8
MAX_CLAIMED_PER_COLLECTION = 20
MAX_ADDRESSES = 50
MAX_APPROVALS = 20
MAX_EXPECTATIONS = 20
DEFAULT_EVENTS = 20
MAX_EVENTS = 100


def _in(values: list[str]) -> str:
    return f"({','.join('?' * len(values))})" if values else "(NULL)"


def _age(at: datetime, stamp: str | None) -> int | None:
    if not stamp:
        return None
    return max(0, int((at - datetime.fromisoformat(stamp)).total_seconds()))


def _members(conn: sa.Connection, namespace: str, group: str, at: datetime) -> list[dict[str, Any]]:
    members = []
    for row in conn.exec_driver_sql(
        "SELECT m.principal_id,m.pool,m.ordinal,m.state,p.disabled FROM group_members m "
        "JOIN principals p USING(principal_id) WHERE m.namespace=? AND m.group_name=? "
        "ORDER BY m.pool,m.ordinal",
        (namespace, group),
    ).mappings():
        principal = str(row["principal_id"])
        active = conn.exec_driver_sql(
            "SELECT b.task_id FROM task_bindings b JOIN tasks t USING(task_id) "
            f"WHERE b.agent_id=? AND t.state NOT IN {TERMINAL} ORDER BY b.rowid DESC LIMIT 1",
            (principal,),
        ).scalar()
        last = (
            conn.exec_driver_sql(
                "SELECT b.task_id,t.state,json_extract(t.snapshot,'$.updated_at') AS at,"
                "json_extract(t.snapshot,'$.failure.code') AS code FROM task_bindings b "
                f"JOIN tasks t USING(task_id) WHERE b.agent_id=? AND t.state IN {TERMINAL} "
                "ORDER BY json_extract(t.snapshot,'$.updated_at') DESC, b.rowid DESC LIMIT 1",
                (principal,),
            )
            .mappings()
            .first()
        )
        claims = [
            f"{c}/{r}"
            for c, r in conn.exec_driver_sql(
                "SELECT collection,record_id FROM records WHERE namespace=? AND holder=? "
                "AND lease_expires_at>? ORDER BY seq LIMIT ?",
                (namespace, principal, at.isoformat(), MAX_CLAIMS_PER_MEMBER + 1),
            )
        ]
        held = int(
            conn.exec_driver_sql(
                "SELECT COUNT(*) FROM records WHERE namespace=? AND holder=? "
                "AND lease_expires_at>?",
                (namespace, principal, at.isoformat()),
            ).scalar_one()
        )
        members.append(
            {
                "principal_id": principal,
                "pool": row["pool"],
                "ordinal": row["ordinal"],
                "state": row["state"],
                "disabled": bool(row["disabled"]),
                "active_task_id": active,
                "last_run": {
                    "task_id": last["task_id"],
                    "outcome": last["state"],
                    "failure_code": last["code"],
                    "finished_at": last["at"],
                }
                if last
                else None,
                "claims": claims[:MAX_CLAIMS_PER_MEMBER],
                "claims_truncated": max(0, held - MAX_CLAIMS_PER_MEMBER),
            }
        )
    return members


def _collections(
    conn: sa.Connection, namespace: str, names: tuple[str, ...], at: datetime
) -> list[dict[str, Any]]:
    listed = []
    for name in names:
        exists = conn.exec_driver_sql(
            "SELECT 1 FROM collections WHERE namespace=? AND name=?", (namespace, name)
        ).first()
        if exists is None:
            listed.append(
                {
                    "name": name,
                    "status": "unavailable",
                    "by_state": {},
                    "total": 0,
                    "claimed": [],
                    "truncated": 0,
                }
            )
            continue
        by_state = {
            str(state) if state is not None else "null": int(count)
            for state, count in conn.exec_driver_sql(
                "SELECT state,COUNT(*) FROM records WHERE namespace=? AND collection=? "
                "GROUP BY state ORDER BY state",
                (namespace, name),
            )
        }
        live = "namespace=? AND collection=? AND holder IS NOT NULL AND lease_expires_at>?"
        values = (namespace, name, at.isoformat())
        claimed = [
            {
                "record_id": row["record_id"],
                "title": row["title"] if isinstance(row["title"], str) else None,
                "state": row["state"],
                "holder": row["holder"],
                "claim_epoch": row["claim_epoch"],
                "claimed_at": row["claimed_at"],
                "age_seconds": _age(at, row["claimed_at"]),
                "lease_expires_at": row["lease_expires_at"],
            }
            for row in conn.exec_driver_sql(
                "SELECT r.record_id,r.state,r.holder,r.claim_epoch,r.lease_expires_at,"
                "json_extract(r.data_json,'$.title') AS title,"
                "(SELECT MAX(e.at) FROM record_events e WHERE e.namespace=r.namespace "
                "AND e.collection=r.collection AND e.record_id=r.record_id "
                "AND e.kind IN ('claimed','transitioned')) AS claimed_at "
                "FROM records r WHERE r.namespace=? AND r.collection=? "
                "AND r.holder IS NOT NULL AND r.lease_expires_at>? "
                "ORDER BY r.seq LIMIT ?",
                (*values, MAX_CLAIMED_PER_COLLECTION),
            ).mappings()
        ]
        count = int(
            conn.exec_driver_sql(f"SELECT COUNT(*) FROM records WHERE {live}", values).scalar_one()
        )
        listed.append(
            {
                "name": name,
                "status": "ok",
                "by_state": by_state,
                "total": sum(by_state.values()),
                "claimed": claimed,
                "truncated": count - len(claimed),
            }
        )
    return listed


def _mailbox(
    conn: sa.Connection, namespace: str, members: list[str], pools: list[str]
) -> dict[str, Any]:
    rows = conn.exec_driver_sql(
        "SELECT COALESCE(recipient_id,pool) AS address,"
        "SUM(state='pending'),SUM(state='leased'),SUM(state='dead'),SUM(state='consumed') "
        f"FROM mailbox_deliveries WHERE namespace=? AND (recipient_id IN {_in(members)} "
        f"OR (recipient_id IS NULL AND pool IN {_in(pools)})) "
        "GROUP BY address ORDER BY address LIMIT ?",
        (namespace, *members, *pools, MAX_ADDRESSES + 1),
    ).all()
    return {
        "addresses": [
            {
                "address": address,
                "pending": int(pending),
                "in_flight": int(leased),
                "dead": int(dead),
                "consumed": int(consumed),
            }
            for address, pending, leased, dead, consumed in rows[:MAX_ADDRESSES]
        ],
        "truncated": max(0, len(rows) - MAX_ADDRESSES),
    }


MEMBER_TASKS = (
    "SELECT task_id FROM task_bindings WHERE agent_id IN "
    "(SELECT principal_id FROM group_members WHERE namespace=? AND group_name=?)"
)


def _approvals(conn: sa.Connection, namespace: str, group: str) -> dict[str, Any]:
    where = f"state IN ('pending','approved') AND task_id IN ({MEMBER_TASKS})"
    rows = conn.exec_driver_sql(
        "SELECT request_id,task_id,operation,approval_resource,state,created_at,expires_at "
        f"FROM approval_requests WHERE {where} ORDER BY created_at LIMIT ?",
        (namespace, group, MAX_APPROVALS),
    ).mappings()
    items = [
        {
            "request_id": r["request_id"],
            "task_id": r["task_id"],
            "operation": r["operation"],
            "resource": r["approval_resource"],
            "state": r["state"],
            "created_at": r["created_at"],
            "expires_at": r["expires_at"],
        }
        for r in rows
    ]
    total = int(
        conn.exec_driver_sql(
            f"SELECT COUNT(*) FROM approval_requests WHERE {where}", (namespace, group)
        ).scalar_one()
    )
    return {"items": items, "truncated": total - len(items)}


def _spend(
    conn: sa.Connection, namespace: str, group: str, spec: GroupSpec, at: datetime
) -> dict[str, Any]:
    accounts = []
    for label, account, pool in (
        ("group", spec.spend, None),
        *((f"pool:{p.name}", p.spend, p.name) for p in spec.pools),
    ):
        if account is None:
            continue
        span = spend.window(account.window, account.timezone, at)
        accounts.append(
            {
                "account": label,
                "usd": account.usd,
                "window": span.key,
                "window_ends_at": span.end.isoformat(),
                "used_usd": round(spend.used(conn, namespace, group, key=span.key, pool=pool), 6),
                **(
                    {
                        "runs": account.runs,
                        "used_runs": spend.runs_used(
                            conn, namespace, group, key=span.key, pool=pool
                        ),
                    }
                    if account.runs is not None
                    else {}
                ),
            }
        )
    rows = (
        conn.exec_driver_sql(
            "SELECT pool,window_key,outcome,observed,reported_at FROM expect_window_reports "
            "WHERE namespace=? AND group_name=? ORDER BY reported_at DESC LIMIT ?",
            (namespace, group, MAX_EXPECTATIONS + 1),
        )
        .mappings()
        .all()
    )
    return {
        "accounts": accounts,
        "expectations": [dict(r) for r in rows[:MAX_EXPECTATIONS]],
        "expectations_truncated": max(0, len(rows) - MAX_EXPECTATIONS),
    }


def _journal(
    conn: sa.Connection, namespace: str, group: str, members: list[str], limit: int
) -> dict[str, Any]:
    # The domain events journal triggers see (ADR-034 decision 6), not the bookkeeping
    # of objects and task transitions behind every run.
    exposed = sorted({*APPROVALS, "record.claim_expired"})
    where = (
        f"namespace=? AND (event_type LIKE 'cardea.%' OR event_type IN {_in(exposed)}) "
        f"AND (producer_id IN {_in(members)} OR task_id IN ({MEMBER_TASKS}))"
    )
    values = (namespace, *exposed, *members, namespace, group)
    rows = conn.exec_driver_sql(
        "SELECT cursor,event_type,producer_id,task_id,occurred_at,payload FROM events "
        f"WHERE {where} ORDER BY cursor DESC LIMIT ?",
        (*values, limit),
    ).mappings()
    events = []
    for row in rows:
        payload = json.loads(row["payload"]) if row["payload"] else {}
        subject = payload.get("subject") if isinstance(payload, dict) else None
        events.append(
            {
                "cursor": row["cursor"],
                "type": row["event_type"],
                "producer_id": row["producer_id"],
                "task_id": row["task_id"],
                "subject": subject if isinstance(subject, str) else None,
                "at": row["occurred_at"],
            }
        )
    total = int(
        conn.exec_driver_sql(f"SELECT COUNT(*) FROM events WHERE {where}", values).scalar_one()
    )
    return {"events": events, "truncated": total - len(events)}


def overview(
    kernel: "Kernel", proof: Proof, namespace: str, name: str, events: int = DEFAULT_EVENTS
) -> dict[str, Any]:
    """The group's overview from one read transaction, for its supervisor."""
    from cardea.service.groups import _owned

    limit = max(0, min(events, MAX_EVENTS))
    with kernel.database.read() as conn:
        principal = proof.resolve(conn, allow_disabled=True)
        _owned(conn, principal, namespace, name)
        Policy(conn).require_paused_read(principal, namespace, "groups.manage")
        at = now()
        last = "SELECT COALESCE(MAX(cursor),0) FROM events"
        cursor = int(conn.exec_driver_sql(last).scalar_one())
        group = (
            conn.exec_driver_sql(
                "SELECT * FROM agent_groups WHERE namespace=? AND name=?", (namespace, name)
            )
            .mappings()
            .one()
        )
        spec = GroupSpec.model_validate_json(group["spec_json"])
        members = _members(conn, namespace, name, at)
        principals = [m["principal_id"] for m in members]
        pools = [
            {
                "name": pool.name,
                "replicas": pool.replicas,
                "members": [m for m in members if m["pool"] == pool.name],
            }
            for pool in spec.pools
        ]
        # Members of pools the spec no longer has (draining or removed) stay visible.
        gone = sorted({str(m["pool"]) for m in members} - {p.name for p in spec.pools})
        pools += [
            {"name": g, "replicas": 0, "members": [m for m in members if m["pool"] == g]}
            for g in gone
        ]
        return {
            "schema_version": "0.2",
            "group": name,
            "namespace": namespace,
            "state": group["state"],
            "generation": group["generation"],
            "observed_generation": group["observed_generation"],
            "as_of": at.isoformat(),
            "cursor": cursor,
            "pools": pools,
            "collections": _collections(conn, namespace, tuple(spec.collections), at),
            "mailbox": _mailbox(
                conn, namespace, principals, [f"{name}.{p['name']}" for p in pools]
            ),
            "approvals": _approvals(conn, namespace, name),
            "spend": _spend(conn, namespace, name, spec, at),
            "journal": _journal(conn, namespace, name, principals, limit),
        }
