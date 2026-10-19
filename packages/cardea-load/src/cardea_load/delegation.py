"""M4's separate comparison contract reuses the M3 metric and validation machinery."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from cardea_load.agent import agent_baseline, agent_compare
from cardea_load.metrics import fingerprint

PROFILE = {
    "name": "m4-delegation-v1",
    "tasks": 4,
    "concurrency": 2,
    "arrival": "closed-loop",
    "children": 2,
    "child_concurrency": 2,
    "protocol": "a2a-1.0-jsonrpc-private-unix-http",
    "authentication": "spiffe-jwt-svid",
    "worker": "debian-systemd-bubblewrap-hash-copy",
    "model_calls": 0,
    "parent_tool_milliseconds": 20000,
    "parent_output_bytes": 163840,
}


def run_delegation(repo: Path, python: Path, output: Path, kind: str) -> None:
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    with (output / "private-fixture.log").open("x") as log:
        result = subprocess.run(
            [
                str(python),
                str(Path(__file__).with_name("delegation_fixture.py")),
                str(repo),
                str(output),
                kind,
                json.dumps(PROFILE),
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=1500,
            check=False,
        )
    if result.returncode:
        raise RuntimeError(f"Delegation campaign failed; inspect {output}")


def compatible(value: dict[str, Any]) -> dict[str, Any]:
    if (
        value.get("schema_version") != "m4-load-1"
        or value.get("contract", {}).get("profile") != PROFILE
    ):
        raise ValueError("Not a matched M4 delegation profile")
    return {**value, "schema_version": "m3-load-1"}


def delegation_baseline(run: dict[str, Any]) -> dict[str, Any]:
    result = agent_baseline(compatible(run))
    return {**result, "schema_version": "m4-load-1", "reference_sha256": fingerprint(run)}


def delegation_compare(baseline: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    result = agent_compare(compatible(baseline), compatible(run))
    return {
        **result,
        "schema_version": "m4-load-1",
        "baseline_sha256": fingerprint(baseline),
        "candidate_sha256": fingerprint(run),
    }
