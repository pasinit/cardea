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
import base64
import hmac
import json
import secrets
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from types import TracebackType
from typing import Any

import uvicorn
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp, Receive, Scope, Send

from cardea.service import loopback

# A call: tool name, arguments, and whether its client has given up on it.
CallTool = Callable[[str, dict[str, Any], Callable[[], bool]], Awaitable[types.CallToolResult]]
# A workspace upload (ADR-037 amendment 2026-11-24): bytes in, an object listing out. It
# raises ValueError for content it refuses.
Upload = Callable[[bytes], Awaitable[dict[str, Any]]]
UPLOAD_PATH = "/objects"
# A member's memory archive (cardea-memory save); same token, same size bound.
MEMORY_PATH = "/memory"
MAX_UPLOAD_BYTES = 8 * 1048576
# The artifacts the run's own tool calls returned, for `cardea-artifact get <call>`: a
# workspace member saves them as files (screenshots it then opens with its file tools).
ARTIFACTS_PATH = "/artifacts/"
# Reads one listed artifact's bytes (checked against its digest); raises on any failure.
ReadArtifact = Callable[[dict[str, Any]], bytes]
MAX_FETCH_BYTES = 16 * 1048576


def listed_artifacts(output: object) -> dict[str, dict[str, Any]]:
    """An operation output's artifacts by file path: env.try names its artifacts `a0`,
    `a1`, ... and maps each to its path under `scenarios[].artifacts`; an artifact without
    a mapped path keeps its name. Paths are made relative and free of `..`."""
    if not isinstance(output, dict) or not isinstance(output.get("artifacts"), list):
        return {}
    paths: dict[str, str] = {}
    for scenario in output.get("scenarios") or ():
        for entry in (scenario.get("artifacts") or ()) if isinstance(scenario, dict) else ():
            if isinstance(entry, dict) and entry.get("name") and entry.get("path"):
                paths[str(entry["name"])] = str(entry["path"])
    found: dict[str, dict[str, Any]] = {}
    for artifact in output["artifacts"]:
        if not isinstance(artifact, dict) or not artifact.get("name"):
            continue
        raw = paths.get(str(artifact["name"]), str(artifact["name"]))
        parts = [part for part in raw.replace("\\", "/").split("/") if part not in ("", ".", "..")]
        if parts:
            found["/".join(parts)] = artifact
        if len(found) >= 64:
            break
    return found


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


async def _reply(send: Send, status: int, body: dict[str, Any]) -> None:
    payload = json.dumps(body).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(payload)).encode()),
    ]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": payload})


