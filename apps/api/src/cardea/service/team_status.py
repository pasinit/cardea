"""A supervisor's read-only status of one agent group, in bounded sections (ADR-034 amendment).

`GET /v0.2/groups/{name}/status/{section}` answers one question about the team's state each:
`work` (records by state and holder), `runs` (active and recent runs, cost and outcome),
`mail` (messages between roles), `changes` (pull requests and their review state), `spend`
(every ceiling against what it has used) and `standing` (what is paused or waived).

Every section is read in one transaction and bounded: list sections take `limit` (at most
`MAX_LIMIT`) and an opaque `after` cursor, and return `next` when more remain; counts cover
the whole group. The rights are those of `GET /v0.2/groups/{name}`: the group's supervisor,
live and holding `groups.manage`; anyone else is refused as for the overview. There is no
write path here. The sections read what the kernel already stores and call no outside
service: `changes` is what the kernel's own `vcs.*` receipts record, not the host's live view,
and message bodies and record data beyond a clipped title are never returned.

A team paused by disabling its supervisor refuses this API like every other (the supervisor
has no live authority); `standing` reports what a still-readable team has paused or waived.
"""

import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa

from cardea.service import host_budget, repositories, spend
from cardea.service.auth import Proof
from cardea.service.database import Fault
from cardea.service.models import GroupSpec, now
from cardea.service.policy import Policy

if TYPE_CHECKING:
    from cardea.service.kernel import Kernel

TERMINAL = ("succeeded", "failed", "cancelled")
DEFAULT_LIMIT = 25
MAX_LIMIT = 100
MAX_HOLDERS = 100
MAX_TITLE = 200
MAX_REVIEWS = 10
DECISIVE = ("APPROVED", "CHANGES_REQUESTED")
OPEN_CHANGE, REVIEW, MERGE = "vcs.open_change", "vcs.review", "vcs.merge"


def _in(values: tuple[str, ...] | list[str]) -> str:
    return f"({','.join('?' * len(values))})" if values else "(NULL)"


def _clip(value: object) -> str | None:
    return value[:MAX_TITLE] if isinstance(value, str) else None


def _bounded(limit: int) -> int:
    return max(1, min(limit, MAX_LIMIT))


def _check(conn: sa.Connection, proof: Proof, namespace: str, name: str) -> tuple[str, GroupSpec]:
    from cardea.service.groups import _owned

    principal = proof.resolve(conn)
    _owned(conn, principal, namespace, name)
    policy = Policy(conn)
    policy.principal(principal)
    policy.require(principal, namespace, "groups.manage")
    spec_json = conn.exec_driver_sql(
        "SELECT spec_json FROM agent_groups WHERE namespace=? AND name=?", (namespace, name)
    ).scalar_one()
    return principal, GroupSpec.model_validate_json(spec_json)


def _members(conn: sa.Connection, namespace: str, name: str) -> dict[str, dict[str, Any]]:
    return {
        str(row["principal_id"]): dict(row)
        for row in conn.exec_driver_sql(
            "SELECT m.principal_id,m.pool,m.state,p.disabled FROM group_members m "
            "JOIN principals p USING(principal_id) WHERE m.namespace=? AND m.group_name=? "
            "ORDER BY m.pool,m.ordinal",
            (namespace, name),
        ).mappings()
    }


def _envelope(name: str, namespace: str, at: datetime) -> dict[str, Any]:
    return {"schema_version": "0.2", "group": name, "namespace": namespace, "as_of": at.isoformat()}


