"""Private SQLite state and real process-kill boundaries."""

import json
import os
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from cardea.kernel.store import SQLiteKernelStore
from dbos import DBOSClient

from adapter import TASK_ID, WORKFLOW_ID, initialize, url

EXPERIMENT = Path(__file__).resolve().parents[1]
SOURCE = EXPERIMENT.parents[1] / "apps/api/src"


class Lab:
    def __init__(self, root: Path, *, separate: bool = False) -> None:
        self.root = root
        self.separate = separate
        self.processes: list[subprocess.Popen[bytes]] = []
        self.observations: dict[str, Any] = {}
        initialize(root, separate=separate)
        self.client = DBOSClient(
            system_database_url=url(root, separate=separate), use_listen_notify=False
        )

    def launch(self, name: str, args: list[str], barrier: str = "") -> subprocess.Popen[bytes]:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("DBOS_", "OTEL_", "FIT_"))}
        env.update(FIT_ROOT=str(self.root), FIT_BARRIER=barrier, PYTHONPATH=str(SOURCE))
        with (self.root / f"process-{len(self.processes)}.log").open("wb") as log:
            proc = subprocess.Popen(
                [sys.executable, str(EXPERIMENT / name), *args],
                cwd=self.root,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        self.processes.append(proc)
        return proc

    def submit(
        self, barrier: str = "", scenario: str = "local", *, engine_first: bool = False
    ) -> subprocess.Popen[bytes]:
        args = [str(self.root), "--scenario", scenario]
        if self.separate:
            args.append("--separate")
        if engine_first:
            args.append("--engine-first")
        proc = self.launch("submit.py", args, barrier)
        if barrier:
            self.wait_file(f"{proc.pid}.{barrier}.entered")
        else:
            assert proc.wait(timeout=20) == 0, self.logs()
        return proc

    def worker(self, barrier: str = "", executor: str = "local-stable") -> subprocess.Popen[bytes]:
        proc = self.launch("worker.py", ["--executor", executor], barrier)
        self.wait_file(f"{proc.pid}.ready")
        if barrier:
            self.wait_file(f"{proc.pid}.{barrier}.entered")
        return proc

    def wait_file(self, name: str, timeout: float = 25) -> None:
        deadline = time.monotonic() + timeout
        while not (self.root / name).exists():
            if self.processes:
                assert self.processes[-1].poll() in (None, 0), self.logs()
            assert time.monotonic() < deadline, (name, self.logs())
            time.sleep(0.03)

    def logs(self) -> str:
        return "\n".join(p.read_text()[-7000:] for p in self.root.glob("process-*.log"))

    def kill(self, proc: subprocess.Popen[bytes]) -> None:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)

    def result(self, proc: subprocess.Popen[bytes]) -> str:
        self.wait_file(f"{proc.pid}.result.json")
        assert proc.wait(timeout=15) == 0, self.logs()
        value = json.loads((self.root / f"{proc.pid}.result.json").read_text())
        self.observations["result"] = value
        return str(value)

    def query(self, query: str, *, separate: bool = False) -> list[Any]:
        assert query.startswith(("SELECT", "PRAGMA"))
        with sqlite3.connect(
            self.root / ("engine.sqlite3" if separate else "kernel.sqlite3")
        ) as conn:
            return conn.execute(query).fetchall()

    def task_state(self) -> str:
        with SQLiteKernelStore(self.root) as store, store.transaction(write=False) as tx:
            return tx.get_task(TASK_ID).state.value

    def revoke(self, field: str = "supervisor_permission") -> None:
        assert field in {
            "supervisor_enabled",
            "supervisor_permission",
            "supervisor_delegation",
            "ancestor",
            "task_grant",
            "platform",
        }
        with sqlite3.connect(self.root / "kernel.sqlite3") as conn:
            conn.execute(f"UPDATE fit_authority SET {field}=0")

    def snapshot(self) -> dict[str, Any]:
        with sqlite3.connect(self.root / "provider.sqlite3") as conn:
            effects = conn.execute("SELECT COUNT(*) FROM effects").fetchone()[0]
        statuses = self.client.list_workflows(workflow_ids=[WORKFLOW_ID])
        status = statuses[0] if statuses else None
        return {
            "tasks": self.query("SELECT task_id, revision, state FROM tasks"),
            "operations": self.query("SELECT state, revision, snapshot FROM operations"),
            "events": self.query("SELECT cursor, event_type, payload FROM events ORDER BY cursor"),
            "objects": self.query("SELECT namespace,resource_id,version,digest FROM objects"),
            "provider_effects": effects,
            "engine_status": status.status if status else None,
            "engine_executor": status.executor_id if status else None,
        }

    def close(self) -> None:
        for proc in self.processes:
            self.kill(proc)
        self.client.destroy()


@pytest.fixture
def lab(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[Lab]:
    instance = Lab(tmp_path)
    try:
        yield instance
    finally:
        for proc in instance.processes:
            instance.kill(proc)
        instance.observations.update(instance.snapshot())
        request.node.user_properties.append(("observations", json.dumps(instance.observations)))
        instance.close()
