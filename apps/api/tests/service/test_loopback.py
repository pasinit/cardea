"""Per-call endpoints listen only on their loopback address (ADR-028).

Under host execution the tool endpoint and the content-gate callback listen on the
helper's dedicated address, the only loopback /32 a confined worker may reach, and refuse
requests that arrive on any other address. Binding 127.0.11.1 needs Linux, where all of
127.0.0.0/8 is local; elsewhere those tests are skipped and the portable ones still run.
"""

import asyncio
import socket
import sys
import urllib.error
import urllib.request
from collections.abc import MutableMapping
from typing import Any, cast

import pytest
from test_agent_runs import FakeSession, kernel, routine, session, submit, tools

from cardea.hosts import launcher
from cardea.service import loopback
from cardea.service.agent_run_runtime import AgentRunRuntime
from cardea.service.content_gate import GateEndpoint
from cardea.service.database import Database
from cardea.service.kernel import Kernel
from cardea.service.operations import OperationContext
from cardea.service.tool_endpoint import ToolEndpoint

# Binds or connects over TCP loopback: not run by `make check-test`.
pytestmark = pytest.mark.loopback

# Fixtures shared with the agent-run tests.
__all__ = ["kernel", "session", "tools"]

LINUX = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="only Linux has all of 127.0.0.0/8 locally"
)


async def no_call(name: str, arguments: dict[str, Any], gone: Any) -> Any:
    raise AssertionError("not called")


def answer(app: Any, server: tuple[str, int]) -> int:
    """The status an ASGI app gives an HTTP request that arrived on `server`."""
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(dict(message))

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    scope = {"type": "http", "method": "GET", "path": "/", "headers": [], "server": server}
    asyncio.run(
        loopback.OnlyOn(inner if app is None else app, launcher.ENDPOINT_ADDRESS)(
            scope, receive, send
        )
    )
    return int(sent[0]["status"])


def test_requests_on_another_address_are_refused() -> None:
    assert answer(None, (launcher.ENDPOINT_ADDRESS, 4100)) == 204
    assert answer(None, ("127.0.0.1", 4100)) == 403
    assert answer(None, ("0.0.0.0", 4100)) == 403


def test_without_host_execution_endpoints_keep_127_0_0_1() -> None:
    with ToolEndpoint([], no_call) as tools:
        assert tools.host == loopback.DEFAULT_HOST == "127.0.0.1"
        assert tools.url == f"http://127.0.0.1:{tools.port}/mcp"


def refused(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=2):
            return False
    except OSError:
        return True


def status(url: str) -> int:
    request = urllib.request.Request(url, data=b"{}", method="POST")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
            return int(response.status)
    except urllib.error.HTTPError as error:
        return int(error.code)


@LINUX
def test_the_tool_endpoint_binds_only_the_dedicated_address() -> None:
    with ToolEndpoint([], no_call, launcher.ENDPOINT_ADDRESS) as tools:
        assert tools.url == f"http://{launcher.ENDPOINT_ADDRESS}:{tools.port}/mcp"
        # It serves there (401: no bearer token) and nothing listens on 127.0.0.1.
        assert status(tools.url) == 401
        assert refused("127.0.0.1", tools.port)


@LINUX
def test_the_gate_callback_binds_only_the_dedicated_address() -> None:
    context = cast(OperationContext, None)
    with GateEndpoint(cast(Database, None), None, context, launcher.ENDPOINT_ADDRESS) as gate:
        assert gate.url == f"http://{launcher.ENDPOINT_ADDRESS}:{gate.port}/check"
        assert status(gate.url) == 401
        assert refused("127.0.0.1", gate.port)


@LINUX
def test_an_agent_run_under_host_execution_gets_the_dedicated_address(
    kernel: Kernel, session: FakeSession
) -> None:
    """The kernel's `endpoint_host` (set by the daemon under host execution) is where the
    attempt's tool endpoint listens, and the run works through it."""
    kernel.endpoint_host = launcher.ENDPOINT_ADDRESS
    session.scripts = [routine]
    assert AgentRunRuntime(kernel).execute(submit(kernel)) == "succeeded"
    assert session.requests[0].mcp_url.startswith(f"http://{launcher.ENDPOINT_ADDRESS}:")
