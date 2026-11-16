"""Built-in context sources and their byte budgets (ADR-034 decision 5, ADR-031 amended).

A run's context blocks are fetched in one read snapshot and fitted to their budgets:

- each block to its entry's `max_bytes`, else its source's default below;
- all blocks together to the run's `context_max_bytes` (24 KiB default, 64 KiB at most).

A source that returns a list drops its oldest entries first and says how many with
`truncated`. The inbox and the caller's claims are filled first and never dropped
entirely: the inbox shows exactly the mail its run leased, and claims keep at least their
count. Any other output that does not fit is replaced by a size note.

`runs.recent` is defined here: the calling principal's own last runs in the namespace.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import JsonValue

from cardea.kernel.models import ResourceRef
from cardea.kernel.store import StoreError
from cardea.service.database import Database, borrow
from cardea.service.operations import OperationContext, OperationDescriptor

RECENT = "runs.recent"
INBOX = "mailbox.inbox"
CLAIMED = "records.claimed"
# Sources filled before every other block, in this order; never dropped entirely.
PRIORITY = (INBOX, CLAIMED)
# Default block budgets (ADR-034 decision 5.1); other operations share the total only.
DEFAULT_BYTES = {
    CLAIMED: 8192,
    INBOX: 6144,
    RECENT: 4096,
    "records.summary": 3072,
    "records.query": 2048,
}
# The list each built-in source's oldest entries are dropped from.
LISTS = {
    CLAIMED: "claims",
    RECENT: "runs",
    "records.summary": "available",
    "records.query": "records",
}
# Sources whose data the kernel reads itself, so their blocks share one snapshot.
SNAPSHOT_SOURCES = frozenset({*DEFAULT_BYTES})


def trim(name: str, output: Any, budget: int, size: Callable[[Any], int]) -> tuple[Any, bool]:
    """`output` whose rendered block is within `budget`, and whether anything was left out."""
    if size(output) <= budget:
        return output, False
    key = LISTS.get(name)
    if isinstance(output, dict) and isinstance(output.get(key), list):
        items = list(output[key])
        dropped = 0
        trimmed = dict(output)
        while items:
            items.pop(0)
            dropped += 1
            trimmed = {
                **output,
                key: items,
                "truncated": int(output.get("truncated") or 0) + dropped,
            }
            if size(trimmed) <= budget:
                break
        if size(trimmed) <= budget:
            return trimmed, True
    return {"omitted": True, "bytes": size(output), "max_bytes": budget}, True


@dataclass(frozen=True)
class Fetched:
    """One context call's result: its operation, budget and output (None if unavailable)."""

    name: str
    max_bytes: int | None
    output: Any
    block: Callable[[Any], str]


def fit(fetched: list[Fetched], total: int) -> list[str]:
    """Render blocks priority first, each within its own and the remaining total budget."""
    order = sorted(
        range(len(fetched)),
        key=lambda i: (
            PRIORITY.index(fetched[i].name) if fetched[i].name in PRIORITY else len(PRIORITY),
            i,
        ),
    )
    left = total
    rendered: list[str] = []
    for index in order:
        item = fetched[index]
        if item.output is None:
            block = item.block(None)
        elif item.name == INBOX:
            # Leased mail is consumed when the run succeeds, so it is shown whole; the
            # lease itself was already bounded by this block's budget.
            block = item.block(item.output)
        else:
            own = item.max_bytes or DEFAULT_BYTES.get(item.name) or total
            budget = min(own, left)
            output, _ = trim(
                item.name, item.output, max(budget, 0), lambda o: len(item.block(o).encode())
            )
            if item.name == CLAIMED and isinstance(output, dict) and output.get("omitted"):
                # Claims are never dropped entirely: at least how many there are.
                claims = item.output.get("claims", []) if isinstance(item.output, dict) else []
                output = {"claims": [], "truncated": len(claims)}
            block = item.block(output)
        left -= len(block.encode())
        rendered.append(block)
    return rendered


def _tail(text: str, limit: int) -> str:
    return text if len(text) <= limit else "…" + text[-limit:]


class RecentRuns:
    """`runs.recent`: the caller's own finished runs in this namespace, oldest first."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def handler(
        self, arguments: dict[str, JsonValue], context: OperationContext
    ) -> dict[str, JsonValue]:
        limit = arguments.get("limit", 5)
        tail = arguments.get("tail_bytes", 400)
        assert isinstance(limit, int) and isinstance(tail, int)
        with self.database.read() as conn, borrow(conn) as tx:
            agent = conn.exec_driver_sql(
                "SELECT agent_id FROM task_bindings WHERE task_id=?", (context.task_id,)
            ).scalar_one()
            rows = conn.exec_driver_sql(
                "SELECT b.task_id,b.request_id,t.state,t.snapshot FROM task_bindings b "
                "JOIN tasks t USING(task_id) WHERE b.agent_id=? AND b.namespace=? "
                "AND b.task_id<>? AND t.state IN ('succeeded','failed','cancelled') "
                "ORDER BY json_extract(t.snapshot,'$.updated_at') DESC, b.rowid DESC LIMIT ?",
                (agent, context.namespace, context.task_id, limit + 1),
            ).fetchall()
            runs: list[JsonValue] = []
            for task_id, request_id, state, snapshot in rows[:limit]:
                task = json.loads(snapshot)
                text = ""
                try:
                    version = tx.latest_object(
                        ResourceRef(namespace=context.namespace, resource_id=f"{task_id}.output")
                    )
                    document = json.loads(tx.read_object(version))
                    text = str(document.get("text") or "") if isinstance(document, dict) else ""
                except StoreError:
                    pass
                touched = [
                    f"{c}/{r}"
                    for c, r in conn.exec_driver_sql(
                        "SELECT DISTINCT collection,record_id FROM record_events WHERE task_id=? "
                        "ORDER BY collection,record_id LIMIT 16",
                        (task_id,),
                    )
                ]
                failure = task.get("failure") or {}
                runs.append(
                    {
                        "task_id": task_id,
                        "request_id": request_id,
                        "outcome": state,
                        "failure_code": failure.get("code") if isinstance(failure, dict) else None,
                        "finished_at": task.get("updated_at"),
                        "records_touched": list(touched),
                        "result_tail": _tail(text, tail),
                    }
                )
        runs.reverse()
        return {"runs": runs, "truncated": 0, "more": len(rows) > limit}


DESCRIPTORS: tuple[OperationDescriptor, ...] = (
    OperationDescriptor.model_validate(
        {
            "tool": {
                "name": RECENT,
                "title": "Your own recent runs",
                "description": "Your last finished runs in this namespace, oldest first: "
                "outcome, records touched and the tail of each result.",
                "inputSchema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                        "tail_bytes": {"type": "integer", "minimum": 0, "maximum": 2048},
                    },
                },
                "outputSchema": {
                    "type": "object",
                    "properties": {
                        "runs": {"type": "array"},
                        "truncated": {"type": "integer"},
                        "more": {"type": "boolean"},
                    },
                    "required": ["runs"],
                },
            },
            "cardea": {
                "version": 1,
                "effect": "read",
                "handler": "cardea.runs.recent",
                "timeout_seconds": 10,
            },
        },
        context={"builtin": True},
    ),
)

