"""Trusted disposable M3 fixture, launched with the target API interpreter only."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any


def main() -> None:
    repo, output = Path(sys.argv[1]), Path(sys.argv[2])
    kind, profile = sys.argv[3], json.loads(sys.argv[4])
    if os.environ.get("M3_LIVE_KEY_FILE"):
        raise ValueError("Load profile forbids live provider credentials")
    sys.path[:0] = [
        str(repo / p)
        for p in ("apps/api/src", "experiments/m3", "experiments/m1", "experiments/spire")
    ]
    from cardea.runtime.incidents import (  # type: ignore[import-not-found]
        IncidentArtifacts,
        IncidentBatch,
        verify,
    )
    from cardea.service.client import KernelClient  # type: ignore[import-not-found]
    from cardea.service.models import (  # type: ignore[import-not-found]
        RIGHTS,
        AgentInput,
        AgentTaskRequest,
        HostBudget,
        ModelBudget,
    )
    from provider_lab import Lab, eventually  # type: ignore[import-not-found]
    from test_integration import SUPERVISOR, service  # type: ignore[import-not-found]

    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    sources = {
        str(p.relative_to(repo)): digest(p)
        for p in sorted((repo / "apps/api/src").rglob("*"))
        if p.is_file() and p.suffix in {".py", ".json"}
    }
    harness = {
        str(p.relative_to(repo)): digest(p)
        for directory in (
            "experiments/m1",
            "experiments/m3",
            "experiments/spire",
            "packages/cardea-load/src",
        )
        for p in sorted((repo / directory).rglob("*.py"))
    }
    limits = {
        name: Path("/sys/fs/cgroup", name).read_text().strip()
        if Path("/sys/fs/cgroup", name).exists()
        else "root-unlimited"
        for name in ("cpu.max", "memory.max", "pids.max")
    }
    contract = {
        "profile": profile,
        "harness": harness,
        "api_lock": digest(repo / "apps/api/uv.lock"),
        "load_lock": digest(repo / "packages/cardea-load/uv.lock"),
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cpus": os.cpu_count(),
            "limits": limits,
            "memory_kib": Path("/proc/meminfo").read_text().splitlines()[0],
            "systemd": subprocess.check_output(
                ["systemctl", "--version"], text=True
            ).splitlines()[0],
            "bwrap": subprocess.check_output(["bwrap", "--version"], text=True).strip(),
        },
    }
    result: dict[str, Any] = {
        "schema_version": "m3-load-1",
        "kind": kind,
        "complete": False,
        "contract": contract,
        "source": sources,
        "points": [],
    }
    destination = output / "run.json"
    if destination.exists():
        raise FileExistsError(destination)
    provider = Lab()
    try:
        provider.start()
        provider.register(SUPERVISOR)
        eventually(lambda: provider.token(SUPERVISOR))
        for repetition in range(2 if kind == "reference" else 1):
            with tempfile.TemporaryDirectory(prefix="m3-load-") as private:
                factory = service.__wrapped__(provider, Path(private))
                lab = next(factory)
                try:
                    lab.document = lab.document.model_copy(
                        update={
                            "model_budgets": (
                                ModelBudget(
                                    principal_id="supervisor",
                                    namespace="test",
                                    calls=profile["model_calls"],
                                    tokens=profile["model_tokens"],
                                ),
                            ),
                            "host_budgets": (
                                HostBudget(
                                    principal_id="supervisor",
                                    namespace="test",
                                    tool_milliseconds=profile["tool_milliseconds"],
                                    output_bytes=profile["output_bytes"],
                                    concurrent_operations=profile["host_concurrency"],
                                ),
                            ),
                        }
                    )
                    lab.apply(lab.document)
                    lab.start()

                    def task(index: int, lab: Any = lab) -> dict[str, Any]:
                        client = KernelClient.for_workload(
                            lab.data / "kernel.sock",
                            provider.socket,
                            SUPERVISOR,
                            lab.config.audience,
                        )
                        row: dict[str, Any] = {"index": index, "passed": False}
                        start = time.monotonic()
                        try:
                            initial = {
                                "records": [
                                    {"id": "INC-1", "title": "Fictional delay", "severity": "low"}
                                ]
                            }
                            full = {
                                "records": [
                                    *initial["records"],
                                    {
                                        "id": "INC-2",
                                        "title": "Fictional recovery",
                                        "severity": "low",
                                    },
                                ]
                            }
                            request = AgentTaskRequest(
                                request_id=f"load-{index}",
                                agent_id="agent-a",
                                namespace="test",
                                objective="Compile the authorized incident records.",
                                inputs=initial,
                                grants=(
                                    *RIGHTS,
                                    "models.invoke",
                                    "models.disclose",
                                    "tools.incidents",
                                    "inputs.append",
                                ),
                            )
                            identity = client.submit_agent(request).task.task_id
                            row.update(
                                task_id=identity, admission_ms=1000 * (time.monotonic() - start)
                            )

                            def wait(expected: str) -> Any:
                                deadline = time.monotonic() + 120
                                while time.monotonic() < deadline:
                                    observed = client.inspect(identity).task
                                    if observed.state.value == expected:
                                        return observed
                                    if observed.state.value in {"failed", "expired", "cancelled"}:
                                        row["terminal"] = observed.state.value
                                        row["failure"] = (
                                            observed.failure.model_dump(mode="json")
                                            if observed.failure
                                            else None
                                        )
                                        raise ValueError("terminal_" + observed.state.value)
                                    time.sleep(0.2)
                                raise TimeoutError("task_wait")

                            wait("waiting")
                            first = client.request("GET", f"/tasks/{identity}/output")
                            assert first["artifact"]["version"] == 1
                            verify(
                                IncidentBatch.model_validate(initial),
                                IncidentArtifacts.model_validate(first["content"]),
                            )
                            update = AgentInput(request_id=f"update-{index}", inputs=full)
                            client.append_input(identity, update)
                            observed = wait("succeeded")
                            row["completion_ms"] = 1000 * (time.monotonic() - start)
                            final = client.request("GET", f"/tasks/{identity}/output")
                            receipts = client.request("GET", f"/tasks/{identity}/agent-receipts")[
                                "receipts"
                            ]
                            verify(
                                IncidentBatch.model_validate(full),
                                IncidentArtifacts.model_validate(final["content"]),
                            )
                            assert final["artifact"]["version"] == 2
                            assert observed.verification and observed.verification.passed
                            assert len(receipts) == 4 and all(
                                r["state"] == "succeeded" for r in receipts
                            )
                            assert sum(r["usage"]["model_calls"] for r in receipts) == 2
                            # Duplicate update must not create another output or model effect.
                            client.append_input(identity, update)
                            row.update(
                                passed=True,
                                output_versions=2,
                                receipts=receipts,
                                final=final,
                                verification=observed.verification.model_dump(mode="json"),
                            )
                        except Exception as exc:
                            row["error"] = type(exc).__name__
                            row["code"] = getattr(exc, "code", "scenario_failed")
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
                        bindings = db.execute("SELECT count(*) FROM task_bindings").fetchone()[0]
                        versions = db.execute(
                            "SELECT resource_id,count(*) FROM objects GROUP BY resource_id"
                        ).fetchall()
                        effects = db.execute("SELECT count(*) FROM agent_effects").fetchone()[0]
                    passed = (
                        all(r["passed"] for r in rows)
                        and integrity == "ok"
                        and not foreign
                        and bindings == profile["tasks"]
                        and effects == 4 * bindings
                        and len(versions) == 2 * bindings
                        and all(n == 2 for _, n in versions)
                    )

                    def p95(key: str, rows: list[dict[str, Any]] = rows) -> float:
                        values = [r[key] for r in rows if key in r]
                        return (
                            statistics.quantiles(values, n=100, method="inclusive")[94]
                            if len(values) > 1
                            else 0
                        )

                    result["points"].append(
                        {
                            "repetition": repetition,
                            "tasks": rows,
                            "passed": passed,
                            "duration_seconds": duration,
                            "admission_p95_ms": p95("admission_ms"),
                            "completion_p95_ms": p95("completion_ms"),
                            "completion_rps": sum(r["passed"] for r in rows) / duration,
                            "integrity": integrity,
                            "bindings": bindings,
                            "effects": effects,
                            "object_versions": versions,
                        }
                    )
                finally:
                    factory.close()
        result["complete"] = True
    finally:
        provider.close()
        with destination.open("x") as stream:
            json.dump(result, stream, indent=2, sort_keys=True)
    if not all(p["passed"] for p in result["points"]):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
