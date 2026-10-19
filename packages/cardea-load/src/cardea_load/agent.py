"""Separate, immutable comparison contract for the bounded M3 agent profile."""

from __future__ import annotations

import json
import statistics
import subprocess
from pathlib import Path
from typing import Any

from cardea_load.metrics import fingerprint, save

PROFILE = {
    "name": "m3-incidents-v1",
    "tasks": 4,
    "concurrency": 2,
    "arrival": "closed-loop",
    "rounds": 2,
    "provider": "native-openrouter-sdk-deterministic-http-fixture",
    "authentication": "spiffe-jwt-svid",
    "mcp": "native-in-memory-2026-07-28",
    "worker": "debian-systemd-bubblewrap-tools.incidents",
    "model_calls": 20,
    "model_tokens": 1000000,
    "tool_milliseconds": 200000,
    "output_bytes": 2000000,
    "host_concurrency": 4,
}
METRICS = ("admission_p95_ms", "completion_p95_ms", "completion_rps")


def run_agent(repo: Path, python: Path, output: Path, kind: str) -> None:
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    with (output / "private-fixture.log").open("x") as log:
        process = subprocess.run(
            [
                str(python),
                str(Path(__file__).with_name("agent_fixture.py")),
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
    if process.returncode:
        raise RuntimeError(f"Agent campaign failed; inspect {output}")


def agent_baseline(run: dict[str, Any]) -> dict[str, Any]:
    validate(run, "reference", 2)
    if not all(p["passed"] for p in run["points"]):
        raise ValueError("Reference correctness failure")
    return {
        "schema_version": "m3-load-1",
        "contract": run["contract"],
        "reference_sha256": fingerprint(run),
        "metrics": {
            metric: {
                "min": min(p[metric] for p in run["points"]),
                "median": statistics.median(p[metric] for p in run["points"]),
                "max": max(p[metric] for p in run["points"]),
            }
            for metric in METRICS
        },
    }


def validate(run: dict[str, Any], kind: str, repetitions: int) -> None:
    if (
        run.get("schema_version") != "m3-load-1"
        or run.get("kind") != kind
        or not run.get("complete")
        or len(run.get("points", [])) != repetitions
    ):
        raise ValueError("Incomplete or incompatible agent campaign")
    for point in run["points"]:
        if len(point["tasks"]) != run["contract"]["profile"]["tasks"]:
            raise ValueError("Incomplete task evidence")


def agent_compare(reference: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    validate(run, "comparison", 1)
    if reference.get("schema_version") != "m3-load-1" or reference["contract"] != run["contract"]:
        raise ValueError("Incomparable profile, harness, dependencies or host limits")
    point = run["points"][0]
    changes = {}
    warnings = []
    for metric in METRICS:
        median = reference["metrics"][metric]["median"]
        ratio = point[metric] / median if median else None
        changes[metric] = {
            "reference": reference["metrics"][metric],
            "candidate": point[metric],
            "ratio": ratio,
        }
        if ratio is not None and (
            (metric == "completion_rps" and ratio < 0.8)
            or (metric != "completion_rps" and ratio > 1.25)
        ):
            warnings.append(metric)
    return {
        "schema_version": "m3-load-1",
        "baseline_sha256": fingerprint(reference),
        "candidate_sha256": fingerprint(run),
        "changes": changes,
        "warnings": warnings,
        "correctness_passed": point["passed"],
    }


def write_agent_baseline(source: Path, output: Path) -> None:
    save(output, agent_baseline(json.loads(source.read_text())))
