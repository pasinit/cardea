"""A scripted OpenAI-compatible chat-completions server for harness tests.

No real provider is ever called: a harness under test is pointed at this server's base URL.
Each request takes the next scripted reply (a tool call or a final text) and is recorded,
so a test can assert what the harness sent, for example which tools it offered the model.
"""

import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from typing import Any


@dataclass
class Reply:
    text: str = ""
    # (tool name, arguments) the model "calls"; empty for a final text reply.
    tool_calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    prompt_tokens: int = 1000
    completion_tokens: int = 100
    # Seconds to wait before answering, to exercise timeouts.
    delay: float = 0.0


class FakeModel:
    def __init__(self, replies: list[Reply] | None = None, *, repeat: bool = False) -> None:
        self.replies = list(replies or [])
        # Keep answering with the last reply instead of running out.
        self.repeat = repeat
        self.requests: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def base_url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def _next(self, body: dict[str, Any], headers: dict[str, str]) -> Reply:
        with self._lock:
            self.requests.append(body)
            self.headers.append(headers)
            if self.replies:
                return (
                    self.replies.pop(0)
                    if len(self.replies) > 1 or not self.repeat
                    else self.replies[0]
                )
            return Reply(text="Nothing left to do.")

    def __enter__(self) -> "FakeModel":
        model = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_GET(self) -> None:
                self._json({"object": "list", "data": [{"id": "fake", "object": "model"}]})

            def do_POST(self) -> None:
                length = int(self.headers.get("content-length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                if not self.path.endswith("/chat/completions"):
                    self.send_response(404)
                    self.send_header("content-length", "0")
                    self.end_headers()
                    return
                reply = model._next(body, {k.lower(): v for k, v in self.headers.items()})
                if reply.delay:
                    time.sleep(reply.delay)
                if body.get("stream"):
                    self._stream(body, reply)
                else:
                    self._json(_completion(body, reply))

            def _json(self, document: dict[str, Any]) -> None:
                data = json.dumps(document).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _stream(self, body: dict[str, Any], reply: Reply) -> None:
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("cache-control", "no-cache")
                self.send_header("connection", "close")
                self.end_headers()
                for chunk in _chunks(body, reply):
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                self.close_connection = True

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


def _usage(reply: Reply) -> dict[str, int]:
    return {
        "prompt_tokens": reply.prompt_tokens,
        "completion_tokens": reply.completion_tokens,
        "total_tokens": reply.prompt_tokens + reply.completion_tokens,
    }


def _calls(reply: Reply) -> list[dict[str, Any]]:
    return [
        {
            "id": f"call_{index}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }
        for index, (name, arguments) in enumerate(reply.tool_calls)
    ]


def _completion(body: dict[str, Any], reply: Reply) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": reply.text or None}
    if reply.tool_calls:
        message["tool_calls"] = _calls(reply)
    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "created": 0,
        "model": body.get("model", "fake"),
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if reply.tool_calls else "stop",
            }
        ],
        "usage": _usage(reply),
    }


def _chunks(body: dict[str, Any], reply: Reply) -> list[dict[str, Any]]:
    base = {"id": "chatcmpl-fake", "object": "chat.completion.chunk", "created": 0}
    base["model"] = body.get("model", "fake")
    chunks: list[dict[str, Any]] = [
        {**base, "choices": [{"index": 0, "delta": {"role": "assistant"}}]}
    ]
    if reply.text:
        chunks.append({**base, "choices": [{"index": 0, "delta": {"content": reply.text}}]})
    for index, call in enumerate(_calls(reply)):
        chunks.append(
            {
                **base,
                "choices": [{"index": 0, "delta": {"tool_calls": [{"index": index, **call}]}}],
            }
        )
    chunks.append(
        {
            **base,
            "choices": [
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": "tool_calls" if reply.tool_calls else "stop",
                }
            ],
        }
    )
    chunks.append({**base, "choices": [], "usage": _usage(reply)})
    return chunks
