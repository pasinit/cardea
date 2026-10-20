"""Inside a confined worker: run one installed operation handler (ADR-028).

The host owner installs it as a worker command, for example
``/opt/cardea/venv/bin/python3 -I -m cardea.hosts.operation_worker telos.browser``. The
entry point is resolved from the read-only environment in the sandbox, exactly as the
in-process registry resolves it. Arguments and context arrive on stdin as JSON; one JSON
document leaves on stdout: ``{"output": {...}}`` or ``{"rejected": "..."}``. Any other exit
is a failed worker, which the kernel treats as an uncertain effect.
"""

import asyncio
import json
import sys
from datetime import datetime
from importlib.metadata import entry_points

from cardea.service.operations import ENTRY_POINT_GROUP, OperationContext, OperationRejected

MAX_INPUT = 131072


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: operation_worker <entry-point>", file=sys.stderr)
        return 2
    points = [p for p in entry_points(group=ENTRY_POINT_GROUP) if p.name == argv[1]]
    if len(points) != 1:
        print("entry point is not installed", file=sys.stderr)
        return 2
    handler = points[0].load()
    document = json.loads(sys.stdin.buffer.read(MAX_INPUT + 1)[:MAX_INPUT])
    recorded = document["context"]
    context = OperationContext(
        task_id=recorded["task_id"],
        namespace=recorded["namespace"],
        receipt_id=recorded["receipt_id"],
        deadline=datetime.fromisoformat(recorded["deadline"]),
        effect_key=recorded["effect_key"],
        agent_id=recorded.get("agent_id"),
        idempotency_context=recorded.get("idempotency_context"),
        slot_time=recorded.get("slot_time"),
    )
    try:
        output = asyncio.run(handler(document["arguments"], context))
    except OperationRejected as rejected:
        print(json.dumps({"rejected": str(rejected)[:256]}))
        return 0
    print(json.dumps({"output": output}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
