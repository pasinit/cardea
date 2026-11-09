"""Kernel journal events as an internal event-trigger source (ADR-034 decision 6, ADR-035).

The kernel appends journal events in the same transaction as the change they report:
mailbox `cardea.mailbox.sent|delivered|dead`, record `cardea.record.created|updated` and
approval `cardea.approval.requested|decided|void|expired|withdrawn`. A journal trigger
reads its own namespace's journal through a cursor and admits its template for each
matching event, exactly as an ADR-035 delivery does:

- the same CloudEvents matching (exact `type`, ADR-035 filters, optional `subjects`);
- the same admission path and live ceiling (`Kernel.admit_scheduled`);
- the same dedupe: one admitted task per (trigger, source, id), and the request ID
  `<trigger_id>.e<digest>`, committed in one transaction with the cursor move;
- the event passed to the run as bounded, untrusted `trigger_event` data.

There is no signature, because the kernel is the sender; the source is always the
trigger's own namespace, `cardea://<namespace>`. An event whose `actor` is the trigger's
own agent is ignored unless the trigger says `include_own`, so a run cannot wake itself.
A group member's events coalesce while its run is unfinished (ADR-034 decision 1.4); an
event coalesced after that run started leaves one follow-up admission pending.
"""

import json
import logging
from typing import TYPE_CHECKING, Any

import rfc8785
import sqlalchemy as sa
from pydantic import JsonValue, ValidationError

from cardea.kernel.lifecycle import ContractError
from cardea.kernel.store import StoreError
from cardea.service.database import Fault, borrow
from cardea.service.events import request_id
from cardea.service.models import InboundEvent, TriggerSpec, now
from cardea.service.triggers import MemberBusy

if TYPE_CHECKING:
    from cardea.service.kernel import Kernel

log = logging.getLogger(__name__)

# Journal event types with no CloudEvents payload of their own, and the type they carry.
APPROVALS = {
    f"approval.{kind}": f"cardea.approval.{kind}"
    for kind in ("requested", "decided", "void", "expired", "withdrawn")
}
# Event data larger than this reaches the run as a size note instead (bounded context).
MAX_DATA_BYTES = 4096
BATCH = 200


def source(namespace: str) -> str:
    return f"cardea://{namespace}"


def _bounded(data: JsonValue) -> JsonValue:
    size = len(rfc8785.dumps(data))
    return data if size <= MAX_DATA_BYTES else {"truncated": True, "bytes": size}


def adapt(
    conn: sa.Connection, row: sa.RowMapping, runtime_id: str
) -> tuple[InboundEvent, dict[str, str]] | None:
    """One journal row as a CloudEvent and its string attributes; None if not exposed.

    `actor` is the principal whose action produced the event: the producer, or, when
    the kernel's runtime produced it on behalf of a run, that run's agent.
    """
    payload = json.loads(row["payload"]) if row["payload"] else {}
    namespace = str(row["namespace"])
    if str(row["event_type"]).startswith(("cardea.mailbox.", "cardea.record.")):
        if payload.get("specversion") != "1.0":
            return None
        kind, subject, data = payload["type"], payload.get("subject"), payload.get("data")
    elif row["event_type"] in APPROVALS:
        kind, subject, data = APPROVALS[row["event_type"]], payload.get("request_id"), payload
    else:
        return None
    actor = str(row["producer_id"])
    if actor == runtime_id and row["task_id"]:
        agent = conn.exec_driver_sql(
            "SELECT agent_id FROM task_bindings WHERE task_id=?", (row["task_id"],)
        ).scalar()
        actor = str(agent) if agent else actor
    event = InboundEvent(
        id=str(row["event_id"]),
        source=source(namespace),
        type=str(kind),
        subject=str(subject) if subject is not None else None,
        time=str(row["occurred_at"]),
        data=_bounded(data),
    )
    attributes = {
        "specversion": "1.0",
        "id": event.id,
        "source": event.source,
        "type": event.type,
        "actor": actor,
        **({"subject": event.subject} if event.subject is not None else {}),
        **({"time": event.time} if event.time else {}),
    }
    return event, attributes


