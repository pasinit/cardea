"""Private PostgreSQL, native Absurd APIs and read-only observations."""

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from absurd_sdk import Absurd, JsonValue, SpawnResult, TaskResultSnapshot

EXPERIMENT = Path(__file__).resolve().parents[1]
SCHEMA_SHA256 = "d34309370c539f3a51f2b36b69b1f77551f8e4a14480a1c8def8bb8f40fd9aab"


class Postgres:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.data = root / "data"
        self.starts = 0

    def start(self) -> None:
        subprocess.run(
            [
                "pg_ctl",
                "-D",
                str(self.data),
                "-l",
                str(self.root / "postgres.log"),
                "-o",
                f"-h '' -k {self.root}",
                "-w",
                "start",
            ],
            check=True,
            capture_output=True,
        )
        self.starts += 1

    def stop(self) -> None:
        subprocess.run(
            ["pg_ctl", "-D", str(self.data), "-m", "immediate", "-w", "stop"],
            check=True,
            capture_output=True,
        )


@pytest.fixture(scope="session")
def postgres() -> Iterator[Postgres]:
    if not shutil.which("initdb") or not shutil.which("pg_ctl"):
        pytest.fail("PostgreSQL initdb/pg_ctl required; see experiment README")
    with tempfile.TemporaryDirectory(prefix="cardea-absurd-pg-", dir="/tmp") as name:
        pg = Postgres(Path(name))
        subprocess.run(
            [
                "initdb",
                "-D",
                str(pg.data),
                "-A",
                "trust",
                "-U",
                "cardea_fit",
                "--no-locale",
                "--encoding=UTF8",
            ],
            check=True,
            capture_output=True,
        )
        pg.start()
        try:
            yield pg
        finally:
            pg.stop()


