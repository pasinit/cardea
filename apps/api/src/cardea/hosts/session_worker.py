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
import os
import sys
from pathlib import Path
from typing import Any

from cardea.runtime.agent_session import ClaudeAgentSession
from cardea.runtime.harness import Harness, HarnessRequest, HarnessResult
from cardea.runtime.opencode import OpenCodeHarness, Price

MAX_INPUT = 262144


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


def main() -> int:
    document = json.loads(sys.stdin.buffer.read(MAX_INPUT + 1)[:MAX_INPUT])
    kind = os.environ.get("CARDEA_HARNESS_KIND", "claude-agent-sdk")
    if kind not in KINDS:
        print("unknown harness kind", file=sys.stderr)
        return 2
    try:
        harness = KINDS[kind](document)
    except Refused as refused:
        result = HarnessResult.failed(str(refused))
    else:
        request = HarnessRequest.model_validate(
            {**document["request"], "model": document.get("model")}
        )
        result = asyncio.run(harness.run(request))
    print(json.dumps({"output": result.model_dump(mode="json")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
