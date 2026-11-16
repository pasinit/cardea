"""Inside a confined worker: run one installed operation handler (ADR-028).

The host owner installs it as a worker command, for example
``/opt/cardea/venv/bin/python3 -I -m cardea.hosts.operation_worker telos.browser``. The
entry point is resolved from the read-only environment in the sandbox, exactly as the
in-process registry resolves it. Arguments and context arrive on stdin as JSON; one JSON
document leaves on stdout: ``{"output": {...}}`` or ``{"rejected": "...", "code": "..."}``.
Any other exit is a failed worker, which the kernel treats as an uncertain effect.

A worker serves one operation: its first entry point is the handler; any others are its
reconcilers (ADR-037), for example ``... operation_worker vcs.push_branch
vcs.find_branch``. A reconcile job (``"reconcile": true``) names its ``entry``, which must
be listed, so a worker never runs the handler when asked to reconcile.

Staged inputs (ADR-037) are read-only files under ``$CARDEA_INPUTS``; the job's manifest
names each one's file, size and SHA-256, and a mismatch refuses the job.
"""

import asyncio
import base64
import hashlib
import json
import os
import sys
from collections.abc import Mapping
from datetime import datetime
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

from cardea.service.content_gate import remote_check
from cardea.service.operations import (
    ENTRY_POINT_GROUP,
    OperationContext,
    OperationRejected,
    Reconciliation,
    WithArtifacts,
    http_status,
)

MAX_INPUT = 131072


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: operation_worker <entry-point> [<entry-point> ...]", file=sys.stderr)
        return 2
    listed = argv[1:]
    document = json.loads(sys.stdin.buffer.read(MAX_INPUT + 1)[:MAX_INPUT])
    name = choose(listed, document)
    if name is None:
        print(json.dumps({"rejected": "this worker does not serve that entry point"}))
        return 0
    points = [p for p in entry_points(group=ENTRY_POINT_GROUP) if p.name == name]
    if len(points) != 1:
        print("entry point is not installed", file=sys.stderr)
        return 2
    print(json.dumps(run(points[0].load(), document)))
    return 0


def choose(listed: list[str], document: dict[str, Any]) -> str | None:
    """The entry point this job runs, from the worker's fixed list."""
    wanted = document.get("entry")
    if wanted in listed:
        return str(wanted)
    if document.get("reconcile") is True:
        return None  # Never run something else when asked to reconcile.
    return listed[0]


def staged_inputs(manifest: object) -> dict[str, bytes]:
    """Read the job's staged inputs and check each against its manifest entry."""
    if not manifest:
        return {}
    folder = os.environ.get("CARDEA_INPUTS")
    if not isinstance(manifest, Mapping) or not folder:
        raise OperationRejected("The job's inputs were not staged", "input_unavailable")
    inputs: dict[str, bytes] = {}
    for pointer, entry in manifest.items():
        if not isinstance(entry, Mapping) or not str(entry.get("file", "")).isdigit():
            raise OperationRejected("The job's inputs were not staged", "input_unavailable")
        path = Path(folder) / str(entry["file"])
        try:
            data = path.read_bytes()
        except OSError:
            raise OperationRejected("A staged input is missing", "input_unavailable") from None
        if len(data) != entry.get("bytes") or hashlib.sha256(data).hexdigest() != entry.get(
            "sha256"
        ):
            raise OperationRejected(
                f"Staged input {pointer} does not match its digest", "input_digest_mismatch"
            )
        inputs[str(pointer)] = data
    return inputs


def run(handler: Any, document: dict[str, Any]) -> dict[str, Any]:
    """Run the handler on one job document; returns the document the worker prints."""
    recorded = document["context"]
    try:
        inputs = staged_inputs(recorded.get("inputs"))
    except OperationRejected as rejected:
        return {"rejected": str(rejected)[:256], "code": rejected.code}
    context = OperationContext(
        task_id=recorded["task_id"],
        namespace=recorded["namespace"],
        receipt_id=recorded["receipt_id"],
        deadline=datetime.fromisoformat(recorded["deadline"]),
        effect_key=recorded["effect_key"],
        agent_id=recorded.get("agent_id"),
        idempotency_context=recorded.get("idempotency_context"),
        slot_time=recorded.get("slot_time"),
        group=recorded.get("group"),
        inputs=inputs,
        # The kernel's content gate over its loopback callback (ADR-037); the gate's files
        # and binary never enter the sandbox.
        check_content=remote_check(recorded.get("content_gate")),
    )
    try:
        output = asyncio.run(handler(document["arguments"], context))
    except OperationRejected as rejected:
        return {"rejected": str(rejected)[:256], "code": rejected.code}
    except Exception as error:
        # Describe the failure without its message, which may carry URLs or secrets.
        return {"error": {"type": type(error).__name__, "status": http_status(error)}}
    if isinstance(output, Reconciliation):
        # A reconciler's observation (ADR-026): read-only, so it carries no artifacts.
        return {"output": {"applied": output.applied, "output": output.output}}
    if isinstance(output, WithArtifacts):
        # Large results travel beside the output; the kernel stores them (ADR-037).
        encoded = {name: base64.b64encode(data).decode() for name, data in output.artifacts.items()}
        return {"output": output.output, "artifacts": encoded}
    return {"output": output}


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
