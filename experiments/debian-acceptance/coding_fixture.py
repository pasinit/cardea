"""Shared pieces of the `code.session` acceptance scripts (long_call.py, gate_isolation.py).

`coding_run.sh` installs, for one run only, the scripts' `code.session` configuration and
their repository aliases. Repositories live in the host's one registry,
`/opt/cardea-code/repositories.json` (ADR-037): `merged_registry()` adds the test aliases to
the host's own entries, which stay as they are, and the wrapper restores the original file
when the run ends.
"""

import json
from pathlib import Path
from typing import Any

FIXTURE = "/opt/cardea-code/repos/acceptance.git"
GATES_DIR = "/opt/cardea-code/acceptance-gates"
PYTHON = "/usr/bin/python3"
# A harness that writes the files the task names: `{"write": {path: content}}`.
WRITE_HARNESS = (
    "import json, os, pathlib\n"
    "task = json.loads(os.environ['CARDEA_TASK'])\n"
    "for name, content in task.get('write', {}).items():\n"
    "    path = pathlib.Path(name)\n"
    "    path.parent.mkdir(parents=True, exist_ok=True)\n"
    "    path.write_text(content)\n"
    "print('wrote', len(task.get('write', {})), 'files')\n"
)


def harness_config(code: str, timeout: int = 3500) -> dict[str, Any]:
    """The coding file the wrapper installs: the harness only (repositories are in the
    registry)."""
    return {"harness": {"argv": [PYTHON, "-I", "-c", code], "timeout_seconds": timeout}}


def merged_registry(existing: Path | None, entries: dict[str, Any]) -> dict[str, Any]:
    """The host's registry plus the test aliases; an existing alias is never replaced."""
    document: dict[str, Any] = {"repositories": {}}
    if existing is not None and existing.is_file():
        document = json.loads(existing.read_text())
    clash = sorted(set(entries) & set(document.get("repositories", {})))
    if clash:
        raise SystemExit(f"the host registry already has aliases {clash}; choose other names")
    document.setdefault("repositories", {}).update(entries)
    return document


def entry(gates: list[dict[str, Any]]) -> dict[str, Any]:
    return {"source": FIXTURE, "gates": gates}