class _Uploads:
    """`POST /objects`: a session's workspace hands a change bundle to the kernel. Every
    other request goes to the MCP app. Behind the same bearer token as the tools."""

    def __init__(
        self,
        app: ASGIApp,
        upload: Upload | None,
        memory: Upload | None = None,
        fetch: Callable[[str], dict[str, Any] | None] | None = None,
    ) -> None:
        self.app, self.routes = app, {UPLOAD_PATH: upload, MEMORY_PATH: memory}
        self.fetch = fetch

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = str(scope.get("path", ""))
        if scope["type"] == "http" and path.startswith(ARTIFACTS_PATH):
            if self.fetch is None:
                await _reply(send, 404, {"error": "no_workspace"})
            elif scope.get("method") != "GET":
                await _reply(send, 405, {"error": "method_not_allowed"})
            else:
                files = await asyncio.to_thread(self.fetch, path[len(ARTIFACTS_PATH) :])
                if files is None:
                    await _reply(send, 404, {"error": "unknown_call"})
                else:
                    await _reply(send, 200, files)
            return
        if scope["type"] != "http" or scope.get("path") not in self.routes:
            await self.app(scope, receive, send)
            return
        handler = self.routes[scope["path"]]
        if handler is None:
            await _reply(send, 404, {"error": "no_workspace"})
            return
        if scope.get("method") != "POST":
            await _reply(send, 405, {"error": "method_not_allowed"})
            return
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                await _reply(send, 413, {"error": "upload_too_large", "limit": MAX_UPLOAD_BYTES})
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        try:
            listing = await handler(b"".join(chunks))
        except ValueError as refused:
            await _reply(send, 422, {"error": "upload_refused", "detail": str(refused)})
            return
        await _reply(send, 201, listing)


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
    def __init__(
        self,
        tools: list[types.Tool],
        call: CallTool,
        host: str = loopback.DEFAULT_HOST,
        upload: Upload | None = None,
        memory: Upload | None = None,
        read_artifact: ReadArtifact | None = None,
    ) -> None:
        # The one loopback address a confined session may reach (ADR-028).
        self.host = host
        self.token = secrets.token_urlsafe(32)
        self.tools = tools
        self.call = call
        # Set only for a run with a workspace; otherwise `POST /objects` is 404.
        self.upload = upload
        self.memory = memory
        # Set only for a run with a workspace: each call's artifacts by call id (`c1`, ...).
        self.read_artifact = read_artifact
        self.fetchable: dict[str, dict[str, dict[str, Any]]] = {}
        self.port = 0
        # Authenticated tools/list requests served; a harness that never lists tools never
        # connected to its tools (ADR-033).
        self.listings = 0
        self._server: uvicorn.Server | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/mcp"

    @property
    def upload_url(self) -> str:
        return f"http://{self.host}:{self.port}{UPLOAD_PATH}"

    @property
    def memory_url(self) -> str:
        return f"http://{self.host}:{self.port}{MEMORY_PATH}"

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
            result = await call(
                params.name, dict(params.arguments or {}), gone.is_set if gone else _never
            )
            return self._offer(result)

        server: Server[Any] = Server(
            "cardea", version="1", on_list_tools=list_tools, on_call_tool=call_tool
        )
        # DNS-rebinding protection for the address the endpoint listens on: the SDK enables
        # it by itself only for 127.0.0.1 and localhost, and would refuse 127.0.11.1.
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[f"{self.host}:*"],
            allowed_origins=[f"http://{self.host}:*"],
        )
        app = server.streamable_http_app(
            json_response=True, stateless_http=True, host=self.host, transport_security=security
        )
        fetch = self._fetch if self.read_artifact is not None else None
        return _Bearer(_Uploads(_Abandonment(app), self.upload, self.memory, fetch), self.token)

    def _offer(self, result: types.CallToolResult) -> types.CallToolResult:
        """Keep a successful result's artifacts fetchable by a new call id, and say so in the
        result's text, so the session can save them as files."""
        if self.read_artifact is None or result.is_error:
            return result
        found = listed_artifacts(result.structured_content)
        if not found:
            return result
        call_id = f"c{len(self.fetchable) + 1}"
        self.fetchable[call_id] = found
        note = (
            f"Artifacts of this call: run `cardea-artifact get {call_id}` to save them as "
            f"files under ./artifacts/{call_id}/ ({', '.join(sorted(found))}), then open "
            "them with your file tools (a PNG shows as an image)."
        )
        content = [*result.content, types.TextContent(type="text", text=note)]
        return result.model_copy(update={"content": content})

    def _fetch(self, call_id: str) -> dict[str, Any] | None:
        """`GET /artifacts/<call>`: that call's artifacts, base64 by path, within a bound;
        one that cannot be read is named under `missing`. `GET /artifacts/` lists every
        call's paths, and `latest` is the newest call: a session need not see the note
        (a client may show a result's structured content instead of its text)."""
        if call_id == "":
            return {"calls": {key: sorted(found) for key, found in self.fetchable.items()}}
        if call_id == "latest" and self.fetchable:
            call_id = list(self.fetchable)[-1]
        found = self.fetchable.get(call_id)
        if found is None or self.read_artifact is None:
            return None
        files: dict[str, str] = {}
        missing: list[str] = []
        total = 0
        for path, artifact in sorted(found.items()):
            size = artifact.get("bytes")
            if not isinstance(size, int) or total + size > MAX_FETCH_BYTES:
                missing.append(path)
                continue
            try:
                content = self.read_artifact(artifact)
            except Exception:  # an unreadable artifact is reported, never raised
                missing.append(path)
                continue
            total += len(content)
            files[path] = base64.b64encode(content).decode()
        return {"call": call_id, "files": files, "missing": missing}

    def __enter__(self) -> "ToolEndpoint":
        self._server, self.port = loopback.start(
            self._app(), self.host, "cardea-tool-endpoint", lifespan="on"
        )
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        loopback.stop(self._server)
