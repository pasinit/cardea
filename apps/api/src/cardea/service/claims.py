"""Claim upkeep: renewal at run boundaries, expiry and follow-up wakes (ADR-034 2.4).

A claim belongs to a principal, so it is renewed only by evidence that the principal is
making progress (ADR-034 2.4, amended 2026-11-17):
- an attempt start of one of its runs extends the lease to at least the attempt's deadline
  plus 120 s, but only if the holder's previous attempt on the claim did not fail;
- a run that succeeds extends it to the collection's full lease from now;
- an idempotent re-claim renews it (`records.claim`).

Failed or crashed attempts renew nothing, and an attempt that follows one does not either: it
may still run, but the claim lapses at its current expiry and the attempt's effects on the
record are then fenced (`claim_lost`). `records.attempt_renewal` carries this: an attempt
start clears it, and progress (a successful run, a successful session that parks for an
approval, a successful claim call) sets it again. A holder whose attempts all fail therefore
loses the claim within its lease, plus one attempt's duration and the 120 s margin if an
attempt started just before the lease ended, plus one minute tick for the sweep, after its
last progress. The per-minute trigger tick expires lapsed claims and,
for group members whose claim is past half its lease and who have no unfinished run, admits
one coalesced follow-up run from the member's first trigger template, at most
`max_followups` per epoch until the holder acts on the record again. A holder whose latest
run the kill switch cancelled (disabling its supervisor) is woken as soon as admission
works again, without waiting for half the lease: nothing else would resume the claimed
work. A claim that lapses after its follow-ups is escalated to the collection's supervisor
by mail.
"""

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import sqlalchemy as sa
from pydantic import ValidationError

from cardea.service import mailbox
from cardea.service.database import Fault, borrow
from cardea.service.models import DISABLED_REQUEST_PREFIX, TriggerSpec, now
from cardea.service.records import (
    ATTEMPT_RENEWAL_MARGIN_SECONDS,
    CollectionSpec,
    expire,
    stamp,
)

if TYPE_CHECKING:
    from cardea.service.kernel import Kernel

log = logging.getLogger(__name__)
UNFINISHED = "t.state NOT IN ('succeeded','failed','cancelled')"
# Bounded work per tick; the next tick continues.
SWEEP_LIMIT = 256


def _live(conn: sa.Connection, holder: str, at: datetime) -> list[sa.RowMapping]:
    return list(
        conn.exec_driver_sql(
            "SELECT r.*, c.spec_json FROM records r JOIN collections c "
            "ON c.namespace=r.namespace AND c.name=r.collection "
            "WHERE r.holder=? AND r.lease_expires_at>?",
            (holder, stamp(at)),
        )
        .mappings()
        .all()
    )


def _extend(conn: sa.Connection, row: sa.RowMapping, until: datetime) -> None:
    current = datetime.fromisoformat(str(row["lease_expires_at"]))
    if until > current:
        conn.exec_driver_sql(
            "UPDATE records SET lease_expires_at=? WHERE seq=?", (stamp(until), row["seq"])
        )


def renew_for_attempt(conn: sa.Connection, holder: str, attempt_deadline: datetime) -> int:
    """An attempt of the holder's run started: keep its claims past that attempt, unless the
    holder's previous attempt on a claim failed. Returns how many claims were extended.

    Every live claim of the holder then waits for this attempt's progress before the next
    attempt start may extend it again (`attempt_renewal`)."""
    rows = _live(conn, holder, now())
    until = attempt_deadline + timedelta(seconds=ATTEMPT_RENEWAL_MARGIN_SECONDS)
    renewed = 0
    for row in rows:
        if int(row["attempt_renewal"]):
            _extend(conn, row, until)
            renewed += 1
    _set_progress(conn, rows, False)
    return renewed


def mark_progress(conn: sa.Connection, holder: str, at: datetime) -> int:
    """The holder's attempt made progress without finishing its run (a successful session
    that parks for an approval): the next attempt start may extend its claims again."""
    rows = _live(conn, holder, at)
    _set_progress(conn, rows, True)
    return len(rows)


