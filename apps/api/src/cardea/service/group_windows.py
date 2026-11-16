"""Windowed pool expectations (ADR-034 decision 5.4, slice 9).

A pool's `expect_window` entries count its members' successful receipted calls of one
operation (for `records.transition`, optionally of one named transition) in each calendar
day or ISO week. The minute tick checks each entry once its window has closed: a count
below `min` or above `max` sends `expectation_unmet` or `expectation_exceeded` mail from the
kernel to the group's supervisor. A report row makes each check happen once. Nothing here
fails a run after the fact.

Only the most recently closed window is checked, so windows that closed while the daemon
was down for longer than one window are skipped, like a `skip` misfire (ADR-024); windows
that began before the group was created are not checked.
"""

import logging
from datetime import datetime
from hashlib import sha256
from typing import TYPE_CHECKING, Any

import rfc8785
import sqlalchemy as sa

from cardea.kernel.models import OperationState
from cardea.kernel.store import KernelTransaction
from cardea.service import mailbox
from cardea.service.database import borrow
from cardea.service.models import GroupSpec, WindowExpectation, now
from cardea.service.spend import Span, previous

if TYPE_CHECKING:
    from cardea.service.kernel import Kernel

log = logging.getLogger(__name__)


def entry_sha256(entry: WindowExpectation) -> str:
    return sha256(rfc8785.dumps(entry.model_dump(mode="json"))).hexdigest()


def observed(
    tx: KernelTransaction,
    conn: sa.Connection,
    namespace: str,
    group: str,
    pool: str,
    entry: WindowExpectation,
    span: Span,
) -> int:
    """Successful, not deduplicated calls of the entry's operation settled in the window."""
    tasks = conn.exec_driver_sql(
        "SELECT b.task_id FROM task_bindings b JOIN group_members m "
        "ON m.principal_id=b.agent_id WHERE m.namespace=? AND m.group_name=? AND m.pool=?",
        (namespace, group, pool),
    ).all()
    count = 0
    for (task_id,) in tasks:
        for receipt in tx.list_operations(str(task_id)):
            if (
                receipt.request.name != entry.operation
                or receipt.state is not OperationState.SUCCEEDED
                or (receipt.result or {}).get("deduplicated")
                or not span.start <= receipt.updated_at < span.end
            ):
                continue
            if entry.transition is not None:
                arguments = receipt.request.arguments.get("arguments")
                if not isinstance(arguments, dict) or arguments.get("transition") != (
                    entry.transition
                ):
                    continue
            count += 1
    return count


def _outcome(entry: WindowExpectation, count: int) -> str:
    if entry.min is not None and count < entry.min:
        return "expectation_unmet"
    if entry.max is not None and count > entry.max:
        return "expectation_exceeded"
    return "met"


def _describe(entry: WindowExpectation) -> str:
    what = entry.operation + (f" ({entry.transition})" if entry.transition else "")
    bounds = []
    if entry.min is not None:
        bounds.append(f"at least {entry.min}")
    if entry.max is not None:
        bounds.append(f"at most {entry.max}")
    return f"{' and '.join(bounds)} successful call(s) of {what} per {entry.window}"


def check(
    kernel: "Kernel",
    group: sa.RowMapping,
    pool: str,
    entry: WindowExpectation,
    at: datetime,
) -> str:
    """Check one entry's most recently closed window, once; returns the outcome."""
    span = previous(entry.window, entry.timezone, at)
    if span.start < datetime.fromisoformat(group["created_at"]):
        return "skipped"
    namespace, name = str(group["namespace"]), str(group["name"])
    digest = entry_sha256(entry)
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        if conn.exec_driver_sql(
            "SELECT 1 FROM expect_window_reports WHERE namespace=? AND group_name=? AND pool=? "
            "AND entry_sha256=? AND window_key=?",
            (namespace, name, pool, digest, span.key),
        ).first():
            return "reported"
        count = observed(tx, conn, namespace, name, pool, entry, span)
        outcome = _outcome(entry, count)
        message_id = None
        if outcome != "met":
            data: dict[str, Any] = {
                "code": outcome,
                "group": name,
                "pool": pool,
                "expectation": entry.model_dump(mode="json"),
                "window": span.key,
                "window_start": span.start.isoformat(),
                "window_end": span.end.isoformat(),
                "observed": count,
            }
            sent = mailbox.send(
                conn,
                tx,
                namespace=namespace,
                sender_id=kernel.runtime_id,
                supervisor_id=str(group["supervisor_id"]),
                to=mailbox.Address(instance=str(group["supervisor_id"])),
                parts=[
                    {
                        "text": f"{outcome}: pool {pool} of group {name} made {count} in "
                        f"{span.key}; expected {_describe(entry)}."
                    },
                    {"data": data},
                ],
                runtime_id=kernel.runtime_id,
                at=at,
                from_kernel=True,
            )
            message_id = str(sent["message_id"])
        conn.exec_driver_sql(
            "INSERT INTO expect_window_reports VALUES (?,?,?,?,?,?,?,?,?)",
            (namespace, name, pool, digest, span.key, outcome, count, message_id, at.isoformat()),
        )
        kernel.fault("group_windows.before_commit")
    return outcome


def tick(kernel: "Kernel", at: datetime | None = None) -> dict[str, str]:
    """The windowed-expectation step of the minute tick."""
    at = at or now()
    with kernel.database.read() as conn:
        groups = list(
            conn.exec_driver_sql(
                # A deleted group (slice 8) checks nothing more.
                "SELECT namespace,name,supervisor_id,spec_json,created_at FROM agent_groups "
                "WHERE state != 'deleted'"
            ).mappings()
        )
    outcomes: dict[str, str] = {}
    for group in groups:
        spec = GroupSpec.model_validate_json(group["spec_json"])
        for pool in spec.pools:
            for entry in pool.expect_window:
                label = (
                    f"{group['namespace']}/{group['name']}/{pool.name}/{entry_sha256(entry)[:12]}"
                )
                try:
                    outcomes[label] = check(kernel, group, pool.name, entry, at)
                except Exception:
                    # One failing check must not stop the others; the next tick retries it.
                    log.exception("windowed expectation check failed", extra={"code": label})
                    outcomes[label] = "error"
    return outcomes
