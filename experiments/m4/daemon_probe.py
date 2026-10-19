"""Acceptance-only timing wrapper; persists no payload, identity proof or provider secret."""

import json
import os
import time
from pathlib import Path

import httpx
from cardea.hosts.debian import DebianHost
from cardea.service import server
from cardea.service.database import private_file
from cardea.service.models import ServiceConfig

config = ServiceConfig.model_validate_json(private_file(Path(os.environ["M1_CONFIG"])))


class TimedTransport(httpx.AsyncHTTPTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        begin = time.monotonic()
        response = await super().handle_async_request(request)
        await response.aread()
        if request.url.host == "cardea":
            method = (
                "AgentCard" if request.method == "GET" else json.loads(request.content)["method"]
            )
            data = {
                "method": method,
                "status": response.status_code,
                "elapsed_ms": 1000 * (time.monotonic() - begin),
            }
            with Path(os.environ["M4_TIMINGS"]).open("a") as stream:
                stream.write(json.dumps(data) + "\n")
        return response


if os.environ.get("M4_TIMINGS"):
    httpx.AsyncHTTPTransport = TimedTransport  # type: ignore[misc]


class CorruptHash(DebianHost):
    def __init__(self) -> None:
        super().__init__()
        self.operations: dict[str, str] = {}

    def start(
        self, job: str, content: bytes, deadline: float, operation: str = "host.sha256"
    ) -> None:
        self.operations[job] = operation
        super().start(job, content, deadline, operation)

    def status(self, job: str):
        result = super().status(job)
        if result["state"] == "finished" and self.operations.get(job) == "host.sha256":
            result["output"] = (
                "e30="  # {}: successful worker result fails independent verification.
            )
        return result


if os.environ.get("M4_CORRUPT_HASH") == "1":
    server.DebianHost = CorruptHash

server.serve(config)
