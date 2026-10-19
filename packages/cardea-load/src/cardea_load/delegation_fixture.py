"""Trusted M4 load fixture, launched using the target API's interpreter."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import sqlite3
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any


def main() -> None:
    repo, output = Path(sys.argv[1]), Path(sys.argv[2])
    kind, profile = sys.argv[3], json.loads(sys.argv[4])
    sys.path[:0] = [
        str(repo / p)
        for p in ("apps/api/src", "experiments/m4", "experiments/m1", "experiments/spire")
    ]
    from cardea.service.client import KernelClient  # type: ignore[import-not-found]
    from cardea.service.models import (  # type: ignore[import-not-found]
        RIGHTS,
        DelegationTaskRequest,
    )
    from harness import wait_for  # type: ignore[import-not-found]
    from provider_lab import Lab, eventually  # type: ignore[import-not-found]
    from test_integration import SUPERVISOR, service  # type: ignore[import-not-found]

    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    harness = {
        str(p.relative_to(repo)): digest(p)
        for directory in (
            "experiments/m1",
            "experiments/m4",
            "experiments/spire",
            "packages/cardea-load/src",
        )
        for p in sorted((repo / directory).rglob("*.py"))
    }
    result: dict[str, Any] = {
        "schema_version": "m4-load-1",
        "kind": kind,
        "complete": False,
        "contract": {
            "profile": profile,
            "harness": harness,
            "api_lock": digest(repo / "apps/api/uv.lock"),
            "load_lock": digest(repo / "packages/cardea-load/uv.lock"),
            "environment": {
                "platform": platform.platform(),
                "python": platform.python_version(),
                "cpus": os.cpu_count(),
                "memory": Path("/proc/meminfo").read_text().splitlines()[0],
                "limits": {
                    name: Path("/sys/fs/cgroup", name).read_text().strip()
                    if Path("/sys/fs/cgroup", name).exists()
                    else "root-unlimited"
                    for name in ("cpu.max", "memory.max", "pids.max")
                },
                "systemd": subprocess.check_output(
                    ["systemctl", "--version"], text=True
                ).splitlines()[0],
                "bwrap": subprocess.check_output(["bwrap", "--version"], text=True).strip(),
            },
        },
        "source": {
            str(p.relative_to(repo)): digest(p)
            for p in sorted((repo / "apps/api/src").rglob("*"))
            if p.is_file() and p.suffix in {".py", ".json"}
        },
        "points": [],
    }
    provider = Lab()
    try:
        provider.start()
        provider.register(SUPERVISOR)
        eventually(lambda: provider.token(SUPERVISOR))
        for repetition in range(2 if kind == "reference" else 1):
            factory = service.__wrapped__(provider)
            lab = next(factory)
            try:
                timing_path = lab.root / "protocol.jsonl"
                env = {
                    k: v
                    for k, v in os.environ.items()
                    if not k.startswith(("DBOS_", "OTEL_", "M1_", "M4_"))
                }
                env.update(
                    PYTHONPATH=str(repo / "apps/api/src"),
                    M1_CONFIG=str(lab.config_path),
                    M4_TIMINGS=str(timing_path),
                )
                with (lab.root / "daemon-load.log").open("wb") as log:
                    process = subprocess.Popen(
                        [sys.executable, str(repo / "experiments/m4/daemon_probe.py")],
                        cwd=lab.root,
                        env=env,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                    )
                lab.processes.append(process)
                wait_for(lambda lab=lab: lab.client.request("GET", "/system"), 60)

                def task(index: int, lab: Any = lab) -> dict[str, Any]:
                    client = KernelClient.for_workload(
                        lab.data / "kernel.sock", provider.socket, SUPERVISOR, lab.config.audience
                    )
                    row: dict[str, Any] = {"index": index, "passed": False}
                    begin = time.monotonic()
                    try:
                        request = DelegationTaskRequest(
                            request_id=f"load-{index}",
                            agent_id="agent-a",
                            namespace="test",
                            objective="Verify both independent child results",
                            grants=(*RIGHTS, "tasks.delegate", "host.sha256", "host.copy"),
                            minimum_success=2,
                            children=(
                                {
                                    "agent_id": "agent-b",
                                    "operation": "host.sha256",
                                    "payload": {"x": index},
                                },
                                {
                                    "agent_id": "agent-b",
                                    "operation": "host.copy",
                                    "payload": {"x": index},
                                },
                            ),
                        )
                        identity = client.submit_delegation(request).task.task_id
                        row.update(task_id=identity, admission_ms=1000 * (time.monotonic() - begin))
                        deadline = time.monotonic() + 180
                        while time.monotonic() < deadline:
                            observed = client.inspect(identity).task
                            if observed.state.value in {"succeeded", "failed", "cancelled"}:
                                break
                            time.sleep(0.2)
                        row["completion_ms"] = 1000 * (time.monotonic() - begin)
                        assert observed.state.value == "succeeded"
                        final = client.request("GET", f"/tasks/{identity}/output")
                        links = client.request("GET", f"/tasks/{identity}/children")
                        assert len(links["children"]) == 2
                        receipts = [
                            client.request("GET", f"/tasks/{c['child_id']}/receipt")["receipt"]
                            for c in links["children"]
                        ]
                        assert all(r["state"] == "succeeded" for r in receipts)
                        assert (
                            final["content"]["verified_children"] == 2
                            and final["artifact"]["version"] == 1
                        )
                        assert observed.verification and observed.verification.passed
                        assert client.submit_delegation(request).task.task_id == identity
                        row.update(passed=True, receipts=receipts, final=final, links=links)
                    except Exception as error:
                        row.update(
                            error=type(error).__name__,
                            code=getattr(error, "code", "acceptance_failed"),
                        )
                    finally:
                        client.close()
                    return row

                begin = time.monotonic()
                with ThreadPoolExecutor(max_workers=profile["concurrency"]) as pool:
                    rows = list(pool.map(task, range(profile["tasks"])))
                duration = time.monotonic() - begin
                with sqlite3.connect(lab.data / "kernel.sqlite3") as db:
                    integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
                    foreign = db.execute("PRAGMA foreign_key_check").fetchall()
                    bindings = db.execute("SELECT COUNT(*) FROM task_bindings").fetchone()[0]
                    effects = db.execute("SELECT COUNT(*) FROM host_reservations").fetchone()[0]
                    versions = db.execute(
                        "SELECT resource_id,COUNT(*) FROM objects GROUP BY resource_id"
                    ).fetchall()
                passed = (
                    all(row["passed"] for row in rows)
                    and integrity == "ok"
                    and not foreign
                    and bindings == profile["tasks"] * 3
                    and effects == profile["tasks"] * 2
                    and len(versions) == bindings * 2
                    and all(n == 1 for _, n in versions)
                )

                def p95(key: str, rows: list[dict[str, Any]] = rows) -> float:
                    values = [r[key] for r in rows if key in r]
                    return (
                        statistics.quantiles(values, n=100, method="inclusive")[94]
                        if len(values) > 1
                        else 0
                    )

                timings = (
                    [json.loads(line) for line in timing_path.read_text().splitlines()]
                    if timing_path.exists()
                    else []
                )
                protocol = {
                    method: {
                        "calls": len(
                            values := [t["elapsed_ms"] for t in timings if t["method"] == method]
                        ),
                        "total_ms": sum(values),
                        "median_ms": statistics.median(values),
                    }
                    for method in sorted({t["method"] for t in timings})
                }
                result["points"].append(
                    {
                        "repetition": repetition,
                        "tasks": rows,
                        "passed": passed,
                        "duration_seconds": duration,
                        "admission_p95_ms": p95("admission_ms"),
                        "completion_p95_ms": p95("completion_ms"),
                        "completion_rps": sum(r["passed"] for r in rows) / duration,
                        "protocol_roundtrips": protocol,
                        "bindings": bindings,
                        "effects": effects,
                        "integrity": integrity,
                        "object_versions": versions,
                    }
                )
            finally:
                factory.close()
        result["complete"] = True
    finally:
        provider.close()
        with (output / "run.json").open("x") as stream:
            json.dump(result, stream, indent=2, sort_keys=True)
    if not all(p["passed"] for p in result["points"]):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
