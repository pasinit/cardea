"""Supplement the frozen measurement oracle using only retained public evidence.

Usage: python tools/audit_results.py CAMPAIGN_DIR
Writes an exclusive final-audit.json; fails on missing, duplicate, or wrong-scope acknowledgments.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


def audit(root: Path) -> dict[str, Any]:
    checked = []
    violations = []
    for kind in ("reference", "comparison", "stress"):
        run = json.loads((root / kind / "run.json").read_text())
        if not run["complete"]:
            violations.append(kind + ":incomplete")
        for point in run["points"]:
            path = root / kind / point["name"]
            snapshot = json.loads((path / "audit.json").read_text())
            with gzip.open(path / "requests.jsonl.gz", "rt") as stream:
                responses = [json.loads(line) for line in stream]
            acknowledged = [r["task_id"] for r in responses if r["category"] == "accepted"]
            bindings = {r["task_id"]: r for r in snapshot["tasks"]}
            prefix = point["name"] + "-"
            label = kind + "/" + point["name"]
            if len(acknowledged) != len(set(acknowledged)):
                violations.append(label + ":duplicate_acknowledgment_for_unique_requests")
            if len(acknowledged) != point["accepted"] or len(responses) != point["requests"]:
                violations.append(label + ":response_count_mismatch")
            for identity in acknowledged:
                if identity not in bindings or not bindings[identity]["request_id"].startswith(
                    prefix
                ):
                    violations.append(label + ":missing_or_wrong_scope_acknowledgment")
            checked.append(
                {
                    "point": label,
                    "acknowledged": len(acknowledged),
                    "persisted_in_scope": sum(
                        r["request_id"].startswith(prefix) for r in bindings.values()
                    ),
                }
            )
    stress = json.loads((root / "stress/run.json").read_text())
    points = {p["name"]: p for p in stress["points"]}
    if not points["crash-restart"]["intervention"].get("receipts_before"):
        violations.append("crash_missing_prior_receipt_evidence")
    if not points["crash-restart"]["fault_unavailable"]:
        violations.append("crash_outage_not_observed")
    for name in ("live-revocation", "provider-outage"):
        if not points[name]["expected_denial"]:
            violations.append(name + ":denial_not_observed")
    return {
        "schema_version": "1",
        "passed": not violations,
        "violations": violations,
        "points": checked,
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }


if __name__ == "__main__":
    directory = Path(sys.argv[1])
    result = audit(directory)
    with (directory / "final-audit.json").open("x") as output:
        json.dump(result, output, indent=2)
        output.write("\n")
    print(
        json.dumps(
            {
                "passed": result["passed"],
                "points": len(result["points"]),
                "violations": result["violations"],
            }
        )
    )
    raise SystemExit(0 if result["passed"] else 2)