class Lab:
    def __init__(self, root: Path, url: str, pg: Postgres) -> None:
        self.root, self.url, self.pg = root, url, pg
        self.client = Absurd(url)
        self.client.create_queue()
        self.client.create_queue("children")
        self.processes: list[subprocess.Popen[bytes]] = []
        self.task: SpawnResult | None = None
        self.observations: dict[str, Any] = {}
        with sqlite3.connect(root / "world.sqlite") as conn:
            conn.executescript("""
                CREATE TABLE effects(kind TEXT, value INTEGER, worker INTEGER);
                CREATE TABLE authority(allowed INTEGER, generation INTEGER);
                INSERT INTO authority VALUES (1, 1);
                CREATE TABLE intents(id INTEGER PRIMARY KEY);
            """)

    def worker(self, *, generation: int = 1, queue: str = "default") -> subprocess.Popen[bytes]:
        env = dict(os.environ)
        for key in list(env):
            if key.startswith(("ABSURD_", "PG", "OTEL_")):
                env.pop(key)
        env["FIT_DATABASE_URL"] = self.url
        env["FIT_GENERATION"] = str(generation)
        with (self.root / f"worker-{len(self.processes)}.log").open("wb") as log:
            proc = subprocess.Popen(
                [sys.executable, str(EXPERIMENT / "worker.py"), str(self.root), "--queue", queue],
                cwd=self.root,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        self.processes.append(proc)
        self.wait_file(f"{proc.pid}.launched")
        return proc

    def spawn(self, scenario: str, value: int = 1, **options: Any) -> SpawnResult:
        self.task = self.client.spawn(
            "fit",
            {"root": str(self.root), "scenario": scenario, "value": value},
            queue="default",
            retry_strategy={"kind": "fixed", "base_seconds": 0.1},
            **options,
        )
        return self.task

    def wait_file(self, name: str, timeout: float = 15) -> None:
        deadline = time.monotonic() + timeout
        while not (self.root / name).exists():
            assert time.monotonic() < deadline, self.logs()
            time.sleep(0.03)

    def logs(self) -> str:
        return "\n".join(p.read_text()[-6000:] for p in self.root.glob("worker-*.log"))

    def release(self, name: str) -> None:
        (self.root / (name + ".release")).touch()

    def inspect(self, query: str, values: tuple[Any, ...] = ()) -> list[Any]:
        # Only fixed SELECT statements supplied by these tests; no state mutation.
        assert query.lstrip().upper().startswith("SELECT ")
        with psycopg.connect(self.url, autocommit=True) as conn:
            return conn.execute(query, values).fetchall()

    def wait_wait(self, event: str, *, child: bool = False) -> None:
        query = (
            "SELECT count(*) FROM absurd.w_children WHERE event_name=%s"
            if child
            else "SELECT count(*) FROM absurd.w_default WHERE event_name=%s"
        )
        deadline = time.monotonic() + 15
        while not self.inspect(query, (event,))[0][0]:
            assert time.monotonic() < deadline, self.logs()
            time.sleep(0.03)

    def snapshot(self) -> TaskResultSnapshot:
        assert self.task is not None
        snapshot = self.client.fetch_task_result(self.task["task_id"])
        assert snapshot is not None
        return snapshot

    def wait_state(self, state: str) -> None:
        deadline = time.monotonic() + 15
        while self.snapshot().state != state:
            assert time.monotonic() < deadline, (self.snapshot(), self.logs())
            time.sleep(0.03)

    def result(self) -> JsonValue:
        assert self.task is not None
        snapshot = self.client.await_task_result(self.task["task_id"], timeout=20)
        self.observations["snapshot"] = asdict(snapshot)
        assert snapshot.state == "completed", (snapshot, self.logs())
        return snapshot.result

    def effects(self, kind: str) -> int:
        with sqlite3.connect(self.root / "world.sqlite") as conn:
            return int(
                conn.execute("SELECT COUNT(*) FROM effects WHERE kind=?", (kind,)).fetchone()[0]
            )

    def revoke(self) -> None:
        with sqlite3.connect(self.root / "world.sqlite") as conn:
            conn.execute("UPDATE authority SET allowed=0")

    def replace_owner(self) -> None:
        with sqlite3.connect(self.root / "world.sqlite") as conn:
            conn.execute("UPDATE authority SET generation=2")

    def stop_workers(self) -> None:
        for proc in self.processes:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)


@pytest.fixture
def lab(tmp_path: Path, postgres: Postgres, request: pytest.FixtureRequest) -> Iterator[Lab]:
    schema = (EXPERIMENT / "vendor/absurd-0.5.0.sql").read_bytes()
    assert hashlib.sha256(schema).hexdigest() == SCHEMA_SHA256
    database = "fit_" + uuid4().hex
    with psycopg.connect(
        host=str(postgres.root), dbname="postgres", user="cardea_fit", autocommit=True
    ) as conn:
        conn.execute(psycopg.sql.SQL("CREATE DATABASE {}").format(psycopg.sql.Identifier(database)))
    url = f"postgresql://cardea_fit@/{database}?host={postgres.root}"
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(schema.decode())
    instance = Lab(tmp_path, url, postgres)
    starts = postgres.starts
    try:
        yield instance
    finally:
        instance.stop_workers()
        instance.observations.update(
            {
                "profile": "postgres-sync-sdk",
                "worker_processes": len(instance.processes),
                "database_restarts": postgres.starts - starts,
                "output_effects": instance.effects("output"),
                "external_effects": instance.effects("external"),
                "runs": instance.inspect(
                    "SELECT attempt, state, claimed_by, result, failure_reason "
                    "FROM absurd.r_default ORDER BY created_at, attempt"
                ),
                "checkpoints": instance.inspect(
                    "SELECT checkpoint_name, state, status "
                    "FROM absurd.c_default ORDER BY checkpoint_name"
                ),
            }
        )
        if instance.task is not None:
            instance.observations["final_snapshot"] = asdict(instance.snapshot())
        request.node.user_properties.append(("observations", json.dumps(instance.observations)))
        instance.client.close()
