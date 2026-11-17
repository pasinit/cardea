"""Replies to effects, polled from their destination, and per-role call counts (ADR-039).

An installed effect operation may declare `replies` (`operations.ReplyPolling`). For each
of its succeeded calls in the last `window_days`, the kernel calls the declared poller
every `interval_seconds` with that call's thread: its receipt, arguments and output. The
poller reads the destination (for example a mailbox provider's thread) and returns:

- `Reply` entries it accepts, as mailbox parts. The kernel delivers each one, once, into
  the mailbox of the agent that made the call, sent by the kernel's runtime. The mailbox
  renders it as an untrusted ADR-031 context block on the agent's next run, like any mail.
- `Dropped` entries it refuses (an unknown sender, an unauthenticated message). The kernel
  records each one once as a content-free journal event, `operation.reply_dropped`, with
  only the operation, the receipt, the reason and a SHA-256 of the provider's message id.
  Journal triggers never see it, and no agent receives it.

Delivery is idempotent: a reply's mailbox `client_key` is derived from the receipt and the
provider's reply id, so polling the same thread again delivers nothing new. Nothing here
calls the destination except through the poller, and a poller failure leaves everything as
it was until the next tick.
"""

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from hashlib import sha256
from typing import TYPE_CHECKING, Any, cast

import sqlalchemy as sa
from pydantic import JsonValue

from cardea.kernel.models import OperationReceipt
from cardea.service import mailbox
from cardea.service.database import Database, Fault, borrow
from cardea.service.models import now
from cardea.service.operations import OperationContext

if TYPE_CHECKING:
    from cardea.service.kernel import Kernel

log = logging.getLogger(__name__)

DROPPED = "operation.reply_dropped"
# Calls that may have reached their destination count toward a role's limit.
COUNTED_STATES = ("running", "succeeded", "outcome_unknown")
MAX_REPLIES_PER_THREAD = 50
REASON = r"^[a-z][a-z0-9_]{0,63}$"


@dataclass(frozen=True)
class Reply:
    """A reply the poller accepts. `reply_id` is the destination's own id for it."""

    reply_id: str
    parts: list[dict[str, JsonValue]]


@dataclass(frozen=True)
class Dropped:
    """A reply the poller refuses. Only a digest of `reply_id` and the reason are kept."""

    reply_id: str
    reason: str


@dataclass(frozen=True)
class Polled:
    replies: tuple[Reply, ...] = ()
    dropped: tuple[Dropped, ...] = ()


Poller = Callable[[dict[str, JsonValue]], Awaitable[Polled]]


@dataclass
class Tally:
    """What one poll did, without content: for logs and tests."""

    threads: int = 0
    delivered: int = 0
    dropped: dict[str, int] = field(default_factory=dict)
    failed: int = 0

    def drop(self, reason: str) -> None:
        self.dropped[reason] = self.dropped.get(reason, 0) + 1


def _digest(*parts: str) -> str:
    return sha256("\0".join(parts).encode()).hexdigest()


def count_calls(database: Database, context: OperationContext, name: str, window: timedelta) -> int:
    """Calls of `name` by the caller's role since `window` ago, this call included.

    The role is the caller's group pool (every member it ever had), or the calling agent
    outside a group, or the task when no agent made the call.
    """
    since = now() - window
    with database.read() as conn:
        if context.group is not None and context.pool is not None:
            subjects = [
                str(r[0])
                for r in conn.exec_driver_sql(
                    "SELECT principal_id FROM group_members WHERE namespace=? AND group_name=? "
                    "AND pool=?",
                    (context.namespace, context.group, context.pool),
                )
            ]
        else:
            subjects = []
        rows = conn.exec_driver_sql(
            "SELECT task_id,snapshot FROM operations WHERE state IN (?,?,?) "
            "AND json_extract(snapshot,'$.request.name')=?",
            (*COUNTED_STATES, name),
        ).fetchall()
    count = 0
    for task_id, raw in rows:
        receipt = OperationReceipt.model_validate_json(raw)
        if receipt.request.resource.namespace != context.namespace:
            continue
        if receipt.created_at < since:
            continue
        if subjects:
            mine = receipt.request.subject_id in subjects
        elif context.agent_id is not None:
            mine = receipt.request.subject_id == context.agent_id
        else:
            mine = str(task_id) == context.task_id
        count += mine
    return count


def threads(conn: sa.Connection, name: str, since: datetime) -> list[dict[str, JsonValue]]:
    """Succeeded calls of `name` since `since`, as the poller's threads."""
    rows = conn.exec_driver_sql(
        "SELECT o.task_id,o.snapshot,b.agent_id FROM operations o "
        "JOIN task_bindings b ON b.task_id=o.task_id WHERE o.state='succeeded' "
        "AND json_extract(o.snapshot,'$.request.name')=?",
        (name,),
    ).fetchall()
    found: list[dict[str, JsonValue]] = []
    for task_id, raw, agent_id in rows:
        receipt = OperationReceipt.model_validate_json(raw)
        if receipt.created_at < since or receipt.result is None:
            continue
        recorded = receipt.request.arguments
        result = cast(dict[str, Any], receipt.result)
        if result.get("deduplicated"):
            # The first call of this effect owns the thread.
            continue
        found.append(
            {
                "operation": name,
                "receipt_id": receipt.receipt_id,
                "task_id": str(task_id),
                "namespace": receipt.request.resource.namespace,
                "agent_id": str(agent_id),
                "arguments": recorded.get("arguments", {}),
                "context": recorded.get("context", {}),
                "output": cast(JsonValue, result.get("output", {})),
                "created_at": receipt.created_at.isoformat(),
            }
        )
    return found


