"""Disposable native engine and PostgreSQL; generated credentials stay in /tmp."""

import hashlib
import json
import os
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest

EXPERIMENT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EXPERIMENT))
from runtime import Input, client  # noqa: E402

SIDECAR_SHA256 = "3903d6c7057ee2d1bc4d3809981287191e3106a1f3be812ae78f7d77855b98ff"


def clean_environment() -> dict[str, str]:
    return {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(
            ("HATCHET_", "DATABASE_", "SERVER_", "OTEL_", "PG", "ADMIN_", "ENCRYPTION_")
        )
    }


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="session")
def engine() -> Iterator[Path]:
    binary = os.environ.get("HATCHET_FIT_SIDECAR")
    assert binary, "Set HATCHET_FIT_SIDECAR to the verified v0.107.0 native sidecar"
    assert hashlib.sha256(Path(binary).read_bytes()).hexdigest() == SIDECAR_SHA256
    with tempfile.TemporaryDirectory(prefix="cardea-hatchet-", dir="/tmp") as name:
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
                f"-h '' -k {root} -c timezone=UTC",
                "-w",
                "start",
            ],
            check=True,
            capture_output=True,
        )
        proc = None
        try:
            url = f"postgresql://cardea_fit@/postgres?host={root}&sslmode=disable"
            with psycopg.connect(url, autocommit=True) as conn:
                conn.execute("CREATE DATABASE hatchet_fit")
            url = f"postgresql://cardea_fit@/hatchet_fit?host={root}&sslmode=disable"
            handshake = root / "connection.json"
            with (root / "engine.log").open("wb") as log:
                proc = subprocess.Popen(
                    [
                        binary,
                        "-database-url",
                        url,
                        "-handshake-file",
                        str(handshake),
                        "-grpc-port",
                        str(free_port()),
                        "-api-port",
                        str(free_port()),
                        "-log-level",
                        "warn",
                    ],
                    env=clean_environment(),
                    cwd=root,
                    stdin=subprocess.PIPE,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            deadline = time.monotonic() + 90
            while not handshake.exists():
                assert proc.poll() is None, re.sub(
                    r"eyJ[A-Za-z0-9_.-]+",
                    "<redacted-token>",
                    (root / "engine.log").read_text()[-5000:],
                )
                assert time.monotonic() < deadline, "Embedded readiness timed out"
                time.sleep(0.1)
            yield handshake
        finally:
            if proc is not None:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)
                if proc.stdin is not None:
                    proc.stdin.close()
            subprocess.run(
                ["pg_ctl", "-D", str(data), "-m", "immediate", "-w", "stop"],
                check=True,
                capture_output=True,
            )


