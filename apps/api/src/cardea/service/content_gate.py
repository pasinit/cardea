"""The content gate: what may leave the host (ADR-037 decision 4).

Kernel-owned and host-configured. Before an operation sends a change or text to a
repository host, it calls `enforce`, which checks:

- the added lines of a unified diff, located as `<file>:<line>`;
- free text fields (commit messages, titles, bodies, comments), located as `<field>` or
  `<field>:<line>`.

The rules are Gitleaks's secret rules, run by the pinned Gitleaks binary (the host
owner's configuration, or Gitleaks's built-in rules), and a host-owner denylist of
case-insensitive terms and regular expressions.

A finding refuses the call before anything is sent. The refusal names only the rule and
the location, never the matched text, and by default it mails the run's supervisor. The
gate fails closed: if the gate is missing or misconfigured, or the scanner fails, the call
is refused too. Denylist terms are never logged and never echoed.
"""

import hashlib
import json
import logging
import re
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import sqlalchemy as sa
from pydantic import JsonValue

from cardea.service.database import Database, Fault, borrow, private_file
from cardea.service.models import ContentGateConfig, now
from cardea.service.operations import ContentRefused, OperationContext, OperationRejected

log = logging.getLogger(__name__)

MAX_TERM_CHARS = 512
MAX_FINDINGS_SHOWN = 10
# Never trust the content's own exemptions: an inline `gitleaks:allow` comment, a
# `.gitleaksignore` file or a baseline would let the agent exempt what it sends.
SCANNER_FLAGS = (
    "stdin",
    "--no-banner",
    "--redact",
    "--ignore-gitleaks-allow",
    "--exit-code",
    "0",
    "--log-level",
    "error",
    "--report-format",
    "json",
    "--report-path",
    "-",
)


@dataclass(frozen=True)
class Finding:
    """A rule that matched, and where. Its line stays in memory only, to redact copies."""

    rule: str
    location: str
    line: str = field(default="", repr=False, compare=False)

    def describe(self) -> str:
        return f"{self.rule} at {self.location}"


def _unavailable(reason: str) -> Fault:
    log.error("content gate unavailable", extra={"code": reason})
    return Fault("content_gate_unavailable", f"The content gate cannot run ({reason})", 503)


# Inputs


def added_lines(diff: str) -> list[tuple[str, str]]:
    """`(location, text)` for every added line of a unified diff.

    The location is `<new file>:<line>`. A diff whose added lines cannot be placed in a
    hunk is refused rather than scanned partly.
    """
    found: list[tuple[str, str]] = []
    path: str | None = None
    line: int | None = None
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            target = raw[4:].split("\t", 1)[0].strip()
            path = None if target == "/dev/null" else target.removeprefix("b/")
            line = None
        elif raw.startswith("--- ") and line is None:
            continue
        elif raw.startswith("@@"):
            hunk = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
            if hunk is None:
                raise Fault("invalid_diff", "A hunk header is malformed", 422)
            line = int(hunk.group(1))
        elif raw.startswith("+"):
            if path is None or line is None:
                raise Fault("invalid_diff", "An added line is outside a file's hunk", 422)
            found.append((f"{path}:{line}", raw[1:]))
            line += 1
        elif raw.startswith(" ") and line is not None:
            line += 1
    return found


def text_lines(texts: Mapping[str, str]) -> list[tuple[str, str]]:
    """`(location, text)` for every line of every field; one-line fields are named alone."""
    found: list[tuple[str, str]] = []
    for name, value in texts.items():
        lines = value.splitlines() or [""]
        for number, text in enumerate(lines, start=1):
            found.append((name if len(lines) == 1 else f"{name}:{number}", text))
    return found


# Rules


@dataclass(frozen=True)
class Denylist:
    """Host-owner terms and patterns; rule IDs give the file's line, never the term."""

    terms: tuple[tuple[str, str], ...] = ()
    patterns: tuple[tuple[str, re.Pattern[str]], ...] = ()

    @classmethod
    def load(cls, path: Path | None) -> "Denylist":
        if path is None:
            return cls()
        try:
            content = private_file(path).decode("utf-8")
        except (Fault, UnicodeDecodeError, OSError):
            raise _unavailable("denylist_unreadable") from None
        terms: list[tuple[str, str]] = []
        patterns: list[tuple[str, re.Pattern[str]]] = []
        for number, raw in enumerate(content.splitlines(), start=1):
            entry = raw.strip()
            if not entry or entry.startswith("#"):
                continue
            if len(entry) > MAX_TERM_CHARS:
                raise _unavailable(f"denylist_line_{number}_too_long")
            rule = f"denylist:{number}"
            if entry.startswith("re:"):
                try:
                    compiled = re.compile(entry[3:], re.IGNORECASE)
                except re.error:
                    raise _unavailable(f"denylist_line_{number}_invalid") from None
                if compiled.search(""):
                    # A pattern that matches nothing in particular would refuse everything
                    # and point at no term; treat it as a configuration error.
                    raise _unavailable(f"denylist_line_{number}_matches_empty")
                patterns.append((rule, compiled))
            else:
                terms.append((rule, entry.casefold()))
        return cls(tuple(terms), tuple(patterns))

    def check(self, units: list[tuple[str, str]]) -> list[Finding]:
        findings: list[Finding] = []
        for location, text in units:
            folded = text.casefold()
            for rule, term in self.terms:
                if term in folded:
                    findings.append(Finding(rule, location, text))
            for rule, pattern in self.patterns:
                if pattern.search(text):
                    findings.append(Finding(rule, location, text))
        return findings


