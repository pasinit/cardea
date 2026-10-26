"""Per-attempt MCP endpoint: an agent session's only path to tools (ADR-027).

It listens on loopback only, requires a random per-attempt bearer token, and serves exactly
the installed operations the run exposes. Authority is not checked here: every call is
handed to the run, which re-checks the lease and live ceiling before any dispatch.

Each call is also told whether its client has given up on it: the client closed the HTTP
connection before the response, or sent ``notifications/cancelled`` for its request id. The
run uses that to withdraw a call still waiting for a worker slot (ADR-027, revised
2026-10-26). The MCP library cannot surface either signal here: in stateless JSON-response
mode it serves every POST on a fresh transport (so a cancellation, sent on its own POST,
never meets the request it names) and never reads the connection again after the body.
"""

import asyncio
import hmac
import json
import secrets
import threading
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from types import TracebackType
from typing import Any

import uvicorn
from mcp import types
from mcp.server.lowlevel import Server
from starlette.types import ASGIApp, Receive, Scope, Send

# A call: tool name, arguments, and whether its client has given up on it.
CallTool = Callable[[str, dict[str, Any], Callable[[], bool]], Awaitable[types.CallToolResult]]
# The abandonment signal of the HTTP request being served, seen by the tool handler it runs.
_GONE: ContextVar[asyncio.Event | None] = ContextVar("cardea_tool_request_gone", default=None)


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


class _Abandonment:
    """Sets a per-request event when the client gives up on a POSTed request.

    The body is read here (and replayed to the MCP app) so that this wrapper alone watches
    the connection afterwards. A `tools/call` request's id is remembered while it is served,
    so a later `notifications/cancelled` naming it sets the same event.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.pending: dict[str, asyncio.Event] = {}

    def _route(self, body: bytes, gone: asyncio.Event) -> str | None:
        try:
            message = json.loads(body)
        except ValueError:
            return None
        if not isinstance(message, dict):
            return None
        if message.get("method") == "notifications/cancelled":
            params = message.get("params")
            target = params.get("requestId") if isinstance(params, dict) else None
            cancelled = self.pending.get(str(target)) if target is not None else None
            if cancelled is not None:
                cancelled.set()
            return None
        if message.get("method") == "tools/call" and message.get("id") is not None:
            key = str(message["id"])
            self.pending[key] = gone
            return key
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        chunks = []
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        gone = asyncio.Event()
        key = self._route(body, gone)
        replayed = False

        async def replay() -> Any:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            await gone.wait()
            return {"type": "http.disconnect"}

        async def watch() -> None:
            while (await receive())["type"] != "http.disconnect":
                pass
            gone.set()

        watcher = asyncio.ensure_future(watch())
        token = _GONE.set(gone)
        try:
            await self.app(scope, replay, send)
        finally:
            _GONE.reset(token)
            watcher.cancel()
            if key is not None and self.pending.get(key) is gone:
                del self.pending[key]


def _never() -> bool:
    return False


class ToolEndpoint:
    def __init__(self, tools: list[types.Tool], call: CallTool) -> None:
        self.token = secrets.token_urlsafe(32)
        self.tools = tools
        self.call = call
        self.port = 0
        # Authenticated tools/list requests served; a harness that never lists tools never
        # connected to its tools (ADR-033).
        self.listings = 0
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
            self.listings += 1
            return types.ListToolsResult(tools=tools)

        async def call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
            if params.name not in names:
                return types.CallToolResult(
                    content=[types.TextContent(type="text", text="unknown_tool")], is_error=True
                )
            gone = _GONE.get()
            return await call(
                params.name, dict(params.arguments or {}), gone.is_set if gone else _never
            )

        server: Server[Any] = Server(
            "cardea", version="1", on_list_tools=list_tools, on_call_tool=call_tool
        )
        app = server.streamable_http_app(json_response=True, stateless_http=True)
        return _Bearer(_Abandonment(app), self.token)

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
