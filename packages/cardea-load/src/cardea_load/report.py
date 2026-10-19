"""Render measured results without turning capacity warnings into success claims."""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any


def render(root: Path) -> str:
    reference = json.loads((root / "reference/run.json").read_text())
    candidate = json.loads((root / "comparison/run.json").read_text())
    stress = json.loads((root / "stress/run.json").read_text())
    all_points = reference["points"] + candidate["points"] + stress["points"]
    checks = all_points + stress.get("scenarios", [])
    failures = [p["name"] for p in checks if not p["passed"]]
    totals = {
        key: sum(p[key] for p in all_points)
        for key in ("requests", "accepted", "capacity_rejected", "unexpected_error")
    }
    env = reference["contract"]["environment"]
    lines = [
        "# M1 load-test result report",
        "",
        f"Reference started: {reference['started_at']}.",
        f"Campaign ended: {stress['finished_at']}.",
        "",
        "**Verdict: "
        + (
            "all executed correctness checks passed."
            if not failures
            else "failures observed: " + ", ".join(failures) + "."
        )
        + "**",
        "",
        f"Vegeta issued {totals['requests']:,} measured requests: {totals['accepted']:,} "
        f"accepted, {totals['capacity_rejected']:,} capacity rejections, "
        f"{totals['unexpected_error']:,} unexpected HTTP/transport errors. "
        "Fault-window denials/unavailability are counted separately in the raw summaries. "
        "Warmups and coordinated retry/capacity scenarios are excluded from these totals.",
        "",
        "## Reference and independent comparison",
        "",
        "Unchanged M1; three reference repetitions and one subsequent comparison. "
        "Each rate has a fresh store, four warmup tasks, 20 seconds of traffic, "
        "and up to 90 seconds to drain. Payload contains a fixed 1,024-character data string. "
        "Latency below covers all HTTP responses, including rejections; accepted-only p95 "
        "and task completion latency are also retained in every point summary.",
        "",
        "| Offered req/s | Reference HTTP p95 ms, median [range] | Comparison p95 ms | "
        "Reference completed/s, median [range] | Comparison completed/s | "
        "Comparison accepted / rejected |",
        "|---:|---:|---:|---:|---:|---:|",
    ]
    for point in candidate["points"]:
        samples = [p for p in reference["points"] if p["rate"] == point["rate"]]

        def cell(metric: str, samples: list[dict[str, Any]] = samples) -> str:
            values = [p[metric] for p in samples]
            return f"{statistics.median(values):.2f} [{min(values):.2f}–{max(values):.2f}]"

        lines.append(
            f"| {point['rate']} | {cell('p95_ms')} | {point['p95_ms']:.2f} | "
            f"{cell('completion_rps')} | {point['completion_rps']:.2f} | "
            f"{point['accepted']} / {point['capacity_rejected']} |"
        )
    comparison_file = root / "comparison.json"
    if comparison_file.exists():
        comparison = json.loads(comparison_file.read_text())
        lines += [
            "",
            "Provisional performance warnings (>25% HTTP p95 increase or >20% "
            "completion-rate decrease): " + (", ".join(comparison["warnings"]) or "none") + ".",
        ]
    else:
        lines += [
            "",
            "No approved comparison artifact: inspect reference correctness and comparability.",
        ]
    lines += [
        "",
        "Completion rate divides successful tasks by submission-through-drain wall time "
        "(including result decoding). It is useful campaign throughput, not peak scheduler "
        "capacity. Vegeta's rate excludes the last interarrival interval; "
        "`requests_per_configured_second` supplies an additional pacing check. "
        "These short repetitions and provisional thresholds are an initial reference, not an SLO.",
        "",
        "## Adversarial scenarios",
        "",
        "| Scenario | Result | Evidence |",
        "|---|---|---|",
    ]
    for check in stress.get("scenarios", []) + stress["points"]:
        detail: Any = check.get("violations") or {
            k: v
            for k, v in check.items()
            if k
            in (
                "unique_task_ids",
                "conflict_status",
                "admitted",
                "overflow_status",
                "inspection_ms",
                "writes_after_policy_cursor",
                "states",
                "failures",
                "distinct_credentials_used",
                "expected_denial",
                "fault_unavailable",
            )
        }
        lines.append(
            f"| {check['name']} | {'PASS' if check['passed'] else 'FAIL'} | "
            f"{json.dumps(detail, sort_keys=True)} |"
        )
    lines += [
        "",
        "## Environment and limits",
        "",
        f"{env['os']} {env['architecture']}; Python {env['python']}; SQLite {env['sqlite']}. "
        f"cgroup CPU `{env['cpu.max']}`, memory `{env['memory.max']}`, PIDs `{env['pids.max']}`.",
        "",
        "Debian amd64 runs under emulation on this Apple Silicon host. Service, SPIRE, "
        "generator and observer share the constrained container. These results support "
        "repeatable local comparisons; they do not establish native Debian capacity. "
        "Real SPIFFE authentication is exercised; OIDC/Keycloak load is not.",
        "",
        "The oracle checks acknowledged task persistence, submission deduplication, one "
        "canonical output version, successful publish receipts, independent verification "
        "artifacts, SQLite integrity/foreign keys and post-revocation object-write events. "
        "It checks stored supervisor bindings but does not add descendant/delegation "
        "load scenarios. Receipt stability is compared across the crash. No production "
        "application changes were made to improve these numbers.",
        "",
        "Unrun: overnight soak, disk-full/slow-I/O faults, native Linux capacity, "
        "OIDC load, remote clients and arbitrary agent/model workloads. The five-minute "
        "soak cannot establish absence of slow leaks. A CPU limit is a quota, not exclusive cores.",
        "",
        "## Evidence",
        "",
        "- `reference/run.json`, `comparison/run.json`, `stress/run.json`: "
        "full campaign manifests.",
        "- `baseline.json`: exclusive measured reference; "
        "`comparison.json`: matching-point comparison.",
        "- Each load point: `vegeta.json`, compressed original `requests.bin.gz`, "
        "normalized `requests.jsonl.gz`, `resources.json`, `audit.json`, `summary.json`.",
        "- Profiles, dependency locks, generator digest, source fingerprints and environment "
        "are recorded. Request credentials and private provider state are not retained.",
        "",
    ]
    return "\n".join(lines)
