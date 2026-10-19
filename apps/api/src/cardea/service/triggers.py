"""Time triggers (ADR-024): host-owner installation and one admission per scheduled slot.

DBOS owns the clock: one DBOS schedule runs `Scheduler.tick` once a minute. A tick
decides each trigger's newest due slot and, in one SQLite transaction, admits it
through `Kernel.admit_scheduled` and records the outcome and `last_slot`. A crash
before that commit leaves nothing; a crash after it leaves a decided slot. The slot
request ID `<trigger_id>.<slot>` additionally makes a repeated admission return the
existing task.
"""

import json
import logging
from collections.abc import Callable, Sequence
from datetime import datetime
from hashlib import sha256
from typing import TYPE_CHECKING, Any

import rfc8785
import sqlalchemy as sa
from pydantic import ValidationError

from cardea.kernel.lifecycle import ContractError
from cardea.kernel.store import StoreError
from cardea.service.database import Fault, borrow
from cardea.service.models import TriggerSpec, now
from cardea.service.operations import Registry
from cardea.service.schedule import Decision, decide, slot_key

if TYPE_CHECKING:
    from cardea.service.kernel import Kernel

log = logging.getLogger(__name__)


def digest(spec: TriggerSpec) -> str:
    return sha256(rfc8785.dumps(spec.model_dump(mode="json"))).hexdigest()


def _event(
    conn: sa.Connection,
    trigger_id: str,
    kind: str,
    at: datetime,
    *,
    slot: datetime | None = None,
    task_id: str | None = None,
    code: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    conn.exec_driver_sql(
        "INSERT INTO trigger_events(trigger_id,kind,slot,task_id,code,detail_json,at) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            trigger_id,
            kind,
            slot.isoformat() if slot else None,
            task_id,
            code,
            json.dumps(detail or {}, sort_keys=True),
            at.isoformat(),
        ),
    )


def install(
    conn: sa.Connection, specs: Sequence[TriggerSpec], operations: Registry, at: datetime
) -> None:
    """Replace the host-owned trigger set inside the provisioning transaction.

    Supervisor-managed triggers (ADR-029) are neither replaced nor removed here.
    """
    seen: set[str] = set()
    for spec in specs:
        if spec.trigger_id in seen:
            raise Fault("trigger_duplicate", "Trigger IDs must be unique")
        seen.add(spec.trigger_id)
        upsert(conn, spec, operations, at, managed_by="host")
    for row in conn.exec_driver_sql(
        "SELECT trigger_id FROM triggers WHERE removed=0 AND managed_by='host'"
    ).all():
        if row[0] not in seen:
            remove(conn, row[0], at)


def upsert(
    conn: sa.Connection,
    spec: TriggerSpec,
    operations: Registry,
    at: datetime,
    *,
    managed_by: str,
) -> str:
    """Install or update one trigger; returns the history kind recorded (or "unchanged")."""
    if not conn.exec_driver_sql(
        "SELECT 1 FROM principals WHERE principal_id=?", (spec.supervisor_id,)
    ).first():
        raise Fault("unknown_principal", "Trigger supervisor is not provisioned")
    request = spec.request(f"{spec.trigger_id}.{slot_key(at)}")
    operations.check_grants(request.grants)
    old = (
        conn.exec_driver_sql("SELECT * FROM triggers WHERE trigger_id=?", (spec.trigger_id,))
        .mappings()
        .first()
    )
    definition, checksum = spec.model_dump_json(), digest(spec)
    if old is None:
        conn.exec_driver_sql(
            "INSERT INTO triggers(trigger_id,supervisor_id,namespace,definition_json,"
            "definition_sha256,enabled,removed,anchor,last_slot,updated_at,managed_by) "
            "VALUES (?,?,?,?,?,?,0,?,NULL,?,?)",
            (
                spec.trigger_id,
                spec.supervisor_id,
                request.namespace,
                definition,
                checksum,
                int(spec.enabled),
                at.isoformat(),
                at.isoformat(),
                managed_by,
            ),
        )
        _event(conn, spec.trigger_id, "installed", at, detail={"sha256": checksum})
        return "installed"
    if old["managed_by"] != managed_by:
        raise Fault("trigger_owner", "Trigger is managed by another owner", 409)
    if old["supervisor_id"] != spec.supervisor_id:
        raise Fault("trigger_immutable", "Install a new trigger to change its supervisor")
    if old["definition_sha256"] == checksum and not old["removed"]:
        return "unchanged"
    # Slots while disabled or removed are not caught up: re-enabling restarts the
    # anchor, so only slots after this change can be admitted.
    restarted = spec.enabled and (old["removed"] or not old["enabled"])
    conn.exec_driver_sql(
        "UPDATE triggers SET namespace=?,definition_json=?,definition_sha256=?,enabled=?,"
        "removed=0,anchor=?,updated_at=? WHERE trigger_id=?",
        (
            request.namespace,
            definition,
            checksum,
            int(spec.enabled),
            at.isoformat() if restarted else old["anchor"],
            at.isoformat(),
            spec.trigger_id,
        ),
    )
    kind = (
        "enabled" if restarted else "disabled" if old["enabled"] and not spec.enabled else "updated"
    )
    _event(conn, spec.trigger_id, kind, at, detail={"sha256": checksum})
    return kind