def _record(
    conn: sa.Connection,
    trigger_id: str,
    event: InboundEvent,
    *,
    task_id: str | None = None,
    code: str | None = None,
) -> None:
    conn.exec_driver_sql(
        "INSERT INTO trigger_deliveries(trigger_id,source,event_id,event_type,outcome,task_id,"
        "code,at) VALUES (?,?,?,?,?,?,?,?)",
        (
            trigger_id,
            event.source,
            event.id,
            event.type,
            "admitted" if task_id else "refused",
            task_id,
            code,
            now().isoformat(),
        ),
    )


def _code(error: Exception) -> str:
    if isinstance(error, Fault):
        return error.code
    if isinstance(error, ValidationError):
        return "invalid_template"
    return str(getattr(error, "code", "refused"))


class Dispatcher:
    """Reads each journal trigger's namespace journal and admits matching events."""

    def __init__(self, kernel: "Kernel") -> None:
        self.kernel = kernel

    def tick(self) -> dict[str, str]:
        with self.kernel.database.read() as conn:
            ids = [
                str(r[0])
                for r in conn.exec_driver_sql(
                    "SELECT trigger_id FROM triggers WHERE kind='journal' AND enabled=1 "
                    "AND removed=0 ORDER BY trigger_id"
                )
            ]
        outcomes: dict[str, str] = {}
        for trigger_id in ids:
            try:
                outcomes[trigger_id] = self.evaluate(trigger_id)
            except Exception:
                # One broken trigger must not stop the others; the next tick retries it.
                log.exception("journal trigger failed", extra={"code": trigger_id})
                outcomes[trigger_id] = "error"
        return outcomes

    def _state(self, conn: sa.Connection, trigger_id: str) -> sa.RowMapping | None:
        return (
            conn.exec_driver_sql(
                "SELECT t.*,c.cursor,c.pending_cursor FROM triggers t "
                "JOIN journal_trigger_cursors c USING(trigger_id) WHERE t.trigger_id=? "
                "AND t.kind='journal' AND t.enabled=1 AND t.removed=0",
                (trigger_id,),
            )
            .mappings()
            .first()
        )

    def evaluate(self, trigger_id: str) -> str:
        """Admit what the trigger's journal holds now; returns the last outcome."""
        outcome = "idle"
        with self.kernel.database.read() as conn:
            state = self._state(conn, trigger_id)
        if state is None:
            return "inactive"
        if state["pending_cursor"] is not None:
            outcome = self._step(trigger_id, int(state["pending_cursor"]), pending=True)
            if outcome == "coalesced":
                return outcome
        while True:
            with self.kernel.database.read() as conn:
                state = self._state(conn, trigger_id)
                if state is None:
                    return "inactive"
                rows = conn.exec_driver_sql(
                    "SELECT cursor FROM events WHERE namespace=? AND cursor>? "
                    "ORDER BY cursor LIMIT ?",
                    (state["namespace"], state["cursor"], BATCH),
                ).fetchall()
            if not rows:
                return outcome
            for (cursor,) in rows:
                result = self._step(trigger_id, int(cursor))
                if result != "skipped":
                    outcome = result
            if len(rows) < BATCH:
                return outcome

    def _step(self, trigger_id: str, cursor: int, *, pending: bool = False) -> str:
        """Decide one journal event for one trigger, in one transaction with the cursor.

        An admission commits with its delivery row and the cursor move. A refusal or a
        coalesced event rolls the admission back and is recorded, with the cursor move, in
        a second transaction that first checks nothing moved meanwhile.
        """
        kernel = self.kernel
        try:
            with kernel.database.engine.begin() as conn, borrow(conn) as tx:
                decided = self._decide(conn, trigger_id, cursor, pending)
                if isinstance(decided, str):
                    return decided
                spec, event = decided
                request = spec.request(request_id(trigger_id, event), event=event)
                view = kernel.admit_scheduled(
                    conn,
                    tx,
                    spec.supervisor_id,
                    request,
                    {"trigger_id": trigger_id, "event_source": event.source, "event_id": event.id},
                )
                _record(conn, trigger_id, event, task_id=view.task.task_id)
                self._advance(conn, trigger_id, cursor, pending)
                kernel.fault("journal.before_commit")
        except MemberBusy as busy:
            with kernel.database.engine.begin() as conn:
                decided = self._decide(conn, trigger_id, cursor, pending)
                if isinstance(decided, str):
                    return decided
                _record(conn, trigger_id, decided[1], code="coalesced")
                started = conn.exec_driver_sql(
                    "SELECT json_extract(snapshot,'$.checkpoint') IS NOT NULL FROM tasks "
                    "WHERE task_id=?",
                    (busy.task_id,),
                ).scalar()
                # A run that already started may have missed this event: keep one wake.
                self._advance(conn, trigger_id, cursor, pending, keep=bool(started))
            return "coalesced"
        except (Fault, ValidationError, StoreError, ContractError) as error:
            code = _code(error)
            with kernel.database.engine.begin() as conn:
                decided = self._decide(conn, trigger_id, cursor, pending)
                if isinstance(decided, str):
                    return decided
                # A journal event has no sender to retry it: the refusal is history.
                _record(conn, trigger_id, decided[1], code=code)
                self._advance(conn, trigger_id, cursor, pending)
            log.warning("journal admission refused", extra={"code": code, "reason": trigger_id})
            return "refused"
        kernel.fault("journal.after_commit")
        return "admitted"

    def _decide(
        self, conn: sa.Connection, trigger_id: str, cursor: int, pending: bool
    ) -> str | tuple[TriggerSpec, InboundEvent]:
        """The trigger and event to admit, or why there is nothing to do."""
        state = self._state(conn, trigger_id)
        if state is None:
            return "inactive"
        if pending and state["pending_cursor"] != cursor:
            return "stale"
        if not pending and int(state["cursor"]) >= cursor:
            return "skipped"
        spec = TriggerSpec.model_validate_json(state["definition_json"])
        assert spec.journal is not None, "journal trigger"
        row = (
            conn.exec_driver_sql(
                "SELECT * FROM events WHERE cursor=? AND namespace=?",
                (cursor, state["namespace"]),
            )
            .mappings()
            .first()
        )
        adapted = adapt(conn, row, self.kernel.runtime_id) if row is not None else None
        agent = str(spec.template.get("agent_id", ""))
        if adapted is None or not spec.journal.matches(*adapted, agent):
            self._advance(conn, trigger_id, cursor, pending)
            return "skipped"
        event = adapted[0]
        if conn.exec_driver_sql(
            "SELECT 1 FROM trigger_deliveries WHERE trigger_id=? AND source=? "
            "AND event_id=? AND outcome='admitted'",
            (trigger_id, event.source, event.id),
        ).first():
            self._advance(conn, trigger_id, cursor, pending)
            return "duplicate"
        return spec, event

    @staticmethod
    def _advance(
        conn: sa.Connection, trigger_id: str, cursor: int, pending: bool, keep: bool = False
    ) -> None:
        """Move past `cursor`; `keep` leaves it pending for one follow-up admission."""
        if pending:
            conn.exec_driver_sql(
                "UPDATE journal_trigger_cursors SET pending_cursor=?,updated_at=? "
                "WHERE trigger_id=?",
                (cursor if keep else None, now().isoformat(), trigger_id),
            )
            return
        conn.exec_driver_sql(
            "UPDATE journal_trigger_cursors SET cursor=MAX(cursor,?),updated_at=?,"
            "pending_cursor=CASE WHEN ? THEN ? ELSE pending_cursor END WHERE trigger_id=?",
            (cursor, now().isoformat(), int(keep), cursor, trigger_id),
        )


def deliveries(conn: sa.Connection, trigger_id: str, limit: int = 50) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.exec_driver_sql(
            "SELECT delivery_id,source,event_id,event_type,outcome,task_id,code,at "
            "FROM trigger_deliveries WHERE trigger_id=? ORDER BY delivery_id DESC LIMIT ?",
            (trigger_id, limit),
        ).mappings()
    ]
