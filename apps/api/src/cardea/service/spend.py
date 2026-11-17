"""Group and pool spend accounts and per-run cost (ADR-034 decision 5.3, slice 9).

A spend account is the `spend` of a group or a pool: a ceiling in US dollars, in admitted
runs, or both, per calendar day or ISO week in a timezone. It is an ADR-015-style
reservation ledger:

- admitting a member's run reserves its `max_cost_usd`, and counts one run, against both
  accounts in the admission transaction, and is refused with `budget_exhausted` if either
  would exceed its ceiling;
- a run refused that way by a trigger mails the group's supervisor once per account and
  window (`notify_refused`); the trigger's next slot or event after the window turns
  admits again, so a ceiling is a hard stop until the next day or week;
- once the run is terminal its reservation settles to the cost its attempts reported
  (ADR-033 metering), which releases the unused part; if any attempt's cost is unknown
  (a lost attempt, or a harness that does not meter), the full reservation stays.

Settlement is lazy: it happens when an account is next read, so no terminal path of a run
needs its own hook. This is a metered bound, not a provider invoice.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

import sqlalchemy as sa

from cardea.service.database import Fault
from cardea.service.models import AgentRunRequest, GroupSpec, GroupSpend, TaskRequest
from cardea.service.schedule import to_utc, zone

TERMINAL = ("succeeded", "failed", "cancelled")
Window = Literal["day", "week"]


@dataclass(frozen=True)
class Span:
    """One calendar window: `[start, end)` in UTC and its stable key."""

    key: str
    start: datetime
    end: datetime


def window(kind: Window, timezone: str, at: datetime) -> Span:
    """The day or ISO week (from Monday) containing `at`, on the wall clock of `timezone`.

    Boundaries are local midnights mapped to UTC with the trigger rules (ADR-024), so a
    midnight inside a DST gap starts at the end of the gap.
    """
    tz = zone(timezone)
    local = at.astimezone(tz).replace(tzinfo=None)
    day = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if kind == "day":
        first, after, key = day, day + timedelta(days=1), f"{day:%Y-%m-%d}"
    else:
        first = day - timedelta(days=day.weekday())
        after = first + timedelta(days=7)
        year, week, _ = first.isocalendar()
        key = f"{year}-W{week:02d}"
    return Span(f"{kind}:{timezone}:{key}", to_utc(first, tz), to_utc(after, tz))


def previous(kind: Window, timezone: str, at: datetime) -> Span:
    """The window that ended most recently at or before `at`."""
    current = window(kind, timezone, at)
    return window(kind, timezone, current.start - timedelta(seconds=1))


def recorded_attempts(conn: sa.Connection, task_id: str) -> int:
    """How many attempts the run started, from its latest checkpoint."""
    snapshot = conn.exec_driver_sql(
        "SELECT snapshot FROM tasks WHERE task_id=?", (task_id,)
    ).scalar_one()
    checkpoint = json.loads(snapshot).get("checkpoint") or {}
    attempt = (checkpoint.get("state") or {}).get("attempt", 0)
    return attempt if isinstance(attempt, int) else 0


def run_cost(conn: sa.Connection, task_id: str) -> tuple[float | None, list[dict[str, Any]]]:
    """The run's total reported cost (None when any attempt's is unknown) and each attempt."""
    rows = conn.exec_driver_sql(
        "SELECT attempt,cost_usd FROM run_attempt_costs WHERE task_id=? ORDER BY attempt",
        (task_id,),
    ).all()
    attempts = [{"attempt": int(a), "cost_usd": c} for a, c in rows]
    started = recorded_attempts(conn, task_id)
    known = {int(a) for a, c in rows if c is not None}
    if not started or any(n not in known for n in range(1, started + 1)):
        return None, attempts
    return round(sum(float(c) for _, c in rows if c is not None), 6), attempts


def record_attempt(
    conn: sa.Connection, task_id: str, attempt: int, cost_usd: float | None, at: datetime
) -> None:
    """Record what one finished attempt reported, in its finishing transaction."""
    conn.exec_driver_sql(
        "INSERT INTO run_attempt_costs VALUES (?,?,?,?) ON CONFLICT(task_id, attempt) DO NOTHING",
        (task_id, attempt, cost_usd if cost_usd is None else max(0.0, cost_usd), at.isoformat()),
    )


def settle(conn: sa.Connection, namespace: str, group: str, at: datetime) -> None:
    """Settle the reservations of the group's runs that have ended."""
    rows = conn.exec_driver_sql(
        "SELECT r.task_id FROM spend_reservations r JOIN tasks t USING(task_id) "
        "WHERE r.namespace=? AND r.group_name=? AND r.state='reserved' "
        f"AND t.state IN {TERMINAL}",
        (namespace, group),
    ).all()
    for (task_id,) in rows:
        cost, _ = run_cost(conn, str(task_id))
        conn.exec_driver_sql(
            "UPDATE spend_reservations SET state=?,cost_usd=?,settled_at=? "
            "WHERE task_id=? AND state='reserved'",
            ("unknown" if cost is None else "settled", cost, at.isoformat(), task_id),
        )


