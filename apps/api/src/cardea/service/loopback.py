"""Per-call loopback endpoints that confined workers reach (ADR-028, ADR-037).

A confined worker may connect to exactly one loopback address: the agent session's
per-attempt tool endpoint and the content gate's callback listen there, and the unit's
`IPAddressAllow` names only that /32 (`cardea.hosts.launcher.ENDPOINT_ADDRESS`). Nothing
else on the host's loopback, such as the kernel API, a metrics port or an operator's own
services on 127.0.0.1, is reachable from the worker.

An endpoint binds only its address and refuses any request that arrived on another one,
so a listener bound more widely by mistake still answers nobody else.

Without host execution there is no confined worker to fence, and the endpoints keep
127.0.0.1 (`DEFAULT_HOST`), which every platform has.
"""

import threading
import time
from typing import Any

import uvicorn
from starlette.types import ASGIApp, Receive, Scope, Send

DEFAULT_HOST = "127.0.0.1"


class OnlyOn:
    """Refuse HTTP and WebSocket requests that did not arrive on `host`."""

    def __init__(self, app: ASGIApp, host: str) -> None:
        self.app, self.host = app, host

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in {"http", "websocket"}:
            server = scope.get("server")
            if not server or server[0] != self.host:
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                    return
                await send(
                    {
                        "type": "http.response.start",
                        "status": 403,
                        "headers": [(b"content-type", b"application/json")],
                    }
                )
                await send({"type": "http.response.body", "body": b'{"error":"wrong_address"}'})
                return
        await self.app(scope, receive, send)


def start(app: ASGIApp, host: str, name: str, **options: Any) -> tuple[uvicorn.Server, int]:
    """Serve `app` on `host` only, on a free port; returns the server and the port."""
    config = uvicorn.Config(OnlyOn(app, host), host=host, port=0, log_level="error", **options)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name=name, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError(f"{name} failed to start on {host}")
        time.sleep(0.01)
    socket = server.servers[0].sockets[0]
    bound = socket.getsockname()
    if bound[0] != host:
        server.should_exit = True
        raise RuntimeError(f"{name} bound {bound[0]}, not {host}")
    server_thread[server] = thread
    return server, int(bound[1])


# The thread serving each started endpoint, so `stop` can wait for it.
server_thread: dict[uvicorn.Server, threading.Thread] = {}


def stop(server: uvicorn.Server | None) -> None:
    if server is None:
        return
    server.should_exit = True
    thread = server_thread.pop(server, None)
    if thread is not None:
        thread.join(timeout=10)
