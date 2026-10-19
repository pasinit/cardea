"""Repeatable campaign orchestration; each measurement owns fresh kernel state."""

from __future__ import annotations

import collections
import hashlib
import json
import platform
import sqlite3
import statistics
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cardea_load.engine import attack
from cardea_load.lab import Lab, task
from cardea_load.metrics import fingerprint, oracle, save


def hashes(repo: Path, paths: list[Path]) -> dict[str, str]:
    return {
        str(p.relative_to(repo)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)
    }


def manifest(repo: Path, vegeta: Path, profile: dict[str, Any]) -> dict[str, Any]:
    package = Path(__file__).resolve().parent
    lock = package.parents[1] / "uv.lock"
    if not lock.exists():
        lock = package / "data/uv.lock"
    environment: dict[str, Any] = {
        "os": platform.system(),
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "kernel": platform.release(),
        "vegeta_sha256": hashlib.sha256(vegeta.read_bytes()).hexdigest(),
        "api_lock": hashlib.sha256((repo / "apps/api/uv.lock").read_bytes()).hexdigest(),
        "package_lock": hashlib.sha256(lock.read_bytes()).hexdigest(),
    }
    for name in ("cpu.max", "memory.max", "pids.max"):
        path = Path("/sys/fs/cgroup") / name
        environment[name] = path.read_text().strip() if path.exists() else "unlimited-native"
    environment["os_release"] = (
        Path("/etc/os-release").read_text()
        if Path("/etc/os-release").exists()
        else platform.platform()
    )
    return {
        "contract": {
            "profile_sha256": fingerprint(profile),
            "environment": environment,
            "harness_sha256": fingerprint(hashes(package, list(package.rglob("*.py")))),
            "supervisor_svid_ttl": 120,
            "credential_refresh_seconds": 5,
        },
        "source": hashes(
            repo,
            list((repo / "apps/api/src/cardea/kernel").rglob("*.py"))
            + list((repo / "apps/api/src/cardea/service").rglob("*.py"))
            + list((repo / "experiments/m1").rglob("*.py"))
            + [repo / "experiments/spire/binding.py"],
        ),
        "vegeta_version": subprocess.check_output([str(vegeta), "-version"], text=True).strip(),
    }


def warm(lab: Lab, profile: dict[str, Any]) -> None:
    lab.reset()
    for i in range(profile["warmup_tasks"]):
        response = lab.request("POST", "/tasks", task(f"warmup-{i}", profile["payload_bytes"]))
        response.raise_for_status()
    if not lab.drain(60):
        raise RuntimeError("Warmup did not drain")
    evidence = lab.call("audit")
    if evidence["violations"] or any(t["state"] != "succeeded" for t in evidence["tasks"]):
        raise RuntimeError("Warmup durable audit failed: " + json.dumps(evidence))