# A settled run is charged what it reported. An open or unknown run is charged its
# reservation, or what its finished attempts already reported when that is more: retries
# and approval continuations can spend beyond the one admission reservation.
REPORTED = (
    "COALESCE((SELECT SUM(c.cost_usd) FROM run_attempt_costs c "
    "WHERE c.task_id=spend_reservations.task_id),0)"
)
CHARGE = (
    "COALESCE(SUM(CASE state WHEN 'settled' THEN cost_usd "
    f"ELSE MAX(reserved_usd, {REPORTED}) END),0)"
)


def used(
    conn: sa.Connection, namespace: str, group: str, *, key: str, pool: str | None = None
) -> float:
    """What a window has spent: settled costs plus open and unknown reservations."""
    if pool is None:
        return float(
            conn.exec_driver_sql(
                f"SELECT {CHARGE} FROM spend_reservations "
                "WHERE namespace=? AND group_name=? AND group_window=?",
                (namespace, group, key),
            ).scalar_one()
        )
    return float(
        conn.exec_driver_sql(
            f"SELECT {CHARGE} FROM spend_reservations "
            "WHERE namespace=? AND group_name=? AND pool=? AND pool_window=?",
            (namespace, group, pool, key),
        ).scalar_one()
    )


def runs_used(
    conn: sa.Connection, namespace: str, group: str, *, key: str, pool: str | None = None
) -> int:
    """How many runs a window admitted, whatever their state."""
    args: tuple[str, ...]
    if pool is None:
        sql, args = "group_window=?", (namespace, group, key)
    else:
        sql, args = "pool=? AND pool_window=?", (namespace, group, pool, key)
    return int(
        conn.exec_driver_sql(
            f"SELECT COUNT(*) FROM spend_reservations WHERE namespace=? AND group_name=? AND {sql}",
            args,
        ).scalar_one()
    )


def _membership(conn: sa.Connection, agent_id: str) -> sa.RowMapping | None:
    return (
        conn.exec_driver_sql(
            "SELECT m.namespace,m.group_name,m.pool,g.spec_json FROM group_members m "
            "JOIN agent_groups g ON g.namespace=m.namespace AND g.name=m.group_name "
            "WHERE m.principal_id=?",
            (agent_id,),
        )
        .mappings()
        .first()
    )


def _check(
    conn: sa.Connection,
    account: GroupSpend,
    at: datetime,
    reserve: float,
    label: str,
    **where: Any,
) -> str:
    span = window(account.window, account.timezone, at)
    if account.runs is not None:
        count = runs_used(conn, key=span.key, **where)
        if count + 1 > account.runs:
            raise Fault(
                "budget_exhausted",
                f"The {label} run ceiling ({account.runs} runs per {account.window}) is spent: "
                f"{count} runs admitted",
                429,
            )
    if account.usd is not None:
        spent = used(conn, key=span.key, **where)
        if spent + reserve > account.usd + 1e-9:
            raise Fault(
                "budget_exhausted",
                f"The {label} spend ceiling ({account.usd} USD per {account.window}) would be "
                f"exceeded: {spent:.4f} USD used or reserved, this run reserves {reserve} USD",
                429,
            )
    return span.key


