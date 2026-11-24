"""Inside a confined worker: run one agent harness session (ADR-028, ADR-033).

Installed by the host owner as a worker command, for example
``/opt/cardea/venv/bin/python3 -I -m cardea.hosts.session_worker``, under a profile with
``model_api_only`` networking. systemd loads the profile's single credential into the unit;
bubblewrap exposes it at ``$CARDEA_CREDENTIAL``. The worker's own configuration (never the
caller) chooses what runs:

- ``CARDEA_HARNESS_KIND``: ``claude-agent-sdk`` (default) or ``opencode``;
- ``CARDEA_CREDENTIAL_KIND``: for the Claude Agent SDK, ``api_key`` or ``oauth_token``;
- ``CARDEA_OPENCODE_BINARY``: for OpenCode, the binary under a read-only bind.

The request arrives on stdin as ``{"model", "request", "harness"?}``. ``harness`` carries
the registry entry's non-secret provider settings and the binary's pinned SHA-256, which
this worker checks before running anything. The result leaves on stdout as
``{"output": <HarnessResult>}``.
"""

import asyncio
import hashlib
import json
import logging
import os
import sys
from collections import deque
from pathlib import Path
from typing import Any

from cardea.hosts.operation_worker import staged_inputs
from cardea.runtime import activity as activities
from cardea.runtime.agent_session import ClaudeAgentSession
from cardea.runtime.harness import Harness, HarnessRequest, HarnessResult
from cardea.runtime.opencode import OpenCodeHarness, Price
from cardea.service.operations import OperationRejected

MAX_INPUT = 262144
# A failed session reports this much of the harness's own output (already scrubbed).
TAIL_LINES = 20
TAIL_LINE_BYTES = 512


class Refused(Exception):
    """The worker's configuration and the request disagree; nothing ran."""


def credential() -> str | None:
    path = os.environ.get("CARDEA_CREDENTIAL")
    return Path(path).read_text().strip() if path else None


def claude(document: dict[str, Any]) -> Harness:
    secret = credential()
    if secret is None:
        raise Refused("harness_credential_missing")
    kind = os.environ.get("CARDEA_CREDENTIAL_KIND", "oauth_token")
    if kind == "api_key":
        return ClaudeAgentSession(model=document["model"], api_key=secret)
    if kind == "oauth_token":
        return ClaudeAgentSession(model=document["model"], oauth_token=secret)
    raise Refused("harness_credential_kind")


def opencode(document: dict[str, Any]) -> Harness:
    settings = document.get("harness")
    if not isinstance(settings, dict) or settings.get("kind") != "opencode":
        raise Refused("harness_kind_mismatch")
    binary = Path(os.environ["CARDEA_OPENCODE_BINARY"])
    try:
        digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    except OSError:
        raise Refused("harness_not_installed") from None
    if digest != settings["sha256"]:
        raise Refused("harness_pin_mismatch")
    return OpenCodeHarness(
        binary=binary,
        base_url=str(settings["base_url"]),
        headers={str(k): str(v) for k, v in dict(settings.get("headers") or {}).items()},
        default_model=str(settings["default_model"]),
        prices={str(m): Price(float(p[0]), float(p[1])) for m, p in settings["prices"].items()},
        api_key=credential(),
    )


KINDS = {"claude-agent-sdk": claude, "opencode": opencode}


class Tail(logging.Handler):
    """Keeps the last few log records of the harness adapters, for a failed session.

    Harness output reaches the log through `AttemptActivity.harness_output`, which scrubs
    registered and credential-shaped secrets before logging.
    """

    def __init__(self) -> None:
        super().__init__()
        self.lines: deque[str] = deque(maxlen=TAIL_LINES)

    def emit(self, record: logging.LogRecord) -> None:
        text = record.getMessage()
        # Numeric outcome fields of attempt events (e.g. a process's return code).
        for name in ("returncode", "signal"):
            value = record.__dict__.get(name)
            if isinstance(value, int):
                text += f" {name}={value}"
        self.lines.append(text.encode()[:TAIL_LINE_BYTES].decode(errors="ignore"))


def quiet_logging() -> Tail:
    """Keep the harness adapters' log records off stderr inside the worker.

    The unit duplicates stdout (the JSON result file) onto stderr, so anything written to
    stderr would corrupt the result; Python's last-resort handler would otherwise print
    warnings there. The records go to a bounded tail that a failed result carries.
    """
    tail = Tail()
    root = logging.getLogger("cardea")
    root.handlers[:] = [tail]
    root.setLevel(logging.INFO)
    root.propagate = False
    return tail


def run(document: dict[str, Any], kind: str, attempt: activities.AttemptActivity) -> HarnessResult:
    try:
        harness = KINDS[kind](document)
    except Refused as refused:
        return HarnessResult.failed(str(refused))
    try:
        # The member's memory archive, staged as an input file (never in the document).
        memory = staged_inputs(document.get("inputs")).get("/memory")
    except OperationRejected as refused:
        return HarnessResult.failed(refused.code)
    request = HarnessRequest.model_validate(
        {**document["request"], "model": document.get("model"), "memory": memory}
    )
    attempt.redact(credential(), request.mcp_token)
    try:
        return asyncio.run(harness.run(request))
    except Exception as error:
        # Reported, not raised: an uncaught exception leaves only "exit status 1".
        attempt.event(f"harness error: {type(error).__name__}: {error}", logging.ERROR)
        return HarnessResult.failed("harness_error")


def main() -> int:
    tail = quiet_logging()
    document = json.loads(sys.stdin.buffer.read(MAX_INPUT + 1)[:MAX_INPUT])
    kind = os.environ.get("CARDEA_HARNESS_KIND", "claude-agent-sdk")
    if kind not in KINDS:
        print("unknown harness kind", file=sys.stderr)
        return 2
    with activities.bind(activities.AttemptActivity("worker", 0)) as attempt:
        result = run(document, kind, attempt)
        output: dict[str, Any] = result.model_dump(mode="json")
        if not result.success:
            # Scrubbed again with every secret this worker knows (credential, tool token).
            output["diagnostics"] = [attempt.scrub(line) for line in tail.lines]
    print(json.dumps({"output": output}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