def point(
    lab: Lab,
    vegeta: Path,
    output: Path,
    name: str,
    rate: int,
    seconds: int,
    profile: dict[str, Any],
    **options: Any,
) -> dict[str, Any]:
    warm(lab, profile)
    start = time.monotonic()
    result = attack(lab, vegeta, output / name, name, rate, seconds, profile, **options)
    drained = lab.drain(profile["drain_seconds"])
    elapsed = time.monotonic() - start
    audit = lab.call("audit")
    rows = [r for r in audit["tasks"] if r["request_id"].startswith(name + "-")]
    violations = oracle(audit, set(result.pop("acknowledged")))
    states = dict(collections.Counter(r["state"] for r in rows))
    failures = dict(collections.Counter(r["failure"] for r in rows if r["failure"]))
    if not drained:
        violations.append("drain_timeout")
    if (
        result["malformed_successes"]
        or result["feeder_errors"]
        or "error" in result["intervention"]
    ):
        violations.append("measurement_error")
    if result["unexpected_error"]:
        violations.append("unexpected_http_errors")
    if (
        not options.get("fault")
        and not options.get("denial")
        and any(s != "succeeded" for s in states)
    ):
        violations.append("tasks_not_successful")
    completion_ms = sorted(
        (
            datetime.fromisoformat(r["updated_at"]) - datetime.fromisoformat(r["created_at"])
        ).total_seconds()
        * 1000
        for r in rows
        if r["state"] == "succeeded"
    )
    if options.get("denial"):
        allowed = (
            {"authority_denied"} if name == "live-revocation" else {"runtime_identity_unavailable"}
        )
        if set(failures) - allowed:
            violations.append("unexpected_task_failure")
    elif options.get("fault") and failures:
        violations.append("unexpected_task_failure")
    if seconds > 120 and result["distinct_credentials_used"] < 2:
        violations.append("credential_renewal_not_observed")
    result.update(
        completion_p50_ms=statistics.median(completion_ms) if completion_ms else None,
        completion_p95_ms=(
            statistics.quantiles(completion_ms, n=100, method="inclusive")[94]
            if len(completion_ms) > 1
            else (completion_ms[0] if completion_ms else None)
        ),
        name=name,
        total_seconds=elapsed,
        drain_seconds=elapsed - result["http_seconds"],
        completion_rps=states.get("succeeded", 0) / elapsed,
        states=states,
        failures=failures,
        violations=violations,
        passed=not violations,
    )
    save(output / name / "audit.json", audit)
    save(output / name / "summary.json", result)
    print(
        json.dumps(
            {
                "point": name,
                "accepted": result["accepted"],
                "states": states,
                "p95_ms": round(result["p95_ms"], 2),
                "passed": result["passed"],
            }
        ),
        flush=True,
    )
    return result


def races(lab: Lab, output: Path, profile: dict[str, Any]) -> list[dict[str, Any]]:
    warm(lab, profile)
    request = task("retry-shared", profile["payload_bytes"])
    with ThreadPoolExecutor(max_workers=32) as pool:
        responses = list(pool.map(lambda _: lab.request("POST", "/tasks", request), range(32)))
    ids = {r.json()["task"]["task_id"] for r in responses if r.is_success}
    conflict = lab.request("POST", "/tasks", {**request, "payload": {"changed": True}})
    lab.drain(60)
    audit = lab.call("audit")
    violations = oracle(audit, ids)
    if len(ids) != 1 or not all(r.is_success for r in responses) or conflict.status_code != 409:
        violations.append("retry_or_conflict_semantics")
    result = [
        {
            "name": "retry-storm",
            "passed": not violations,
            "violations": violations,
            "requests": 32,
            "unique_task_ids": len(ids),
            "conflict_status": conflict.status_code,
        }
    ]
    save(output / "retry-audit.json", audit)
    warm(lab, profile)
    ids = set()
    for i in range(100):
        response = lab.request("POST", "/tasks", task(f"waiting-{i}", wait=True))
        if response.is_success:
            ids.add(response.json()["task"]["task_id"])
    time.sleep(2)
    counts = lab.call("counts")
    overflow = lab.request("POST", "/tasks", task("overflow", wait=True))
    began = time.monotonic()
    inspection = lab.request("GET", "/tasks/" + sorted(ids)[0])
    inspection_ms = (time.monotonic() - began) * 1000
    cancel_codes = []
    for i, identity in enumerate(sorted(ids)):
        cancel_codes.append(
            lab.request(
                "POST",
                f"/tasks/{identity}/cancel",
                {"schema_version": "0.2", "request_id": f"cancel-{i}"},
            ).status_code
        )
    lab.drain(60)
    audit = lab.call("audit")
    violations = oracle(audit, ids)
    if len(ids) != 100 or overflow.status_code != 429 or not inspection.is_success:
        violations.append("capacity_or_inspection")
    if counts.get("waiting") != 4:
        violations.append("four_waiting_slots")
    if not all(200 <= c < 300 for c in cancel_codes):
        violations.append("cancel_failed")
    if any(
        r["state"] != "cancelled" for r in audit["tasks"] if r["request_id"].startswith("waiting-")
    ):
        violations.append("not_all_cancelled")
    result.append(
        {
            "name": "waiting-capacity",
            "passed": not violations,
            "violations": violations,
            "admitted": len(ids),
            "counts_at_capacity": counts,
            "overflow_status": overflow.status_code,
            "inspection_ms": inspection_ms,
        }
    )
    save(output / "waiting-audit.json", audit)
    return result