def reserve(conn: sa.Connection, request: TaskRequest, task_id: str, at: datetime) -> None:
    """Reserve a group member's run against its group and pool accounts, or refuse it."""
    if not isinstance(request, AgentRunRequest):
        return
    member = _membership(conn, request.agent_id)
    if member is None:
        return
    spec = GroupSpec.model_validate_json(member["spec_json"])
    pool = next((p for p in spec.pools if p.name == member["pool"]), None)
    if spec.spend is None and (pool is None or pool.spend is None):
        return
    namespace, group = str(member["namespace"]), str(member["group_name"])
    settle(conn, namespace, group, at)
    amount = request.max_cost_usd
    group_window = pool_window = None
    if spec.spend is not None:
        group_window = _check(
            conn, spec.spend, at, amount, "group", namespace=namespace, group=group
        )
    if pool is not None and pool.spend is not None:
        pool_window = _check(
            conn,
            pool.spend,
            at,
            amount,
            f"pool {pool.name}",
            namespace=namespace,
            group=group,
            pool=pool.name,
        )
    conn.exec_driver_sql(
        "INSERT INTO spend_reservations VALUES (?,?,?,?,?,?,?,'reserved',NULL,?,NULL)",
        (
            task_id,
            namespace,
            group,
            member["pool"],
            group_window,
            pool_window,
            amount,
            at.isoformat(),
        ),
    )


@dataclass(frozen=True)
class Exhausted:
    """A run's account at or over its ceiling in the window the run was charged to."""

    account: str
    window: str
    used: float
    ceiling: float
    group: str
    pool: str
    namespace: str

    @property
    def message(self) -> str:
        return (
            f"The {self.account} spend ceiling ({self.ceiling} USD) is spent for {self.window} "
            f"({self.used:.4f} USD used or reserved); no further attempt starts"
        )


