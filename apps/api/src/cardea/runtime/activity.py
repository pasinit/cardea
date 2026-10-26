"""Structured, bounded activity logs for one agent-run attempt.

The kernel binds an `AttemptActivity` around each attempt; harness adapters report what
they observe through it (process spawn and exit, model turns, harness stderr), and the
runtime reports tool calls. Every record carries the attempt's `task_id` and `attempt`, so
an operator can reconstruct an attempt from the service log, and a periodic heartbeat says
how long ago the model and the tools were last active, which shows where a stall begins.

What is logged is metadata only: operation names and ids, durations, outcome codes, token
counts, exit codes. Tool arguments and outputs, prompts and completions are never passed
in. Harness stderr is the one free-text source; it is truncated per line, capped per
attempt and scrubbed of bearer tokens, key-like assignments and every secret registered
with `redact`.
"""

import logging
import re
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("cardea.attempt")

REDACTED = "[redacted]"
# Credentials as they typically appear in tool and HTTP-client diagnostics.
SECRET_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[^\s\"',;]+"),
    re.compile(
        r"(?i)((?:api[_-]?key|access[_-]?token|auth[_-]?token|oauth[_-]?token|token|secret"
        r"|password|authorization|x-api-key)[\"']?\s*[:=]\s*[\"']?)(?!bearer\s)[^\s\"',;}]+"
    ),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
)
# Shorter values are not registered: replacing them would mangle ordinary text.
MIN_SECRET = 8


@dataclass(frozen=True)
class OutputLimits:
    """Bounds on harness free-text output logged for one attempt."""

    line_bytes: int = 2048
    total_bytes: int = 262144


def scrub(text: str, secrets: tuple[str, ...] = ()) -> str:
    """`text` with registered secrets and credential-shaped substrings replaced."""
    for secret in secrets:
        text = text.replace(secret, REDACTED)
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(lambda m: (m.group(1) if m.groups() else "") + REDACTED, text)
    return text


def _ago(now: float, then: float | None) -> float | None:
    return None if then is None else round(now - then, 1)