def remove(conn: sa.Connection, trigger_id: str, at: datetime) -> None:
    conn.exec_driver_sql(
        "UPDATE triggers SET removed=1,enabled=0,updated_at=? WHERE trigger_id=?",
        (at.isoformat(), trigger_id),
    )
    _event(conn, trigger_id, "removed", at)


class Scheduler:
    """Evaluates installed triggers; never raises for a single trigger's failure."""

    def __init__(self, kernel: "Kernel", clock: Callable[[], datetime] = now) -> None:
        self.kernel = kernel
        self.clock = clock

    def tick(self) -> dict[str, str]:
        with self.kernel.database.read() as conn:
            ids = [
                str(row[0])
                for row in conn.exec_driver_sql(
                    "SELECT trigger_id FROM triggers WHERE enabled=1 AND removed=0 "
                    "ORDER BY trigger_id"
                )
            ]
        outcomes: dict[str, str] = {}
        for trigger_id in ids:
            try:
                outcomes[trigger_id] = self.evaluate(trigger_id)
            except Exception:
                # A broken trigger must not stop the others; the next tick retries it.
                log.exception("trigger evaluation failed", extra={"code": trigger_id})
                outcomes[trigger_id] = "error"
        return outcomes

    def _state(self, conn: sa.Connection, trigger_id: str) -> sa.RowMapping | None:
        return (
            conn.exec_driver_sql(
                "SELECT * FROM triggers WHERE trigger_id=? AND enabled=1 AND removed=0",
                (trigger_id,),
            )
            .mappings()
            .first()
        )

    def evaluate(self, trigger_id: str) -> str:
        at = self.clock()
        with self.kernel.database.read() as conn:
            state = self._state(conn, trigger_id)
        if state is None:
            return "inactive"
        spec = TriggerSpec.model_validate_json(state["definition_json"])
        after = datetime.fromisoformat(state["last_slot"] or state["anchor"])
        decision = decide(spec.schedule, spec.timezone, spec.misfire, spec.grace_seconds, after, at)
        if decision.slot is None:
            return "idle"
        self.kernel.fault("trigger.slot_computed")
        expected = (state["definition_sha256"], state["anchor"], state["last_slot"])
        try:
            with self.kernel.database.engine.begin() as conn, borrow(conn) as tx:
                if not self._current(conn, trigger_id, expected):
                    return "stale"
                self._record_missed(conn, trigger_id, decision, at)
                outcome = "missed"
                if decision.admit:
                    request = spec.request(f"{trigger_id}.{slot_key(decision.slot)}")
                    view = self.kernel.admit_scheduled(
                        conn,
                        tx,
                        spec.supervisor_id,
                        request,
                        {"trigger_id": trigger_id, "slot": decision.slot.isoformat()},
                    )
                    _event(
                        conn,
                        trigger_id,
                        "admitted",
                        at,
                        slot=decision.slot,
                        task_id=view.task.task_id,
                        detail={"late": decision.late},
                    )
                    outcome = "admitted"
                self._advance(conn, trigger_id, decision.slot)
                self.kernel.fault("trigger.before_commit")
        except (Fault, ValidationError, StoreError, ContractError) as error:
            code = (
                error.code
                if isinstance(error, Fault)
                else "invalid_template"
                if isinstance(error, ValidationError)
                else str(error.code)
            )
            # The admission rolled back. Record the refusal and close the slot so a
            # revoked or broken trigger cannot retry the same slot every minute.
            with self.kernel.database.engine.begin() as conn:
                if not self._current(conn, trigger_id, expected):
                    return "stale"
                self._record_missed(conn, trigger_id, decision, at)
                _event(conn, trigger_id, "failed", at, slot=decision.slot, code=code)
                self._advance(conn, trigger_id, decision.slot)
            log.warning("trigger admission refused", extra={"code": code, "reason": trigger_id})
            return "failed"
        self.kernel.fault("trigger.after_commit")
        return outcome

    def _current(
        self, conn: sa.Connection, trigger_id: str, expected: tuple[str, str, str | None]
    ) -> bool:
        state = self._state(conn, trigger_id)
        return (
            state is not None
            and (
                state["definition_sha256"],
                state["anchor"],
                state["last_slot"],
            )
            == expected
        )

    def _record_missed(
        self, conn: sa.Connection, trigger_id: str, decision: Decision, at: datetime
    ) -> None:
        if not decision.missed:
            return
        assert decision.first_missed is not None and decision.last_missed is not None
        _event(
            conn,
            trigger_id,
            "missed",
            at,
            detail={
                "count": decision.missed,
                "count_capped": decision.missed_capped,
                "first": decision.first_missed.isoformat(),
                "last": decision.last_missed.isoformat(),
            },
        )

    def _advance(self, conn: sa.Connection, trigger_id: str, slot: datetime) -> None:
        conn.exec_driver_sql(
            "UPDATE triggers SET last_slot=? WHERE trigger_id=?", (slot.isoformat(), trigger_id)
        )