def exhausted(conn: sa.Connection, task_id: str) -> Exhausted | None:
    """Whether a group member run's group or pool account is at or over its ceiling.

    Checked before a retry or an approval continuation starts, in the windows the run was
    charged to at admission (decision 2), counting runs still open at their reservation or
    their reported cost, whichever is more.
    """
    row = (
        conn.exec_driver_sql(
            "SELECT r.*,g.spec_json FROM spend_reservations r JOIN agent_groups g "
            "ON g.namespace=r.namespace AND g.name=r.group_name WHERE r.task_id=?",
            (task_id,),
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    spec = GroupSpec.model_validate_json(row["spec_json"])
    namespace, group, pool_name = str(row["namespace"]), str(row["group_name"]), str(row["pool"])
    pool = next((p for p in spec.pools if p.name == pool_name), None)
    checks: list[tuple[str, GroupSpend | None, str | None, str | None]] = [
        ("group", spec.spend, row["group_window"], None),
        (f"pool {pool_name}", pool.spend if pool else None, row["pool_window"], pool_name),
    ]
    for account, ceiling, key, pool_filter in checks:
        if ceiling is None or key is None:
            continue
        if ceiling.usd is None:
            # A run ceiling counts admissions; a retry of an admitted run is not a new run.
            continue
        spent = used(conn, namespace, group, key=str(key), pool=pool_filter)
        if spent >= ceiling.usd - 1e-9:
            return Exhausted(account, str(key), spent, ceiling.usd, group, pool_name, namespace)
    return None


def notify_exhausted(
    conn: sa.Connection,
    tx: Any,
    runtime_id: str,
    task_id: str,
    spent: Exhausted,
    at: datetime,
) -> None:
    """Kernel mail to the group's supervisor that a run stopped on its spend ceiling."""
    from cardea.service import mailbox

    supervisor = conn.exec_driver_sql(
        "SELECT supervisor_id FROM task_bindings WHERE task_id=?", (task_id,)
    ).scalar_one()
    mailbox.send(
        conn,
        tx,
        namespace=spent.namespace,
        sender_id=runtime_id,
        supervisor_id=str(supervisor),
        to=mailbox.Address(instance=str(supervisor)),
        parts=[
            {
                "text": f"budget_exhausted: run {task_id} of pool {spent.pool} stopped. "
                f"{spent.message}."
            },
            {
                "data": {
                    "code": "budget_exhausted",
                    "task_id": task_id,
                    "group": spent.group,
                    "pool": spent.pool,
                    "account": spent.account,
                    "window": spent.window,
                    "used_usd": round(spent.used, 6),
                    "ceiling_usd": spent.ceiling,
                }
            },
        ],
        runtime_id=runtime_id,
        at=at,
        from_kernel=True,
    )


def view(conn: sa.Connection, task_id: str) -> dict[str, Any]:
    """The run's cost accounting for the run view: each attempt, the total and any reservation."""
    cost, attempts = run_cost(conn, task_id)
    row = (
        conn.exec_driver_sql(
            "SELECT r.*,t.state AS task_state FROM spend_reservations r JOIN tasks t "
            "USING(task_id) WHERE r.task_id=?",
            (task_id,),
        )
        .mappings()
        .first()
    )
    reservation = None
    if row is not None:
        state = str(row["state"])
        if state == "reserved" and row["task_state"] in TERMINAL:
            # Not yet settled by an admission: show what settlement will record.
            state = "unknown" if cost is None else "settled"
        reservation = {
            "group": row["group_name"],
            "pool": row["pool"],
            "group_window": row["group_window"],
            "pool_window": row["pool_window"],
            "reserved_usd": row["reserved_usd"],
            "state": state,
            "charged_usd": cost if state == "settled" else row["reserved_usd"],
        }
    return {"attempts": attempts, "cost_usd": cost, "reservation": reservation}


def notify_refused(
    conn: sa.Connection,
    tx: Any,
    runtime_id: str,
    agent_id: object,
    message: str,
    at: datetime,
) -> bool:
    """Kernel mail to a group's supervisor that a trigger's run was refused on a ceiling.

    Sent once per group and window: the client key names the window, so later refusals
    in the same window add nothing. Returns whether this call sent it. The trigger keeps
    firing; its next slot or event after the window turns is admitted again.
    """
    from cardea.service import mailbox

    member = _membership(conn, str(agent_id)) if isinstance(agent_id, str) else None
    if member is None:
        return False
    spec = GroupSpec.model_validate_json(member["spec_json"])
    pool = next((p for p in spec.pools if p.name == member["pool"]), None)
    accounts = [a for a in (spec.spend, pool.spend if pool else None) if a is not None]
    if not accounts:
        return False
    namespace, group = str(member["namespace"]), str(member["group_name"])
    keys = sorted({window(a.window, a.timezone, at).key for a in accounts})
    key = f"budget-refused:{group}:{member['pool']}:{'|'.join(keys)}"
    exists = conn.exec_driver_sql(
        "SELECT 1 FROM mailbox_messages WHERE namespace=? AND sender_id=? AND client_key=?",
        (namespace, runtime_id, key),
    ).first()
    if exists is not None:
        return False
    supervisor = spec.supervisor
    mailbox.send(
        conn,
        tx,
        namespace=namespace,
        sender_id=runtime_id,
        supervisor_id=supervisor,
        to=mailbox.Address(instance=supervisor),
        parts=[
            {
                "text": f"budget_exhausted: pool {member['pool']} of group {group} stopped "
                f"admitting runs for {', '.join(keys)}. {message} Runs resume when the "
                "window turns."
            },
            {
                "data": {
                    "code": "budget_exhausted",
                    "group": group,
                    "pool": str(member["pool"]),
                    "windows": list(keys),
                }
            },
        ],
        runtime_id=runtime_id,
        at=at,
        client_key=key,
        from_kernel=True,
    )
    return True
