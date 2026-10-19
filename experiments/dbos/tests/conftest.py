"""Private databases and child processes only; no existing service is touched."""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from dbos import DBOSClient

WORKER = Path(__file__).resolve().parents[1] / "worker.py"


@pytest.fixture(scope="session")
def postgres_socket() -> Iterator[Path]:
    if not shutil.which("initdb") or not shutil.which("pg_ctl"):
        pytest.skip("PostgreSQL binaries unavailable; PostgreSQL profile NOT RUN")
    with tempfile.TemporaryDirectory(prefix="cardea-pg-", dir="/tmp") as name:
        root = Path(name)
        data = root / "data"
        subprocess.run(
            [
                "initdb",
                "-D",
                str(data),
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
        subprocess.run(
            [
                "pg_ctl",
                "-D",
                str(data),
                "-l",
                str(root / "postgres.log"),
                "-o",
                f"-h '' -k {root}",
                "-w",
                "start",
            ],
            check=True,
            capture_output=True,
        )
        try:
            yield root
        finally:
            subprocess.run(
                ["pg_ctl", "-D", str(data), "-m", "immediate", "-w", "stop"],
                check=True,
                capture_output=True,
            )


class Lab:
    def __init__(self, root: Path, url: str) -> None:
        self.root = root
        self.url = url
        self.processes: list[subprocess.Popen[bytes]] = []
        self.client: DBOSClient | None = None
        with sqlite3.connect(root / "world.sqlite") as conn:
            conn.executescript("""
                CREATE TABLE effects(kind TEXT, value INTEGER, worker INTEGER);
                CREATE TABLE authority(allowed INTEGER, generation INTEGER);
                INSERT INTO authority VALUES (1, 1);
                CREATE TABLE intents(id INTEGER PRIMARY KEY);
            """)

    def start(
        self, scenario: str, mode: str = "start", value: int = 1, generation: int = 1
    ) -> subprocess.Popen[bytes]:
        env = dict(os.environ)
        # No inherited provider/DBOS deployment configuration is used by the fixture.
        for key in list(env):
            if key.startswith(("DBOS_", "OTEL_")):
                env.pop(key)
        env["FIT_DATABASE_URL"] = self.url
        env["FIT_GENERATION"] = str(generation)
        logfile = self.root / f"worker-{len(self.processes)}.log"
        with logfile.open("wb") as output:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    str(WORKER),
                    mode,
                    str(self.root),
                    scenario,
                    "--value",
                    str(value),
                ],
                env=env,
                cwd=self.root,
                stdout=output,
                stderr=output,
            )
        self.processes.append(proc)
        return proc

    def wait_file(self, name: str, timeout: float = 15) -> None:
        deadline = time.monotonic() + timeout
        while not (self.root / name).exists():
            if time.monotonic() > deadline:
                pytest.fail(f"Timed out waiting for {name}\n{self.logs()}")
            time.sleep(0.03)

    def logs(self) -> str:
        return "\n".join(p.read_text()[-6000:] for p in self.root.glob("worker-*.log"))

    def release(self, name: str) -> None:
        (self.root / (name + ".release")).touch()

    def connect(self) -> DBOSClient:
        if self.client is None:
            self.client = DBOSClient(
                system_database_url=self.url, use_listen_notify=False, retry_connection_errors=False
            )
        return self.client

    def event(self, name: str) -> Any:
        return self.connect().get_event("fit-task", name, timeout_seconds=15)

    def send(self, message: str, topic: str = "input", key: str | None = None) -> None:
        self.connect().send("fit-task", message, topic=topic, idempotency_key=key)

    def result(self, proc: subprocess.Popen[bytes]) -> Any:
        self.wait_file(f"{proc.pid}.result.json", timeout=20)
        return json.loads((self.root / f"{proc.pid}.result.json").read_text())

    def effects(self, kind: str) -> int:
        with sqlite3.connect(self.root / "world.sqlite") as conn:
            return int(
                conn.execute("SELECT COUNT(*) FROM effects WHERE kind=?", (kind,)).fetchone()[0]
            )

    def replace_owner(self) -> None:
        with sqlite3.connect(self.root / "world.sqlite") as conn:
            conn.execute("UPDATE authority SET generation=2")

    def revoke(self) -> None:
        with sqlite3.connect(self.root / "world.sqlite") as conn:
            conn.execute("UPDATE authority SET allowed=0")

    def close(self) -> None:
        for proc in self.processes:
            if proc.poll() is None:
                proc.kill()  # Also kills SIGSTOP'd test workers.
            proc.wait(timeout=10)
        if self.client is not None:
            self.client.destroy()


@pytest.fixture(params=["sqlite", "postgres"])
def lab(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Lab]:
    if request.param == "postgres":
        socket = request.getfixturevalue("postgres_socket")
        database = "fit_" + uuid4().hex
        with psycopg.connect(
            host=str(socket), dbname="postgres", user="cardea_fit", autocommit=True
        ) as conn:
            conn.execute(
                psycopg.sql.SQL("CREATE DATABASE {}").format(psycopg.sql.Identifier(database))
            )
        url = f"postgresql+psycopg://cardea_fit@/{database}?host={socket}"
    else:
        url = f"sqlite:///{tmp_path / 'dbos.sqlite'}"
    instance = Lab(tmp_path, url)
    try:
        yield instance
    finally:
        instance.close()
        observations = {
            "profile": request.param,
            "worker_processes": len(instance.processes),
            "output_effects": instance.effects("output"),
            "external_effects": instance.effects("external"),
            "returned_results": [
                json.loads(path.read_text()) for path in sorted(tmp_path.glob("*.result.json"))
            ],
        }
        request.node.user_properties.append(("observations", json.dumps(observations)))
