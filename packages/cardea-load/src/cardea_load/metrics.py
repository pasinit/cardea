"""Auditable classifications and immutable, environment-matched references."""

from __future__ import annotations

import hashlib
import json
import statistics
from pathlib import Path
from typing import Any

import rfc8785


def fingerprint(value: Any) -> str:
    return hashlib.sha256(rfc8785.dumps(value)).hexdigest()


def save(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")


def classify(code: int, deliberate_denial: bool = False, fault: bool = False) -> str:
    if 200 <= code < 300:
        return "accepted"
    if code == 429:
        return "capacity_rejected"
    if deliberate_denial and code in (401, 403):
        return "expected_denial"
    if fault and code in (0, 503):
        return "fault_unavailable"
    return "unexpected_error"


def oracle(audit: dict[str, Any], acknowledged: set[str]) -> list[str]:
    violations = list(audit["violations"])
    rows = audit["tasks"]
    ids = {row["task_id"] for row in rows}
    violations.extend("missing_acknowledged:" + i for i in sorted(acknowledged - ids))
    keys = [(r["supervisor"], r["request_id"]) for r in rows]
    if len(keys) != len(set(keys)):
        violations.append("duplicate_submission")
    for row in rows:
        if row["outputs"] > 1:
            violations.append(row["task_id"] + ":duplicate_artifact")
        if row["state"] == "succeeded" and (row["outputs"] != 1 or not row["verification"]):
            violations.append(row["task_id"] + ":false_success")
    return sorted(set(violations))


def baseline(run: dict[str, Any]) -> dict[str, Any]:
    if run["kind"] != "reference" or not run["complete"]:
        raise ValueError("Only complete reference campaigns can create a baseline")
    if run.get("schema_version") != "1":
        raise ValueError("Unsupported result schema")
    expected = run["profile"]["reference_repetitions"]
    rates = run["profile"]["rates"]
    if not rates or len(rates) != len(set(rates)) or expected < 1:
        raise ValueError("Reference requires unique rates and positive repetitions")
    if len(run["points"]) != expected * len(rates):
        raise ValueError("Incomplete or extra reference points")
    points = {}
    for rate in run["profile"]["rates"]:
        samples = [p for p in run["points"] if p["rate"] == rate]
        if len(samples) != expected:
            raise ValueError("Incomplete reference repetitions")
        points[str(rate)] = {
            metric: {
                "median": statistics.median(p[metric] for p in samples),
                "min": min(p[metric] for p in samples),
                "max": max(p[metric] for p in samples),
            }
            for metric in ("p95_ms", "p99_ms", "completion_rps", "accepted", "capacity_rejected")
        }
    return {
        "schema_version": "1",
        "reference_run_sha256": fingerprint(run),
        "contract": run["contract"],
        "source": run["source"],
        "points": points,
        "correctness_passed": all(p["passed"] for p in run["points"]),
    }


def compare(reference: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    if reference.get("schema_version") != "1" or run.get("schema_version") != "1":
        raise ValueError("Unsupported result schema")
    if reference["contract"] != run["contract"]:
        raise ValueError("Incomparable workload, harness, generator or environment")
    if not run["complete"] or run["kind"] != "comparison":
        raise ValueError("Comparison campaign is incomplete or has the wrong kind")
    if not reference["correctness_passed"]:
        raise ValueError(
            "Reference has correctness failures; retain evidence but do not approve it"
        )
    expected = run["profile"]["comparison_repetitions"]
    rates = run["profile"]["rates"]
    if not rates or expected < 1 or len(run["points"]) != expected * len(rates):
        raise ValueError("Incomplete comparison points")
    if set(reference["points"]) != {str(rate) for rate in rates} or any(
        sum(p["rate"] == rate for p in run["points"]) != expected for rate in rates
    ):
        raise ValueError("Comparison points do not match reference rates")
    warnings = []
    changes = []
    thresholds = run["profile"]["performance_warnings"]
    for point in run["points"]:
        control = reference["points"][str(point["rate"])]
        row: dict[str, Any] = {"rate": point["rate"], "passed": point["passed"]}
        for metric in ("p95_ms", "completion_rps"):
            median = control[metric]["median"]
            ratio = point[metric] / median if median else None
            row[metric] = {"candidate": point[metric], "reference": control[metric], "ratio": ratio}
            if ratio is not None and (
                (metric == "p95_ms" and ratio > 1 + thresholds["p95_increase_fraction"])
                or (
                    metric == "completion_rps"
                    and ratio < 1 - thresholds["throughput_decrease_fraction"]
                )
            ):
                warnings.append(f"{point['rate']} rps: {metric}")
        changes.append(row)
    return {
        "schema_version": "1",
        "baseline_sha256": fingerprint(reference),
        "candidate_sha256": fingerprint(run),
        "changes": changes,
        "warnings": warnings,
        "correctness_passed": all(p["passed"] for p in run["points"]),
    }
