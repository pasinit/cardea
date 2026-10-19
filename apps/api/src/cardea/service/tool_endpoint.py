"""Per-attempt MCP endpoint: an agent session's only path to tools (ADR-027).

It listens on loopback only, requires a random per-attempt bearer token, and serves exactly
the installed operations the run exposes. Authority is not checked here: every call is
handed to the run, which re-checks the lease and live ceiling before any dispatch.
"""

import hmac
import secrets
import threading
import time
from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import Any

import uvicorn
from mcp import types
from mcp.server.lowlevel import Server
from starlette.types import ASGIApp, Receive, Scope, Send

CallTool = Callable[[str, dict[str, Any]], Awaitable[types.CallToolResult]]


def tool_name(operation: str) -> str:
    """Model APIs reject dots in tool names; operation names never contain hyphens."""
    return operation.replace(".", "-")


def operation_name(tool: str) -> str:
    return tool.replace("-", ".")


class _Bearer:
    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app, self.expected = app, f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            supplied = dict(scope.get("headers", [])).get(b"authorization", b"")
            if not hmac.compare_digest(supplied, self.expected):
                await send({"type": "http.response.start", "status": 401, "headers": []})
                await send({"type": "http.response.body", "body": b""})
                return
        await self.app(scope, receive, send)


class ToolEndpoint:
    def __init__(self, tools: list[types.Tool], call: CallTool) -> None:
        self.token = secrets.token_urlsafe(32)
        self.tools = tools
        self.call = call
        self.port = 0
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    def _app(self) -> ASGIApp:
        tools = self.tools
        names = {tool.name for tool in tools}
        call = self.call

        async def list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
            return types.ListToolsResult(tools=tools)

        async def call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
            if params.name not in names:
                return types.CallToolResult(
                    content=[types.TextContent(type="text", text="unknown_tool")], is_error=True
                )
            return await call(params.name, dict(params.arguments or {}))

        server: Server[Any] = Server(
            "cardea", version="1", on_list_tools=list_tools, on_call_tool=call_tool
        )
        app = server.streamable_http_app(json_response=True, stateless_http=True)
        return _Bearer(app, self.token)

    def __enter__(self) -> "ToolEndpoint":
        config = uvicorn.Config(
            self._app(), host="127.0.0.1", port=0, log_level="error", lifespan="on"
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(
            target=self._server.run, name="cardea-tool-endpoint", daemon=True
        )
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started:
            if time.monotonic() > deadline or not self._thread.is_alive():
                raise RuntimeError("tool endpoint failed to start")
            time.sleep(0.01)
        self.port = self._server.servers[0].sockets[0].getsockname()[1]
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=10)