def run(
    repo: Path, api_python: Path, vegeta: Path, profile: dict[str, Any], output: Path, kind: str
) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=False)
    result: dict[str, Any] = {
        "schema_version": "1",
        "kind": kind,
        "profile": profile,
        "started_at": datetime.now(UTC).isoformat(),
        "complete": False,
        **manifest(repo, vegeta, profile),
        "points": [],
    }
    save(output / "manifest.json", result)
    lab = Lab(repo, api_python)
    try:
        if kind in ("reference", "comparison"):
            for repetition in range(profile[kind + "_repetitions"]):
                for rate in profile["rates"]:
                    name = f"r{repetition + 1}-{rate}rps"
                    result["points"].append(
                        point(lab, vegeta, output, name, rate, profile["seconds"], profile)
                    )
        elif kind == "stress":
            result["scenarios"] = races(lab, output, profile)

            def crash(target: Lab) -> dict[str, Any]:
                time.sleep(5)
                before = target.call("audit")
                target.call("crash")
                time.sleep(2)
                restarted = target.call("restart")
                target.info.update(restarted)
                return {
                    "restarted": restarted,
                    "receipts_before": {
                        t["task_id"]: t["receipt_sha256"]
                        for t in before["tasks"]
                        if "receipt_sha256" in t
                    },
                }

            result["points"].append(
                point(
                    lab,
                    vegeta,
                    output,
                    "crash-restart",
                    10,
                    25,
                    profile,
                    fault=True,
                    intervention=crash,
                )
            )
            crash_point = result["points"][-1]
            after = json.loads((output / "crash-restart/audit.json").read_text())
            receipts = {t["task_id"]: t.get("receipt_sha256") for t in after["tasks"]}
            stable = all(
                receipts.get(k) == v
                for k, v in crash_point["intervention"].get("receipts_before", {}).items()
            )
            result["scenarios"].append({"name": "receipt-stability", "passed": stable})

            def revoke(target: Lab) -> dict[str, Any]:
                time.sleep(5)
                return target.call("revoke")

            result["points"].append(
                point(
                    lab,
                    vegeta,
                    output,
                    "live-revocation",
                    10,
                    20,
                    profile,
                    denial=True,
                    intervention=revoke,
                )
            )
            post = lab.call("after_revoke")
            result["scenarios"].append(
                {
                    "name": "revocation-ceiling",
                    "passed": post["writes"] == 0,
                    "writes_after_policy_cursor": post["writes"],
                }
            )

            def provider(target: Lab) -> dict[str, Any]:
                time.sleep(5)
                target.call("provider_stop")
                time.sleep(5)
                return target.call("provider_restart")

            result["points"].append(
                point(
                    lab,
                    vegeta,
                    output,
                    "provider-outage",
                    5,
                    25,
                    profile,
                    denial=True,
                    fault=True,
                    intervention=provider,
                )
            )
            result["points"].append(
                point(
                    lab,
                    vegeta,
                    output,
                    "soak",
                    profile["soak_rate"],
                    profile["soak_seconds"],
                    profile,
                )
            )
        else:
            raise ValueError("Unknown campaign kind")
        result["complete"] = True
    finally:
        lab.close()
        result["finished_at"] = datetime.now(UTC).isoformat()
        save(output / "run.json", result)
    return result