def deliver(kernel: "Kernel", thread: dict[str, JsonValue], polled: Polled, tally: Tally) -> None:
    """Commit one thread's new replies and drops in one transaction."""
    namespace = str(thread["namespace"])
    receipt_id = str(thread["receipt_id"])
    task_id = str(thread["task_id"])
    agent = str(thread["agent_id"])
    at = now()
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        seen = {
            json.loads(row[0]).get("reply_sha256")
            for row in conn.exec_driver_sql(
                "SELECT payload FROM events WHERE task_id=? AND event_type=? AND correlation_id=?",
                (task_id, DROPPED, receipt_id),
            )
        }
        supervisor = conn.exec_driver_sql(
            "SELECT supervisor_id FROM task_bindings WHERE task_id=?", (task_id,)
        ).scalar_one()
        drops = list(polled.dropped)
        for reply in polled.replies[:MAX_REPLIES_PER_THREAD]:
            key = "reply." + _digest(receipt_id, reply.reply_id)[:40]
            exists = conn.exec_driver_sql(
                "SELECT 1 FROM mailbox_messages WHERE namespace=? AND sender_id=? AND client_key=?",
                (namespace, kernel.runtime_id, key),
            ).first()
            if exists is not None:
                continue
            parts: list[dict[str, JsonValue]] = [
                *reply.parts,
                {
                    "data": {
                        "reply_to": {
                            "operation": thread["operation"],
                            "receipt_id": receipt_id,
                        }
                    }
                },
            ]
            savepoint = conn.begin_nested()
            try:
                mailbox.send(
                    conn,
                    tx,
                    namespace=namespace,
                    sender_id=kernel.runtime_id,
                    supervisor_id=str(supervisor),
                    to=mailbox.Address(instance=agent),
                    parts=parts,
                    runtime_id=kernel.runtime_id,
                    at=at,
                    client_key=key,
                    sender_task_id=task_id,
                )
            except Fault as refused:
                # For example a removed member: nobody can read it, so it is dropped.
                savepoint.rollback()
                reason = {"body_too_large": "too_large", "invalid_parts": "invalid"}.get(
                    refused.code, "recipient_unavailable"
                )
                drops.append(Dropped(reply.reply_id, reason))
                continue
            savepoint.commit()
            tally.delivered += 1
        for dropped in drops:
            digest = _digest(dropped.reply_id)
            if digest in seen:
                continue
            seen.add(digest)
            tx.append_event(
                namespace=namespace,
                event_type=DROPPED,
                producer_id=kernel.runtime_id,
                correlation_id=receipt_id,
                task_id=task_id,
                at=at,
                payload={
                    "operation": str(thread["operation"]),
                    "receipt_id": receipt_id,
                    "reason": dropped.reason,
                    "reply_sha256": digest,
                },
            )
            tally.drop(dropped.reason)


def dropped_counts(conn: sa.Connection, namespace: str | None = None) -> dict[str, int]:
    """Dropped replies by reason, from the journal; never their content."""
    counts: dict[str, int] = {}
    for (raw,) in conn.exec_driver_sql(
        "SELECT payload FROM events WHERE event_type=?"
        + (" AND namespace=?" if namespace is not None else ""),
        (DROPPED, namespace) if namespace is not None else (DROPPED,),
    ):
        reason = str(json.loads(raw).get("reason"))
        counts[reason] = counts.get(reason, 0) + 1
    return counts


async def _complete(call: Awaitable[Any]) -> Any:
    return await call


class ReplyPoller:
    """Polls every operation that declares `replies`, each on its own interval."""

    def __init__(self, kernel: "Kernel") -> None:
        self.kernel = kernel
        self.last: dict[str, float] = {}

    def names(self) -> list[str]:
        return [
            name
            for name, descriptor in self.kernel.operations.descriptors.items()
            if descriptor.cardea.replies is not None
        ]

    def poll(self, *, force: bool = False) -> Tally:
        """One pass over the operations that are due (all of them with `force`)."""
        tally = Tally()
        clock = time.monotonic()
        for name in self.names():
            replies = self.kernel.operations.get(name).cardea.replies
            assert replies is not None
            if not force and clock - self.last.get(name, -1e12) < replies.interval_seconds:
                continue
            self.last[name] = clock
            poller = self.kernel.operations.poller(name)
            with self.kernel.database.read() as conn:
                found = threads(conn, name, now() - timedelta(days=replies.window_days))
            for thread in found:
                tally.threads += 1
                try:
                    polled: object = asyncio.run(_complete(poller(thread)))
                    if not isinstance(polled, Polled):
                        raise TypeError("A reply poller must return Polled")
                    for dropped in polled.dropped:
                        if not re.fullmatch(REASON, dropped.reason):
                            raise ValueError("A dropped reply needs a short reason")
                    deliver(self.kernel, thread, polled, tally)
                except Exception as error:
                    # The class name only: an error text may hold an address or a reply.
                    log.warning("reply poll of %s failed: %s", name, type(error).__name__)
                    tally.failed += 1
        if tally.threads:
            log.info(
                "reply poll: %d threads, %d delivered, %d failed, dropped %s",
                tally.threads,
                tally.delivered,
                tally.failed,
                dict(sorted(tally.dropped.items())),
            )
        return tally