def renew_for_success(conn: sa.Connection, holder: str, at: datetime) -> int:
    """The holder's run succeeded: each live claim gets its collection's full lease."""
    rows = _live(conn, holder, at)
    for row in rows:
        claims = CollectionSpec.model_validate_json(row["spec_json"]).claims
        if claims is not None:
            _extend(conn, row, at + timedelta(seconds=claims.lease_seconds))
    _set_progress(conn, rows, True)
    return len(rows)


def _set_progress(conn: sa.Connection, rows: list[sa.RowMapping], allowed: bool) -> None:
    for row in rows:
        conn.exec_driver_sql(
            "UPDATE records SET attempt_renewal=? WHERE seq=?", (int(allowed), row["seq"])
        )


def sweep(kernel: "Kernel", at: datetime) -> int:
    """Release every claim whose lease has ended; the record returns to the pool."""
    with kernel.database.engine.begin() as conn:
        rows = (
            conn.exec_driver_sql(
                "SELECT r.*, c.spec_json, c.supervisor_id FROM records r JOIN collections c "
                "ON c.namespace=r.namespace AND c.name=r.collection "
                "WHERE r.holder IS NOT NULL AND r.lease_expires_at<=? LIMIT ?",
                (stamp(at), SWEEP_LIMIT),
            )
            .mappings()
            .all()
        )
        for row in rows:
            spec = CollectionSpec.model_validate_json(row["spec_json"])
            claims = spec.claims
            exhausted = claims is not None and int(row["followups"]) >= claims.max_followups
            expire(
                conn,
                row,
                at,
                actor=kernel.runtime_id,
                reason="followups_exhausted" if exhausted else "lease_expired",
                spec=spec,
            )
            if exhausted:
                escalate(conn, row, kernel.runtime_id, at)
    return len(rows)


def escalate(conn: sa.Connection, row: sa.RowMapping, runtime_id: str, at: datetime) -> None:
    """Tell the collection's supervisor that a holder's claim lapsed after its follow-ups.

    The notice is mail from the kernel's runtime (ADR-034 decisions 2.4 and 4), committed
    with the expiry.
    """
    try:
        record: mailbox.RecordRef | None = mailbox.RecordRef(
            collection=row["collection"], record_id=row["record_id"]
        )
    except ValidationError:
        record = None
    supervisor = str(row["supervisor_id"])
    with borrow(conn) as tx:
        mailbox.send(
            conn,
            tx,
            namespace=str(row["namespace"]),
            sender_id=runtime_id,
            supervisor_id=supervisor,
            to=mailbox.Address(instance=supervisor),
            parts=[
                {
                    "text": f"{row['holder']} held {row['collection']}/{row['record_id']} "
                    f"through {row['followups']} follow-up runs without acting on it, so its "
                    "claim lapsed and the record returned to the pool."
                },
                {
                    "data": {
                        "collection": row["collection"],
                        "record_id": row["record_id"],
                        "holder": row["holder"],
                        "claim_epoch": row["claim_epoch"],
                        "followups": row["followups"],
                        "reason": "followups_exhausted",
                    }
                },
            ],
            runtime_id=runtime_id,
            at=at,
            record=record,
            from_kernel=True,
        )


def followup_request_id(holder: str, record_id: str, epoch: int, n: int) -> str:
    return f"{holder}.claim.{record_id}.{epoch}.{n}"


