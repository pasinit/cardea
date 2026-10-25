"""Service logging: one JSON object per line on stderr (journald under systemd), and
optional per-attempt log files under the data directory."""

import json
import logging
import os
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

# Structured fields callers may attach via `extra`; anything else is dropped.
LOG_FIELDS = (
    "task_id",
    "attempt",
    "harness",
    "model",
    "code",
    "reason",
    "method",
    "path",
    "job",
    "unit",
    "pid",
    "argv",
    "returncode",
    "signal",
    "step",
    "stream",
    "operation",
    "operation_id",
    "kind",
    "outcome",
    "duration_ms",
    "duration_s",
    "elapsed_s",
    "since_model_s",
    "since_tool_s",
    "since_output_s",
    "tools_in_flight",
    "tool_calls",
    "turns",
    "tools",
    "mcp",
    "blocks",
    "stop_reason",
    "error",
    "input_tokens",
    "output_tokens",
    "cost_usd",
    "lease_expires_at",
    "timeout_s",
    "cap_bytes",
    "output_bytes",
    "output_lines_dropped",
)
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
ATTEMPT_LOGS = "attempt-logs"
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")


class JournalFormatter(logging.Formatter):
    """One JSON object per line; never includes request bodies or credentials."""

    def __init__(self, timestamps: bool = False) -> None:
        super().__init__()
        # journald stamps each line itself; a standalone file needs its own time.
        self.timestamps = timestamps

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {}
        if self.timestamps:
            entry["ts"] = datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            )
        entry |= {"level": record.levelname, "logger": record.name, "msg": record.getMessage()}
        entry |= {k: getattr(record, k) for k in LOG_FIELDS if hasattr(record, k)}
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def log_level(configured: str = "INFO") -> str:
    """`CARDEA_LOG_LEVEL` overrides the configured level; unknown values fall back to it."""
    override = os.environ.get("CARDEA_LOG_LEVEL", "").upper()
    return override if override in LEVELS else configured.upper()


def configure_logging(level: str = "INFO") -> None:
    """Route every `cardea.*` record at `level` or above to stderr as JSON lines."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JournalFormatter())
    root = logging.getLogger("cardea")
    root.handlers[:] = [handler]
    root.setLevel(log_level(level))
    root.propagate = False


class _TaskFilter(logging.Filter):
    def __init__(self, task_id: str) -> None:
        super().__init__()
        self.task_id = task_id

    def filter(self, record: logging.LogRecord) -> bool:
        return getattr(record, "task_id", None) == self.task_id


def prune(directory: Path, keep: int) -> int:
    """Delete all but the `keep` most recently modified attempt logs; returns how many."""
    logs = sorted(
        (p for p in directory.glob("*.log") if p.is_file() and not p.is_symlink()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    removed = 0
    for path in logs[keep:]:
        try:
            path.unlink()
            removed += 1
        except FileNotFoundError:
            pass
    return removed


@contextmanager
def attempt_log_file(directory: Path, task_id: str, attempt: int, keep: int) -> Iterator[Path]:
    """Copy every `cardea.*` record carrying `task_id` to a private per-attempt file.

    The file receives what the service log receives (the same records at the same level),
    with timestamps. Older files beyond `keep` are deleted when the attempt ends.
    """
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / f"{_UNSAFE.sub('_', task_id)}-{attempt}.log"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    stream = os.fdopen(descriptor, "a", encoding="utf-8")
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JournalFormatter(timestamps=True))
    handler.addFilter(_TaskFilter(task_id))
    root = logging.getLogger("cardea")
    root.addHandler(handler)
    try:
        yield path
    finally:
        root.removeHandler(handler)
        handler.close()
        stream.close()
        prune(directory, keep)
