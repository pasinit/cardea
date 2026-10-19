"""Process boundary around the target's disposable, real-provider M1 fixtures."""

from __future__ import annotations

import json
import select
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

TERMINAL = {"succeeded", "failed", "cancelled", "expired"}


class Lab:
    def __init__(self, repo: Path, api_python: Path) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="cardea-load-", dir="/tmp")
        self.private = Path(self.temp.name)
        self.log = (self.private / "fixture.log").open("w")
        self.process = subprocess.Popen(
            [
                str(api_python),
                str(Path(__file__).with_name("fixture.py")),
                str(repo),
                str(self.private),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.log,
            text=True,
            bufsize=1,
        )
        self.info: dict[str, Any] = {}
        self.client: httpx.Client | None = None
        try:
            self.provider = self.call("boot")
        except BaseException:
            self.close()
            raise

    def call(self, action: str, **options: Any) -> dict[str, Any]:
        assert self.process.stdin is not None and self.process.stdout is not None
        self.process.stdin.write(json.dumps({"action": action, **options}) + "\n")
        self.process.stdin.flush()
        if not select.select([self.process.stdout], [], [], 120)[0]:
            raise TimeoutError(f"Fixture {action} exceeded 120 seconds")
        response = json.loads(self.process.stdout.readline())
        if "error" in response:
            raise RuntimeError(f"Fixture {action}: {response['error']}; private log {self.private}")
        return dict(response["ok"])

    def reset(self) -> None:
        if self.client:
            self.client.close()
        self.info = self.call("reset")
        self.client = httpx.Client(
            transport=httpx.HTTPTransport(uds=self.info["socket"]),
            base_url="http://localhost/v0.2",
            timeout=15,
            limits=httpx.Limits(max_connections=128, max_keepalive_connections=16),
        )

    def credential(self) -> str:
        return str(json.loads(Path(self.info["credential"]).read_text())["token"])

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> httpx.Response:
        assert self.client is not None
        return self.client.request(
            method, path, json=body, headers={"Authorization": "Bearer " + self.credential()}
        )

    def drain(self, seconds: float) -> bool:
        end = time.monotonic() + seconds
        while True:
            counts = self.call("counts")
            if not any(n for state, n in counts.items() if state not in TERMINAL):
                return True
            if time.monotonic() >= end:
                return False
            time.sleep(0.5)

    def close(self) -> None:
        if self.client:
            self.client.close()
        if self.process.poll() is None:
            assert self.process.stdin is not None
            try:
                self.process.stdin.write('{"action":"close"}\n')
                self.process.stdin.flush()
                self.process.wait(timeout=25)
            except (BrokenPipeError, subprocess.TimeoutExpired):
                self.process.kill()
                self.process.wait()
        self.log.close()
        self.temp.cleanup()


def task(request_id: str, payload_bytes: int = 1024, wait: bool = False) -> dict[str, Any]:
    return {
        "schema_version": "0.2",
        "request_id": request_id,
        "agent_id": "agent-a",
        "namespace": "test",
        "objective": "Publish and independently verify load-test JSON",
        "payload": {"data": "x" * payload_bytes},
        "timeout_seconds": 300,
        "wait_for_event": wait,
    }