def work(
    kernel: "Kernel",
    proof: Proof,
    namespace: str,
    name: str,
    *,
    collection: str | None,
    state: str | None,
    holder: str | None,
    after: int,
    limit: int,
) -> dict[str, Any]:
    """Records of the group's collections: counts by state and by live holder, and a page."""
    limit = _bounded(limit)
    with kernel.database.read() as conn:
        _, spec = _check(conn, proof, namespace, name)
        at = now()
        if collection is not None and collection not in spec.collections:
            raise Fault("collection_not_found", "No accessible collection", 404)
        names = (collection,) if collection else spec.collections
        members = _members(conn, namespace, name)
        by_state: dict[str, dict[str, int]] = {}
        for item in names:
            counts = {
                str(s) if s is not None else "null": int(n)
                for s, n in conn.exec_driver_sql(
                    "SELECT state,COUNT(*) FROM records WHERE namespace=? AND collection=? "
                    "GROUP BY state ORDER BY state",
                    (namespace, item),
                )
            }
            by_state[item] = counts
        live = f"collection IN {_in(names)} AND holder IS NOT NULL AND lease_expires_at>?"
        holders = conn.exec_driver_sql(
            f"SELECT holder,COUNT(*) FROM records WHERE namespace=? AND {live} "
            "GROUP BY holder ORDER BY holder LIMIT ?",
            (namespace, *names, at.isoformat(), MAX_HOLDERS + 1),
        ).all()
        clauses = [f"namespace=? AND collection IN {_in(names)} AND seq>?"]
        values: list[object] = [namespace, *names, after]
        if state is not None:
            clauses.append("state=?")
            values.append(state)
        if holder is not None:
            clauses += ["holder=?", "lease_expires_at>?"]
            values += [holder, at.isoformat()]
        rows = (
            conn.exec_driver_sql(
                "SELECT seq,collection,record_id,state,holder,claim_epoch,lease_expires_at,"
                "updated_at,json_extract(data_json,'$.title') AS title FROM records WHERE "
                + " AND ".join(clauses)
                + " ORDER BY seq LIMIT ?",
                (*values, limit + 1),
            )
            .mappings()
            .all()
        )
    page = rows[:limit]
    return {
        **_envelope(name, namespace, at),
        "by_state": by_state,
        "by_holder": [
            {"holder": h, "pool": (members.get(str(h)) or {}).get("pool"), "count": int(n)}
            for h, n in holders[:MAX_HOLDERS]
        ],
        "by_holder_truncated": max(0, len(holders) - MAX_HOLDERS),
        "items": [
            {
                "collection": r["collection"],
                "record_id": r["record_id"],
                "title": _clip(r["title"]),
                "state": r["state"],
                "holder": r["holder"]
                if r["lease_expires_at"] and r["lease_expires_at"] > at.isoformat()
                else None,
                "claim_epoch": r["claim_epoch"],
                "lease_expires_at": r["lease_expires_at"],
                "updated_at": r["updated_at"],
            }
            for r in page
        ],
        "next": page[-1]["seq"] if len(rows) > limit else None,
    }