class Scanner:
    """The pinned Gitleaks binary on stdin, with every exemption disabled."""

    def __init__(self, config: ContentGateConfig) -> None:
        self.config = config
        try:
            binary = config.gitleaks_binary.read_bytes()
        except OSError:
            raise _unavailable("scanner_missing") from None
        if hashlib.sha256(binary).hexdigest() != config.gitleaks_sha256:
            raise _unavailable("scanner_digest_mismatch")
        if config.gitleaks_config is not None:
            try:
                private_file(config.gitleaks_config)
            except Fault:
                raise _unavailable("scanner_config_unreadable") from None

    def check(self, units: list[tuple[str, str]]) -> list[Finding]:
        if not units:
            return []
        locations = [location for location, _ in units]
        lines = [text for _, text in units]
        content = "\n".join(text for _, text in units) + "\n"
        with tempfile.TemporaryDirectory(prefix="cardea-gate-") as scratch:
            command = [str(self.config.gitleaks_binary), *SCANNER_FLAGS, "-i", scratch]
            if self.config.gitleaks_config is not None:
                command += ["--config", str(self.config.gitleaks_config)]
            try:
                done = subprocess.run(  # noqa: S603 - a pinned binary, fixed arguments
                    command,
                    input=content.encode(),
                    capture_output=True,
                    cwd=scratch,
                    # No GITLEAKS_CONFIG and no inherited secrets from the service.
                    env={"PATH": "/usr/bin:/bin"},
                    timeout=self.config.timeout_seconds,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                raise _unavailable("scanner_failed") from None
        if done.returncode != 0:
            raise _unavailable("scanner_failed")
        try:
            report = json.loads(done.stdout or b"[]")
            findings = [
                Finding(
                    str(item["RuleID"]),
                    locations[int(item["StartLine"]) - 1],
                    lines[int(item["StartLine"]) - 1],
                )
                for item in report
            ]
        except (ValueError, KeyError, IndexError, TypeError):
            raise _unavailable("scanner_report_invalid") from None
        return findings


@dataclass(frozen=True)
class Gate:
    scanner: Scanner
    denylist: Denylist

    @classmethod
    def load(cls, config: ContentGateConfig) -> "Gate":
        return cls(Scanner(config), Denylist.load(config.denylist_file))

    def check(
        self, diff: str | None = None, texts: Mapping[str, str] | None = None
    ) -> list[Finding]:
        """Every finding in the diff's added lines and the text fields, deduplicated."""
        units = [*(added_lines(diff) if diff else []), *text_lines(texts or {})]
        found = [*self.denylist.check(units), *self.scanner.check(units)]
        return list(dict.fromkeys(found))


# The check operations call


def _notify(
    database: Database, context: OperationContext, operation: str, found: list[Finding]
) -> None:
    """Kernel mail to the run's supervisor: the rules and locations, never the text."""
    from cardea.service import mailbox
    from cardea.service.policy import Policy

    with database.engine.begin() as conn, borrow(conn) as tx:
        binding = Policy(conn).binding(context.task_id)
        runtime = str(binding["runtime_id"])
        supervisor = str(binding["supervisor_id"])
        listed: list[JsonValue] = [{"rule": f.rule, "location": f.location} for f in found]
        mailbox.send(
            conn,
            tx,
            namespace=context.namespace,
            sender_id=runtime,
            supervisor_id=supervisor,
            to=mailbox.Address(instance=supervisor),
            parts=[
                {
                    "text": f"The content gate refused {operation} in task {context.task_id}: "
                    + "; ".join(f.describe() for f in found[:MAX_FINDINGS_SHOWN])
                    + ". Nothing was sent."
                },
                {
                    "data": {
                        "task_id": context.task_id,
                        "receipt_id": context.receipt_id,
                        "operation": operation,
                        "findings": listed[:MAX_FINDINGS_SHOWN],
                        "findings_total": len(found),
                    }
                },
            ],
            runtime_id=runtime,
            at=now(),
            sender_task_id=context.task_id,
            from_kernel=True,
        )


def enforce(
    database: Database,
    config: ContentGateConfig | None,
    context: OperationContext,
    *,
    operation: str,
    diff: str | None = None,
    texts: Mapping[str, str] | None = None,
) -> None:
    """Refuse the call (nothing is sent) if anything would leave the host unchecked or
    matches a rule. Call before any network request; the refusal is definite."""
    try:
        if config is None:
            raise _unavailable("not_configured")
        found = Gate.load(config).check(diff, texts)
    except Fault as error:
        raise OperationRejected(error.message, error.code) from None
    if not found:
        return
    log.warning(
        "content gate refused a call",
        extra={"code": "content_refused", "reason": f"{operation}:{len(found)}"},
    )
    if config.notify_supervisor:
        try:
            _notify(database, context, operation, found)
        except (Fault, sa.exc.SQLAlchemyError):
            # The refusal stands without the mail; the receipt still records it.
            log.exception("content gate notice failed", extra={"code": "content_refused"})
    shown = "; ".join(f.describe() for f in found[:MAX_FINDINGS_SHOWN])
    hidden = len(found) - MAX_FINDINGS_SHOWN
    more = f" (and {hidden} more)" if hidden > 0 else ""
    # Everything this call offered to send, and each matched line, so that the kernel can
    # redact them (and any copy) from the receipt.
    offered = (diff,) if diff else ()
    refused = (*offered, *(texts or {}).values(), *(f.line for f in found))
    raise ContentRefused(f"Refused by the content gate: {shown}{more}", refused)
