"""Disposable loopback server, independent workers, and fictional provider state."""

import asyncio
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from temporalio.api.common.v1 import Payloads, WorkflowExecution
from temporalio.api.enums.v1 import EventType
from temporalio.api.workflowservice.v1 import SignalWorkflowExecutionRequest
from temporalio.client import Client, WorkflowHandle

WORKER = Path(__file__).resolve().parents[1] / "worker.py"


class Server:
    def __init__(self, root: Path, binary: str) -> None:
        self.root = root
        self.binary = binary
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.address = f"127.0.0.1:{self.port}"
        self.process: subprocess.Popen[bytes] | None = None
        self.starts = 0

    def start(self) -> None:
        with (self.root / f"server-{self.starts}.log").open("wb") as log:
            self.process = subprocess.Popen(
                [
                    self.binary,
                    "server",
                    "start-dev",
                    "--ip",
                    "127.0.0.1",
                    "--port",
                    str(self.port),
                    "--headless",
                    "--db-filename",
                    str(self.root / "temporal.sqlite"),
                    "--sqlite-pragma",
                    "synchronous=FULL",
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        self.starts += 1

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=10)

    async def connect(self) -> Client:
        deadline = time.monotonic() + 30
        while True:
            try:
                client = await asyncio.wait_for(Client.connect(self.address), timeout=2)
                if await client.service_client.check_health(timeout=timedelta(seconds=2)):
                    return client
            except (RuntimeError, TimeoutError):
                pass
            assert time.monotonic() < deadline, "Server readiness timed out"
            await asyncio.sleep(0.1)


@pytest.fixture(scope="session")
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Server]:
    binary = os.environ.get("TEMPORAL_CLI") or shutil.which("temporal")
    if not binary:
        pytest.fail("Set TEMPORAL_CLI to the verified Temporal CLI 1.9.1 binary; see README")
    version = subprocess.check_output([binary, "--version"], text=True).strip()
    assert version == "temporal version 1.9.1 (Server 1.32.0, UI 2.54.1)", version
    instance = Server(tmp_path_factory.mktemp("temporal-server"), binary)
    instance.start()
    try:
        yield instance
    finally:
        instance.stop()


class Lab:
    def __init__(self, root: Path, server: Server, client: Client) -> None:
        self.root = root
        self.server = server
        self.client = client
        self.id = "fit-" + uuid4().hex
        self.processes: list[subprocess.Popen[bytes]] = []
        self.handle: WorkflowHandle[Any, Any] | None = None
        self.observations: dict[str, Any] = {}
        with sqlite3.connect(root / "world.sqlite") as conn:
            conn.executescript("""
                CREATE TABLE effects(kind TEXT, value INTEGER, worker INTEGER);
                CREATE TABLE authority(allowed INTEGER, generation INTEGER);
                INSERT INTO authority VALUES (1, 1);
                CREATE TABLE intents(id INTEGER PRIMARY KEY);
            """)

    async def worker(self, generation: int = 1) -> subprocess.Popen[bytes]:
        env = dict(os.environ)
        for key in list(env):
            if key.startswith(("TEMPORAL_", "OTEL_")):
                env.pop(key)
        env["FIT_GENERATION"] = str(generation)
        with (self.root / f"worker-{len(self.processes)}.log").open("wb") as log:
            proc = subprocess.Popen(
                [sys.executable, str(WORKER), self.server.address, self.id, str(self.root)],
                cwd=self.root,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        self.processes.append(proc)
        await self.wait_file(f"{proc.pid}.launched")
        return proc

    async def start(self, scenario: str, value: int = 1, **options: Any) -> None:
        self.handle = await self.client.start_workflow(
            "FitWorkflow",
            args=[str(self.root), scenario, value],
            id=self.id,
            task_queue=self.id,
            task_timeout=timedelta(seconds=2),
            **options,
        )

    async def wait_file(self, name: str, timeout: float = 15) -> None:
        deadline = time.monotonic() + timeout
        while not (self.root / name).exists():
            assert time.monotonic() < deadline, self.logs()
            await asyncio.sleep(0.03)

    def release(self, name: str) -> None:
        (self.root / (name + ".release")).touch()

    def logs(self) -> str:
        return "\n".join(p.read_text() for p in self.root.glob("worker-*.log"))

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

    async def phase(self, expected: str, *, child: bool = False) -> None:
        handle = self.client.get_workflow_handle(self.id + ("-child" if child else ""))
        deadline = time.monotonic() + 15
        while True:
            try:
                if await handle.query("state", rpc_timeout=timedelta(seconds=2)) == expected:
                    return
            except Exception:
                if time.monotonic() >= deadline:
                    raise
            assert time.monotonic() < deadline, self.logs()
            await asyncio.sleep(0.05)

    async def send(
        self, value: str, topic: str = "input", key: str | None = None, *, child: bool = False
    ) -> None:
        identity = self.id + ("-child" if child else "")
        if key is None:
            await self.client.get_workflow_handle(identity).signal("input", args=[value, topic])
        else:
            # Public protobuf RPC: high-level signal() generates a fresh request ID.
            payloads = await self.client.data_converter.encode([value, topic])
            await self.client.workflow_service.signal_workflow_execution(
                SignalWorkflowExecutionRequest(
                    namespace="default",
                    workflow_execution=WorkflowExecution(workflow_id=identity),
                    signal_name="input",
                    input=Payloads(payloads=payloads),
                    request_id=key,
                    identity="fictional-fit-client",
                )
            )

    async def result(self) -> Any:
        assert self.handle is not None
        result = await asyncio.wait_for(self.handle.result(), timeout=20)
        self.observations["result"] = result
        return result

    async def history(self) -> list[dict[str, Any]]:
        assert self.handle is not None
        history = await self.handle.fetch_history()
        return [
            {"id": event.event_id, "type": EventType.Name(event.event_type)}
            for event in history.events
        ]

    async def wait_history(self, name: str) -> None:
        deadline = time.monotonic() + 15
        while not any(event["type"] == name for event in await self.history()):
            assert time.monotonic() < deadline, self.logs()
            await asyncio.sleep(0.03)

    def close(self) -> None:
        for proc in self.processes:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)


@pytest.fixture
async def lab(tmp_path: Path, server: Server, request: pytest.FixtureRequest) -> AsyncIterator[Lab]:
    instance = Lab(tmp_path, server, await server.connect())
    initial_starts = server.starts
    try:
        yield instance
    finally:
        instance.close()
        instance.observations.update(
            {
                "profile": "temporal-dev-sqlite",
                "worker_processes": len(instance.processes),
                "server_restarts": server.starts - initial_starts,
                "output_effects": instance.effects("output"),
                "external_effects": instance.effects("external"),
            }
        )
        if instance.handle is not None:
            instance.observations["history"] = await instance.history()
        request.node.user_properties.append(("observations", json.dumps(instance.observations)))