class AttemptActivity:
    """Activity of one attempt. Safe to call from several threads and event loops."""

    def __init__(
        self,
        task_id: str,
        attempt: int,
        *,
        harness: str = "",
        model: str = "",
        limits: OutputLimits | None = None,
        heartbeat_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        logger: logging.Logger = log,
    ) -> None:
        self.task_id, self.attempt = task_id, attempt
        self.harness, self.model = harness, model
        self.limits = limits or OutputLimits()
        self.heartbeat_seconds = heartbeat_seconds
        self.clock = clock
        self.logger = logger
        self.started = clock()
        self.last_model: float | None = None
        self.last_tool: float | None = None
        self.last_output: float | None = None
        self.tools_in_flight = 0
        self.tool_calls = 0
        # Calls refused before admission (no receipt), for example `tool_busy`.
        self.tool_calls_refused = 0
        self.turns = 0
        self.output_bytes = 0
        self.output_dropped = 0
        self._next_heartbeat = self.started + heartbeat_seconds
        self._secrets: tuple[str, ...] = ()
        self._lock = threading.Lock()

    # Records

    def fields(self, **extra: Any) -> dict[str, Any]:
        base: dict[str, Any] = {"task_id": self.task_id, "attempt": self.attempt}
        if self.harness:
            base["harness"] = self.harness
        if self.model:
            base["model"] = self.model
        return base | {k: v for k, v in extra.items() if v is not None}

    def event(
        self, message: str, level: int = logging.INFO, *, exc_info: bool = False, **extra: Any
    ) -> None:
        self.logger.log(level, message, exc_info=exc_info, extra=self.fields(**extra))

    def redact(self, *secrets: str | None) -> None:
        """Register values that must never appear in logged harness output."""
        with self._lock:
            known = set(self._secrets)
            known.update(s for s in secrets if s and len(s) >= MIN_SECRET)
            # Longest first, so a secret containing another is replaced whole.
            self._secrets = tuple(sorted(known, key=len, reverse=True))

    # Harness process

    def process_started(self, pid: int | None, argv: list[str] | None = None) -> None:
        self.event("harness process started", pid=pid, argv=argv)

    def process_exited(self, returncode: int | None, duration: float) -> None:
        signal = -returncode if returncode is not None and returncode < 0 else None
        self.event(
            "harness process exited",
            logging.INFO if returncode == 0 else logging.WARNING,
            returncode=returncode,
            signal=signal,
            duration_s=round(duration, 3),
        )

    # Model loop

    def model_activity(self, step: str, level: int = logging.INFO, **extra: Any) -> None:
        """The harness reported a model-loop event (a turn started or finished, a result)."""
        with self._lock:
            self.last_model = self.clock()
            if step == "turn":
                self.turns += 1
        self.event(f"model {step}", level, step=step, **extra)

    # Tool calls (reported by the runtime, which owns every tool call)

    def tool_started(self, operation: str, operation_id: str, kind: str = "call") -> None:
        with self._lock:
            self.last_tool = self.clock()
            self.tools_in_flight += 1
            self.tool_calls += 1
        self.event("tool call started", operation=operation, operation_id=operation_id, kind=kind)

    def tool_refused(self, operation: str, code: str, *, kind: str = "call", **extra: Any) -> None:
        """A call refused before admission: nothing ran and no receipt was written."""
        with self._lock:
            self.last_tool = self.clock()
            self.tool_calls_refused += 1
        self.event(
            "tool call refused", logging.WARNING, operation=operation, kind=kind, code=code, **extra
        )

    def tool_finished(
        self,
        operation: str,
        operation_id: str,
        *,
        kind: str = "call",
        duration_ms: int,
        outcome: str,
        reason: str = "",
    ) -> None:
        with self._lock:
            self.last_tool = self.clock()
            self.tools_in_flight = max(0, self.tools_in_flight - 1)
        self.event(
            "tool call finished",
            logging.INFO if outcome == "ok" else logging.WARNING,
            operation=operation,
            operation_id=operation_id,
            kind=kind,
            duration_ms=duration_ms,
            outcome=outcome,
            reason=reason or None,
        )

    # Harness free-text output

    def harness_output(
        self, line: str | bytes, stream: str = "stderr", level: int = logging.INFO
    ) -> None:
        """Log one line of harness output, truncated, scrubbed and within the attempt cap."""
        text = line.decode(errors="replace") if isinstance(line, bytes) else line
        text = text.rstrip("\r\n")
        if not text.strip():
            return
        raw = text.encode()
        cut = len(raw) - self.limits.line_bytes
        if cut > 0:
            text = raw[: self.limits.line_bytes].decode(errors="ignore")
        with self._lock:
            self.last_output = self.clock()
            if self.output_bytes >= self.limits.total_bytes:
                self.output_dropped += 1
                return
            self.output_bytes += min(len(raw), self.limits.line_bytes)
            capped = self.output_bytes >= self.limits.total_bytes
            secrets = self._secrets
        text = scrub(text, secrets)
        if cut > 0:
            text += f" ...[truncated {cut} bytes]"
        self.event(f"harness {stream}: {text}", level, stream=stream)
        if capped:
            self.event(
                "harness output cap reached; further lines are counted, not logged",
                logging.WARNING,
                cap_bytes=self.limits.total_bytes,
            )

    # Heartbeat

    def tick(self) -> bool:
        """Log a heartbeat when one is due; returns whether it did."""
        now = self.clock()
        with self._lock:
            if self.heartbeat_seconds <= 0 or now < self._next_heartbeat:
                return False
            self._next_heartbeat = now + self.heartbeat_seconds
        self.heartbeat(now)
        return True

    def heartbeat(self, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        with self._lock:
            fields: dict[str, Any] = {
                "elapsed_s": round(now - self.started, 1),
                "since_model_s": _ago(now, self.last_model),
                "since_tool_s": _ago(now, self.last_tool),
                "since_output_s": _ago(now, self.last_output),
                "tools_in_flight": self.tools_in_flight,
                "tool_calls": self.tool_calls,
                "tool_calls_refused": self.tool_calls_refused,
                "turns": self.turns,
            }
        # Absent `since_*` fields mean "never" in this attempt.
        self.event("attempt still running", **fields)

    def summary(self) -> dict[str, Any]:
        with self._lock:
            return {
                "turns": self.turns,
                "tool_calls": self.tool_calls,
                "tool_calls_refused": self.tool_calls_refused,
                "output_bytes": self.output_bytes,
                "output_lines_dropped": self.output_dropped,
            }


_CURRENT: ContextVar[AttemptActivity | None] = ContextVar("cardea_attempt", default=None)


def current() -> AttemptActivity:
    """The attempt bound to this context, else a detached one (no task id) for direct use."""
    bound = _CURRENT.get()
    return bound if bound is not None else AttemptActivity("", 0)


@contextmanager
def bind(activity: AttemptActivity) -> Iterator[AttemptActivity]:
    """Bind `activity` for this context; tasks and `asyncio.run` started inside inherit it."""
    token = _CURRENT.set(activity)
    try:
        yield activity
    finally:
        _CURRENT.reset(token)
