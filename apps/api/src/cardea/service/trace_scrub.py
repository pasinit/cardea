"""Span scrubbing and caps: what a trace span may carry out of the kernel.

A pure function: no I/O, no clock. `scrub_run` takes the spans of one run and returns
spans that are safe to hand to an exporter, plus counts of what was dropped.

- Attributes: only names on the allowlist survive. Values are scalars (str, int, float,
  bool); anything else is dropped. A string value is redacted (secrets, home paths, prompt
  text) and clipped to `max_attr_len`. At most `max_attrs` attributes per span or event.
- Events: at most `max_events` per span; the rest are counted, not kept.
- Spans: at most `max_spans` per run; the rest are counted, not kept.
- Truncation is visible: a clipped string ends with `TRUNCATED`, and a span that lost
  attributes or events carries `dropped_attributes` / `dropped_events`.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

REDACTED = "[redacted]"
TRUNCATED = "...[truncated]"

DEFAULT_ALLOWED = frozenset(
    {
        "cardea.run_id",
        "cardea.group",
        "cardea.member",
        "cardea.operation",
        "cardea.outcome",
        "cardea.error_code",
        "cardea.duration_ms",
        "cardea.attempt",
        "http.method",
        "http.status_code",
    }
)

# Prompt-like attribute names are redacted even if someone allowlists them.
_PROMPT_KEYS = re.compile(r"prompt|message|content|instruction|completion", re.IGNORECASE)

_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY|$)", re.S),
    re.compile(r"\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*"),
    re.compile(r"\b(?:sk|pk|rk|ghp|gho|ghs|ghu|github_pat|xox[abprs]|AKIA|ASIA)[-_A-Za-z0-9]{12,}"),
    re.compile(r"(?i)\b(?:api[_-]?key|secret|token|password|passwd|authorization)\b\s*[:=]\s*\S+"),
    re.compile(r"\b[A-Za-z0-9+/_-]{40,}={0,2}"),
)
_PATH = re.compile(r"(?:(?<=\s)|^|(?<=[=:\"']))(?:/[^\s/\"']+){2,}|\b[A-Za-z]:\\[^\s\"']+")


@dataclass(frozen=True)
class Limits:
    max_attrs: int = 32
    max_attr_len: int = 256
    max_events: int = 16
    max_spans: int = 512
    allowed: frozenset[str] = DEFAULT_ALLOWED


DEFAULT_LIMITS = Limits()


@dataclass(frozen=True)
class Scrubbed:
    spans: list[dict[str, Any]]
    dropped_spans: int = 0
    stats: dict[str, int] = field(default_factory=dict)


def redact(value: str) -> str:
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub(REDACTED, value)
    return _PATH.sub(REDACTED, value)


def _clip(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(limit - len(TRUNCATED), 0)] + TRUNCATED


def _attrs(raw: Any, limits: Limits) -> tuple[dict[str, Any], int]:
    out: dict[str, Any] = {}
    dropped = 0
    if not isinstance(raw, Mapping):
        return out, 0
    for name, value in raw.items():
        if name not in limits.allowed or len(out) >= limits.max_attrs:
            dropped += 1
            continue
        if isinstance(value, bool | int | float):
            out[name] = value
        elif isinstance(value, str):
            if _PROMPT_KEYS.search(name):
                out[name] = REDACTED
            else:
                out[name] = _clip(redact(value), limits.max_attr_len)
        else:
            dropped += 1
    return out, dropped


def _event(raw: Any, limits: Limits) -> dict[str, Any]:
    event = raw if isinstance(raw, Mapping) else {}
    attrs, dropped = _attrs(event.get("attributes"), limits)
    name = event.get("name")
    out: dict[str, Any] = {
        "name": _clip(redact(name), limits.max_attr_len) if isinstance(name, str) else "",
        "attributes": attrs,
    }
    if dropped:
        out["dropped_attributes"] = dropped
    return out


def scrub_span(raw: Mapping[str, Any], limits: Limits = DEFAULT_LIMITS) -> dict[str, Any]:
    """One span: an allowlisted, redacted, capped copy. The input is not modified."""
    attrs, dropped = _attrs(raw.get("attributes"), limits)
    raw_events = raw.get("events")
    events_in = list(raw_events) if isinstance(raw_events, list | tuple) else []
    events = [_event(e, limits) for e in events_in[: limits.max_events]]
    name = raw.get("name")
    out: dict[str, Any] = {
        "name": _clip(redact(name), limits.max_attr_len) if isinstance(name, str) else "",
        "attributes": attrs,
        "events": events,
    }
    for key in ("trace_id", "span_id", "parent_id", "start", "end", "status"):
        value = raw.get(key)
        if isinstance(value, str | int | float) and not isinstance(value, bool):
            out[key] = _clip(value, 64) if isinstance(value, str) else value
    if dropped:
        out["dropped_attributes"] = dropped
    if len(events_in) > limits.max_events:
        out["dropped_events"] = len(events_in) - limits.max_events
    return out


def scrub_run(spans: Iterable[Mapping[str, Any]], limits: Limits = DEFAULT_LIMITS) -> Scrubbed:
    """The spans of one run, capped at `limits.max_spans`; the excess is counted."""
    kept: list[dict[str, Any]] = []
    dropped = 0
    for span in spans:
        if len(kept) >= limits.max_spans:
            dropped += 1
            continue
        kept.append(scrub_span(span, limits))
    return Scrubbed(spans=kept, dropped_spans=dropped, stats={"kept": len(kept)})
