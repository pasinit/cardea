"""W3C trace context for agent runs: one trace per run, one task span per attempt.

The run's root span is created with its first attempt and its `traceparent` is stored on the
run's `agent_runs` row, so every later attempt (a retry, a resume after approval, a restart
after a crash) joins the same trace. Each attempt is a child span of the run span. Spans are
metadata only (ids and names) and are reported on the attempt's log lines. Tracing is
diagnostic: it never changes effect semantics, and a failure here never fails a run.
"""

import re
import secrets
from dataclasses import dataclass

import sqlalchemy as sa

TRACEPARENT = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")


@dataclass(frozen=True)
class Span:
    trace_id: str
    span_id: str
    parent_span_id: str | None = None

    @property
    def traceparent(self) -> str:
        return f"00-{self.trace_id}-{self.span_id}-01"


def _hex(nbytes: int) -> str:
    while True:
        value = secrets.token_hex(nbytes)
        if int(value, 16):  # all-zero ids are invalid
            return value


def new_root() -> Span:
    return Span(_hex(16), _hex(8))


def parse(traceparent: str) -> Span | None:
    found = TRACEPARENT.match(traceparent)
    if found is None or not int(found[1], 16) or not int(found[2], 16):
        return None
    return Span(found[1], found[2])


def child(parent: Span) -> Span:
    return Span(parent.trace_id, _hex(8), parent.span_id)


def run_span(conn: sa.Connection, task_id: str) -> Span:
    """The run's root span: stored with the first attempt, read by every later one."""
    row = conn.exec_driver_sql(
        "SELECT traceparent FROM agent_runs WHERE task_id=?", (task_id,)
    ).first()
    stored = parse(row[0]) if row is not None and row[0] else None
    if stored is not None:
        return stored
    root = new_root()
    if row is None:
        conn.exec_driver_sql(
            "INSERT INTO agent_runs(task_id, traceparent) VALUES (?,?)",
            (task_id, root.traceparent),
        )
    else:
        conn.exec_driver_sql(
            "UPDATE agent_runs SET traceparent=? WHERE task_id=?", (root.traceparent, task_id)
        )
    return root


def attempt_span(conn: sa.Connection, task_id: str) -> Span:
    """A new task span, child of the run span."""
    return child(run_span(conn, task_id))