class Lab:
    def __init__(self, root: Path, connection: Path) -> None:
        self.root = root
        self.connection = connection
        self.namespace = "fit" + uuid4().hex
        self.hatchet = client(connection, self.namespace)
        self.processes: list[subprocess.Popen[bytes]] = []
        self.run_id: str | None = None
        self.observations: dict[str, Any] = {}
        with sqlite3.connect(root / "world.sqlite") as conn:
            conn.executescript("""
                CREATE TABLE effects(kind TEXT, value INTEGER);
                CREATE TABLE authority(allowed INTEGER);
                INSERT INTO authority VALUES (1);
                CREATE TABLE intents(id INTEGER PRIMARY KEY);
            """)

    def worker(self) -> subprocess.Popen[bytes]:
        (self.root / "worker.ready").unlink(missing_ok=True)
        env = clean_environment()
        env["FIT_CONNECTION"] = str(self.connection)
        env["FIT_NAMESPACE"] = self.namespace
        env["FIT_ROOT"] = str(self.root)
        with (self.root / f"worker-{len(self.processes)}.log").open("wb") as log:
            proc = subprocess.Popen(
                [sys.executable, str(EXPERIMENT / "worker.py")],
                env=env,
                cwd=self.root,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        self.processes.append(proc)
        self.wait_file("worker.ready", timeout=30)
        return proc

    def wait_file(self, name: str, timeout: float = 30) -> None:
        deadline = time.monotonic() + timeout
        while not (self.root / name).exists():
            assert time.monotonic() < deadline, self.logs()
            time.sleep(0.05)

    def logs(self) -> str:
        # Strip tokens if a dependency ever includes one in an error.
        import re

        raw = "\n".join(p.read_text()[-7000:] for p in self.root.glob("worker-*.log"))
        return re.sub(r"eyJ[A-Za-z0-9_.-]+", "<redacted-token>", raw)

    def start(self, scenario: str = "checkpoint", value: int = 1, key: str | None = None) -> str:
        ref = self.hatchet.stubs.workflow(name="fit", input_validator=Input).run(
            Input(
                root=str(self.root),
                scenario=scenario,
                scope=self.namespace,
                value=value,
                operation_key=key or self.namespace,
            ),
            wait_for_result=False,
        )
        self.run_id = ref.workflow_run_id
        return self.run_id

    def release(self, name: str) -> None:
        (self.root / (name + ".release")).touch()

    def emit(
        self, key: str = "continue", value: str = "first", *, scope: str | None = None
    ) -> None:
        self.hatchet.event.push(key, {"value": value}, scope=scope or self.namespace)

    def effects(self, kind: str) -> int:
        with sqlite3.connect(self.root / "world.sqlite") as conn:
            return int(
                conn.execute("SELECT COUNT(*) FROM effects WHERE kind=?", (kind,)).fetchone()[0]
            )

    def revoke(self) -> None:
        with sqlite3.connect(self.root / "world.sqlite") as conn:
            conn.execute("UPDATE authority SET allowed=0")

    def durable_entries(self) -> list[Any]:
        assert self.run_id is not None
        with psycopg.connect(
            host=str(self.connection.parent), dbname="hatchet_fit", user="cardea_fit"
        ) as conn:
            return conn.execute(
                "SELECT e.kind::text, e.is_satisfied, e.wait_data "
                "FROM v1_durable_event_log_entry e "
                "JOIN v1_task t ON e.durable_task_id=t.id "
                "AND e.durable_task_inserted_at=t.inserted_at "
                "WHERE t.external_id=%s ORDER BY e.id",
                (self.run_id,),
            ).fetchall()

    def wait_registration(self, kind: str = "USER_EVENT") -> None:
        deadline = time.monotonic() + 30
        while True:
            entries = self.durable_entries()
            if any(
                entry_kind == "WAIT_FOR"
                and not satisfied
                and data
                and any(c["kind"] == kind for c in data["conditions"])
                for entry_kind, satisfied, data in entries
            ):
                self.observations["registered_wait_kind"] = kind
                return
            assert time.monotonic() < deadline, (entries, self.logs())
            time.sleep(0.05)

    def wait_state(self, expected: str, timeout: float = 60) -> None:
        assert self.run_id is not None
        deadline = time.monotonic() + timeout
        while (status := self.hatchet.runs.get_status(self.run_id).value) != expected:
            assert time.monotonic() < deadline, (status, self.logs())
            time.sleep(0.2)
        self.observations["status"] = expected

    def result(self, timeout: float = 60) -> Any:
        assert self.run_id is not None
        started = time.monotonic()
        deadline = started + timeout
        while True:
            status = self.hatchet.runs.get_status(self.run_id).value
            if status in ("COMPLETED", "FAILED", "CANCELLED"):
                self.observations["status"] = status
                assert status == "COMPLETED", (status, self.logs())
                result = self.hatchet.runs.get_result(self.run_id)
                self.observations["result"] = result
                self.observations["result_wait_seconds"] = round(time.monotonic() - started, 3)
                return result
            assert time.monotonic() < deadline, (status, self.logs())
            time.sleep(0.2)

    def close(self) -> None:
        for proc in self.processes:
            with suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)


@pytest.fixture
def lab(tmp_path: Path, engine: Path, request: pytest.FixtureRequest) -> Iterator[Lab]:
    instance = Lab(tmp_path, engine)
    try:
        yield instance
    finally:
        instance.close()
        if instance.run_id is not None:
            instance.observations["durable_entries"] = instance.durable_entries()
        instance.observations.update(
            {
                "output_effects": instance.effects("output"),
                "external_effects": instance.effects("external"),
                "worker_processes": len(instance.processes),
            }
        )
        request.node.user_properties.append(("observations", json.dumps(instance.observations)))