def runs(
    kernel: "Kernel",
    proof: Proof,
    namespace: str,
    name: str,
    *,
    active: bool,
    after: str,
    limit: int,
) -> dict[str, Any]:
    """The members' runs, newest first, each with its outcome and what it cost."""
    limit = _bounded(limit)
    with kernel.database.read() as conn:
        _check(conn, proof, namespace, name)
        at = now()
        created = "json_extract(t.snapshot,'$.created_at')"
        scope = (
            "FROM task_bindings b JOIN tasks t USING(task_id) JOIN group_members m "
            "ON m.principal_id=b.agent_id AND m.namespace=b.namespace AND m.group_name=? "
            "WHERE b.namespace=?"
        )
        clauses, values = [], []
        if active:
            clauses.append(f"t.state NOT IN {_in(TERMINAL)}")
            values += list(TERMINAL)
        if after:
            start = conn.exec_driver_sql(
                f"SELECT {created} {scope} AND b.task_id=?", (name, namespace, after)
            ).first()
            if start is None:
                raise Fault("invalid_cursor", "Cursor does not name a run of this group", 400)
            clauses.append(f"({created},b.task_id) < (?,?)")
            values += [str(start[0]), after]
        where = "".join(f" AND {c}" for c in clauses)
        counts = {
            str(s): int(n)
            for s, n in conn.exec_driver_sql(
                f"SELECT t.state,COUNT(*) {scope} GROUP BY t.state ORDER BY t.state",
                (name, namespace),
            )
        }
        rows = (
            conn.exec_driver_sql(
                "SELECT b.task_id,b.agent_id,m.pool,t.state,"
                f"{created} AS created_at,json_extract(t.snapshot,'$.updated_at') AS updated_at,"
                "json_extract(t.snapshot,'$.failure.code') AS failure_code "
                f"{scope}{where} ORDER BY {created} DESC, b.task_id DESC LIMIT ?",
                (name, namespace, *values, limit + 1),
            )
            .mappings()
            .all()
        )
        items = []
        for row in rows[:limit]:
            task_id = str(row["task_id"])
            cost, _ = spend.run_cost(conn, task_id)
            held = conn.exec_driver_sql(
                "SELECT state,reserved_usd FROM spend_reservations WHERE task_id=?", (task_id,)
            ).first()
            finished = row["state"] in TERMINAL
            items.append(
                {
                    "task_id": task_id,
                    "member": row["agent_id"],
                    "pool": row["pool"],
                    "state": row["state"],
                    "outcome": row["state"] if finished else None,
                    "failure_code": row["failure_code"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "cost_usd": cost,
                    "reserved_usd": held[1] if held else None,
                    "reservation": held[0] if held else None,
                }
            )
    return {
        **_envelope(name, namespace, at),
        "by_state": counts,
        "items": items,
        "next": items[-1]["task_id"] if len(rows) > limit else None,
    }


def _role(
    address: str, name: str, members: dict[str, dict[str, Any]], supervisor: str
) -> str | None:
    """A sender's or recipient's role: its pool, or `supervisor`; None when it has neither."""
    if address in members:
        return str(members[address]["pool"])
    if address.startswith(f"{name}."):
        return address[len(name) + 1 :]
    return "supervisor" if address == supervisor else None


def mail(
    kernel: "Kernel",
    proof: Proof,
    namespace: str,
    name: str,
    *,
    after: str,
    limit: int,
) -> dict[str, Any]:
    """Messages sent from or to the group's roles, newest first; delivery counts, no bodies."""
    limit = _bounded(limit)
    with kernel.database.read() as conn:
        supervisor, _ = _check(conn, proof, namespace, name)
        at = now()
        members = _members(conn, namespace, name)
        size = len(name) + 1
        scope = (
            "namespace=? AND (sender_id=? OR substr(sender_id,1,?)=? OR to_address=? "
            "OR substr(to_address,1,?)=?)"
        )
        scoped = (namespace, name, size, f"{name}.", name, size, f"{name}.")
        values: tuple[object, ...] = ()
        clause = ""
        if after:
            start = conn.exec_driver_sql(
                f"SELECT created_at FROM mailbox_messages WHERE {scope} AND message_id=?",
                (*scoped, after),
            ).first()
            if start is None:
                raise Fault("invalid_cursor", "Cursor does not name a message of this group", 400)
            clause = " AND (created_at,message_id) < (?,?)"
            values = (str(start[0]), after)
        total = int(
            conn.exec_driver_sql(
                f"SELECT COUNT(*) FROM mailbox_messages WHERE {scope}", scoped
            ).scalar_one()
        )
        rows = (
            conn.exec_driver_sql(
                "SELECT message_id,kind,sender_id,to_kind,to_address,created_at,body_bytes "
                f"FROM mailbox_messages WHERE {scope}{clause} "
                "ORDER BY created_at DESC, message_id DESC LIMIT ?",
                (*scoped, *values, limit + 1),
            )
            .mappings()
            .all()
        )
        items = []
        for row in rows[:limit]:
            delivery = {
                str(s): int(n)
                for s, n in conn.exec_driver_sql(
                    "SELECT state,COUNT(*) FROM mailbox_deliveries WHERE message_id=? "
                    "GROUP BY state ORDER BY state",
                    (row["message_id"],),
                )
            }
            sender, address = str(row["sender_id"]), str(row["to_address"])
            items.append(
                {
                    "message_id": row["message_id"],
                    "kind": row["kind"],
                    "from": {"address": sender, "role": _role(sender, name, members, supervisor)},
                    "to": {
                        "kind": row["to_kind"],
                        "address": address,
                        "role": _role(address, name, members, supervisor),
                    },
                    "sent_at": row["created_at"],
                    "body_bytes": row["body_bytes"],
                    "deliveries": delivery,
                }
            )
    return {
        **_envelope(name, namespace, at),
        "total": total,
        "items": items,
        "next": items[-1]["message_id"] if len(rows) > limit else None,
    }


def _receipts(operation: str) -> str:
    """A succeeded call of `operation` in the namespace, as the kernel recorded it."""
    return (
        f"state='succeeded' AND json_extract(snapshot,'$.request.name')='{operation}' "
        "AND json_extract(snapshot,'$.request.resource.namespace')=?"
    )


ARGUMENT = "json_extract(snapshot,'$.request.arguments.arguments.{}')"
CALLER = "json_extract(snapshot,'$.request.arguments.context.{}')"
CREATED = "json_extract(snapshot,'$.created_at')"


def _merge_of(alias: str) -> str:
    """SQL: a `vcs.merge` receipt for the pull request of the opening receipt `alias`."""
    return (
        "EXISTS (SELECT 1 FROM operations WHERE "
        + _receipts(MERGE)
        + f" AND {ARGUMENT.format('repository')}={alias}.repository"
        + f" AND {ARGUMENT.format('number')}={alias}.number)"
    )


def _cursor(after: str) -> tuple[str, str, int]:
    try:
        stamp, repository, number = after.split("|")
        return stamp, repository, int(number)
    except ValueError:
        raise Fault("invalid_cursor", "Cursor does not name a pull request", 400) from None


def changes(
    kernel: "Kernel",
    proof: Proof,
    namespace: str,
    name: str,
    *,
    state: str,
    after: str,
    limit: int,
) -> dict[str, Any]:
    """Pull requests the group's members opened, newest first, with their review state.

    Built from the kernel's `vcs.open_change`, `vcs.review` and `vcs.merge` receipts; a
    pull request the host closed without a merge still shows as open.
    """
    limit = _bounded(limit)
    with kernel.database.read() as conn:
        _check(conn, proof, namespace, name)
        at = now()
        opened = (
            f"SELECT {ARGUMENT.format('repository')} AS repository,"
            f"json_extract(snapshot,'$.result.output.number') AS number,"
            f"MIN({CREATED}) AS first_at,receipt_id,"
            f"{ARGUMENT.format('title')} AS title,{CALLER.format('agent_id')} AS agent_id,"
            f"json_extract(snapshot,'$.result.output.url') AS url FROM operations WHERE "
            f"{_receipts(OPEN_CHANGE)} AND {CALLER.format('group')}=? "
            "GROUP BY repository,number"
        )
        base = (namespace, name)
        merged = _merge_of("o")
        counts = {"open": 0, "merged": 0}
        for row in conn.exec_driver_sql(
            f"SELECT {merged} AS merged,COUNT(*) FROM ({opened}) o GROUP BY merged",
            (namespace, *base),
        ):
            counts["merged" if row[0] else "open"] = int(row[1])
        clauses = []
        if state in ("open", "merged"):
            clauses.append(("" if state == "merged" else "NOT ") + merged)
        if after:
            clauses.append("(o.first_at,o.repository,o.number) < (?,?,?)")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = (
            conn.exec_driver_sql(
                f"SELECT * FROM ({opened}) o{where} ORDER BY o.first_at DESC, o.repository DESC, "
                "o.number DESC LIMIT ?",
                (*base, *_params(state, namespace, after), limit + 1),
            )
            .mappings()
            .all()
        )
        items = [_change(conn, namespace, row) for row in rows[:limit]]
    return {
        **_envelope(name, namespace, at),
        "totals": counts,
        "items": items,
        "next": _next(rows[limit - 1]) if len(rows) > limit else None,
    }


def _params(state: str, namespace: str, after: str) -> list[object]:
    found: list[object] = []
    if state in ("open", "merged"):
        found.append(namespace)
    if after:
        found += list(_cursor(after))
    return found


def _next(row: sa.RowMapping) -> str:
    return f"{row['first_at']}|{row['repository']}|{row['number']}"


def _change(conn: sa.Connection, namespace: str, row: sa.RowMapping) -> dict[str, Any]:
    same = f" AND {ARGUMENT.format('repository')}=? AND {ARGUMENT.format('number')}=?"
    key = (namespace, row["repository"], row["number"])
    reviews = [
        {
            "review_id": r["review_id"],
            "reviewer": r["reviewer"],
            "state": r["state"],
            "commit_id": r["commit_id"],
            "at": r["at"],
        }
        for r in conn.exec_driver_sql(
            "SELECT json_extract(snapshot,'$.result.output.review_id') AS review_id,"
            f"{CALLER.format('agent_id')} AS reviewer,"
            "json_extract(snapshot,'$.result.output.state') AS state,"
            "json_extract(snapshot,'$.result.output.commit_id') AS commit_id,"
            f"{CREATED} AS at FROM operations WHERE {_receipts(REVIEW)}{same} "
            f"ORDER BY {CREATED} DESC, receipt_id DESC LIMIT ?",
            (*key, MAX_REVIEWS),
        ).mappings()
    ]
    decisive = conn.exec_driver_sql(
        "SELECT json_extract(snapshot,'$.result.output.state') FROM operations "
        f"WHERE {_receipts(REVIEW)}{same} AND json_extract(snapshot,'$.result.output.state') "
        f"IN {_in(DECISIVE)} ORDER BY {CREATED} DESC, receipt_id DESC LIMIT 1",
        (*key, *DECISIVE),
    ).scalar()
    merge = (
        conn.exec_driver_sql(
            f"SELECT {CALLER.format('agent_id')} AS agent_id,{CREATED} AS at,"
            "json_extract(snapshot,'$.result.output.sha') AS sha FROM operations "
            f"WHERE {_receipts(MERGE)}{same} ORDER BY {CREATED} LIMIT 1",
            key,
        )
        .mappings()
        .first()
    )
    return {
        "repository": row["repository"],
        "number": row["number"],
        "title": _clip(row["title"]),
        "url": row["url"],
        "state": "merged" if merge else "open",
        "opened_by": row["agent_id"],
        "opened_at": row["first_at"],
        "review_state": decisive or "none",
        "reviews": reviews,
        "merged_by": merge["agent_id"] if merge else None,
        "merged_at": merge["at"] if merge else None,
        "merge_sha": merge["sha"] if merge else None,
    }


def _ceiling(limit: float, used: float) -> dict[str, float]:
    return {"limit": limit, "used": used, "remaining": max(0, limit - used)}


def ceilings(kernel: "Kernel", proof: Proof, namespace: str, name: str) -> dict[str, Any]:
    """Spend against every ceiling: the group's and each pool's window, and the host allocation."""
    with kernel.database.read() as conn:
        principal, spec = _check(conn, proof, namespace, name)
        at = now()
        accounts = []
        for label, account, pool in (
            ("group", spec.spend, None),
            *((f"pool:{p.name}", p.spend, p.name) for p in spec.pools),
        ):
            if account is None:
                continue
            span = spend.window(account.window, account.timezone, at)
            usd = runs_ = None
            if account.usd is not None:
                charged = round(spend.used(conn, namespace, name, key=span.key, pool=pool), 6)
                usd = _ceiling(account.usd, charged)
            if account.runs is not None:
                admitted = spend.runs_used(conn, namespace, name, key=span.key, pool=pool)
                runs_ = _ceiling(account.runs, admitted)
            accounts.append(
                {
                    "account": label,
                    "window": span.key,
                    "window_ends_at": span.end.isoformat(),
                    "usd": usd,
                    "runs": runs_,
                    "at_ceiling": any(c is not None and c["remaining"] <= 0 for c in (usd, runs_)),
                }
            )
        allocation = None
        limit = host_budget.limits(conn, principal, namespace)
        if limit is not None:
            held = host_budget.used(conn, principal, namespace)
            parts = {
                key: _ceiling(limit[i], held[i])
                for i, key in enumerate(("tool_ms", "output_bytes", "concurrency"))
            }
            allocation = {**parts, "at_ceiling": any(p["remaining"] <= 0 for p in parts.values())}
    return {**_envelope(name, namespace, at), "accounts": accounts, "host_allocation": allocation}


def standing(kernel: "Kernel", proof: Proof, namespace: str, name: str) -> dict[str, Any]:
    """What is paused or waived: the group's state, disabled and draining members, and the
    host-rule waivers on the repositories the supervisor holds a `repo:` grant for."""
    with kernel.database.read() as conn:
        principal, _ = _check(conn, proof, namespace, name)
        at = now()
        state = conn.exec_driver_sql(
            "SELECT state FROM agent_groups WHERE namespace=? AND name=?", (namespace, name)
        ).scalar_one()
        members = _members(conn, namespace, name)
        granted = {
            str(op)[len("repo:") :]
            for (op,) in conn.exec_driver_sql(
                "SELECT operation FROM policies WHERE principal_id=? AND namespace=? "
                "AND mode='allow' AND expires_at>? AND operation LIKE 'repo:%'",
                (principal, namespace, time.time()),
            )
        }
    active = [m for m in members.values() if m["state"] == "active"]
    disabled = [str(p) for p, m in members.items() if m["disabled"]]
    waivers: list[dict[str, Any]] = []
    registry = "not_configured"
    try:
        loaded = repositories.load()
    except Fault:
        loaded, registry = None, "invalid"
    if loaded is not None:
        registry = "ok"
        waivers = [
            {"repository": alias, "reason": entry.waiver_reason}
            for alias, entry in sorted(loaded.repositories.items())
            if entry.host_rules == "waived" and alias in granted
        ]
    return {
        **_envelope(name, namespace, at),
        "group_state": state,
        "paused": state != "active" or (bool(active) and all(m["disabled"] for m in active)),
        "members_disabled": disabled,
        "members_draining": [str(p) for p, m in members.items() if m["state"] == "draining"],
        "registry": registry,
        "waived": waivers,
    }