def followups(kernel: "Kernel", at: datetime) -> list[str]:
    """Admit one follow-up run for each idle group-member holder past half its lease."""
    with kernel.database.read() as conn:
        rows = (
            conn.exec_driver_sql(
                "SELECT r.*, c.spec_json FROM records r JOIN collections c "
                "ON c.namespace=r.namespace AND c.name=r.collection "
                "JOIN group_members m ON m.principal_id=r.holder AND m.state='active' "
                "WHERE r.holder IS NOT NULL AND r.lease_expires_at>? ORDER BY r.seq LIMIT ?",
                (stamp(at), SWEEP_LIMIT),
            )
            .mappings()
            .all()
        )
    admitted: list[str] = []
    woken: set[str] = set()
    for row in rows:
        holder = str(row["holder"])
        claims = CollectionSpec.model_validate_json(row["spec_json"]).claims
        if claims is None or holder in woken or int(row["followups"]) >= claims.max_followups:
            continue
        left = datetime.fromisoformat(str(row["lease_expires_at"])) - at
        if left.total_seconds() * 2 >= claims.lease_seconds and not interrupted(kernel, holder):
            continue
        task = _followup(kernel, row, holder)
        if task is not None:
            woken.add(holder)
            admitted.append(task)
    return admitted


def interrupted(kernel: "Kernel", holder: str) -> bool:
    """The holder's latest run was cancelled by disabling its supervisor (the kill switch).

    Once a run is admitted again (a follow-up) it is the latest, so one interruption wakes
    the holder once; while the supervisor stays disabled, admission refuses the follow-up
    and nothing is counted."""
    with kernel.database.read() as conn:
        row = conn.exec_driver_sql(
            "SELECT t.state, (SELECT 1 FROM control_requests c WHERE c.task_id=b.task_id "
            "AND c.kind='cancel' AND substr(c.request_id,1,?)=?) "
            "FROM task_bindings b JOIN tasks t USING(task_id) "
            "WHERE b.agent_id=? ORDER BY b.rowid DESC LIMIT 1",
            (len(DISABLED_REQUEST_PREFIX), DISABLED_REQUEST_PREFIX, holder),
        ).first()
    return row is not None and row[0] == "cancelled" and row[1] is not None


def _followup(kernel: "Kernel", row: sa.RowMapping, holder: str) -> str | None:
    """Admit one follow-up and count it in one transaction; a refusal rolls both back."""
    epoch, n = int(row["claim_epoch"]), int(row["followups"]) + 1
    try:
        with kernel.database.engine.begin() as conn, borrow(conn) as tx:
            busy = conn.exec_driver_sql(
                "SELECT 1 FROM task_bindings b JOIN tasks t USING(task_id) "
                f"WHERE b.agent_id=? AND {UNFINISHED} LIMIT 1",
                (holder,),
            ).first()
            trigger = conn.exec_driver_sql(
                "SELECT definition_json FROM triggers WHERE trigger_id LIKE ? AND removed=0 "
                "AND kind IN ('time','journal') ORDER BY length(trigger_id), trigger_id "
                "LIMIT 1",
                (f"{holder}.t%",),
            ).first()
            if busy is not None or trigger is None:
                # A running holder renews its own claim. The template is the member's
                # first trigger, time or journal (ADR-034 slice 7); a member without one
                # has nothing to wake with.
                return None
            changed = conn.exec_driver_sql(
                "UPDATE records SET followups=followups+1 "
                "WHERE seq=? AND holder=? AND claim_epoch=? AND followups=?",
                (row["seq"], holder, epoch, n - 1),
            ).rowcount
            if changed != 1:
                return None
            spec = TriggerSpec.model_validate_json(trigger[0])
            request = spec.request(followup_request_id(holder, str(row["record_id"]), epoch, n))
            view = kernel.admit_scheduled(
                conn,
                tx,
                spec.supervisor_id,
                request,
                {"followup": f"{row['collection']}/{row['record_id']}", "claim_epoch": str(epoch)},
            )
            return view.task.task_id
    except Fault as refused:
        log.warning("claim follow-up refused", extra={"code": refused.code, "reason": holder})
        return None


def upkeep(kernel: "Kernel", at: datetime) -> None:
    """The tick's claim work; a failure is logged and the next tick retries."""
    try:
        sweep(kernel, at)
        followups(kernel, at)
    except Exception:
        log.exception("claim upkeep failed")
