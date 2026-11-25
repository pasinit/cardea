"""A read-only team dashboard with a supervisor message box (ADR-034, ADR-039 follows).

    python3 -I deploy/team-kit/dashboard.py --fixture deploy/team-kit/dashboard.fixture.json
    python3 -I deploy/team-kit/teamkit.py dashboard --host HOST.json [--port 8765]

One page (`dashboard.html`, `dashboard.js`; plain browser JavaScript, no build step) shows the
group overview, `GET /v0.2/groups/{name}/overview`, as the supervisor sees it. The only write
is the message box: it sends one text message to a role's pool address, `<group>.<role>`.

This server holds no authority of its own. It calls the kernel with the supervisor's own
credential (the same one `teamkit.py` uses), so the kernel's rules decide what is read and
whether the supervisor may mail the role (`mail:<group>.<role>`). It listens on the loopback
interface only, and refuses a request whose Host or Origin is not that address.

A message carries the page's client key. The kernel returns the message already sent for a
repeated key, so a double submit, a retry or a second tab with the same form delivers once.
"""

import argparse
import json
import re
import sys
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent
LOOPBACK = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_MESSAGE_CHARS = 4000
MAX_BODY_BYTES = 16384
MAX_REPLIES = 20
MAIL_PAGE = 50
MAX_MAIL_PAGES = 20
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
TEAM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
ITEM = re.compile(r"^[a-z][a-z0-9_-]{0,63}/[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
CURSOR = re.compile(r"^[0-9a-f-]{36}$")
MAIL_FILTERS = {"team", "member", "item", "after", "limit"}
MAIL_LIMIT = 20
DETAIL_FILTERS = {"team", "member", "task", "after", "limit"}
RUNS_PAGE = 20
STATIC = {
    "/": ("dashboard.html", "text/html; charset=utf-8"),
    "/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8"),
}
MEMBER_STATIC = {
    "/member": ("dashboard_member.html", "text/html; charset=utf-8"),
    "/dashboard_member.js": ("dashboard_member.js", "text/javascript; charset=utf-8"),
}
# The page loads its one script from this server and talks only to this server.
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


class DashboardError(Exception):
    def __init__(self, code: str, status: int = 400) -> None:
        super().__init__(code)
        self.code, self.status = code, status


class Backend(Protocol):
    def overview(self) -> dict[str, Any]: ...

    def replies(self) -> list[dict[str, Any]]: ...

    def send(self, role: str, text: str, client_key: str) -> dict[str, Any]: ...

    def mail(self, query: dict[str, Any]) -> dict[str, Any]: ...


def mail_query(raw: str) -> dict[str, Any]:
    """A mail request's filters, checked: `team`, `member`, `item`, `after` and `limit` only."""
    try:
        found = parse_qs(raw, keep_blank_values=True, strict_parsing=bool(raw))
    except ValueError:
        raise DashboardError("invalid_query") from None
    if set(found) - MAIL_FILTERS or any(len(v) != 1 for v in found.values()):
        raise DashboardError("invalid_query")
    query: dict[str, Any] = {k: v[0] for k, v in found.items() if v[0]}
    for key, pattern in (("team", TEAM), ("member", IDENTIFIER), ("item", ITEM), ("after", CURSOR)):
        if key in query and not pattern.match(query[key]):
            raise DashboardError("invalid_query")
    limit = query.get("limit", str(MAIL_LIMIT))
    if not (limit.isascii() and limit.isdigit() and 1 <= int(limit) <= 50):
        raise DashboardError("invalid_query")
    query["limit"] = int(limit)
    return query


def detail_query(raw: str) -> dict[str, Any]:
    """A member-page request's filters, checked: `team`, `member`, `task`, `after`, `limit`."""
    try:
        found = parse_qs(raw, keep_blank_values=True, strict_parsing=bool(raw))
    except ValueError:
        raise DashboardError("invalid_query") from None
    if set(found) - DETAIL_FILTERS or any(len(v) != 1 for v in found.values()):
        raise DashboardError("invalid_query")
    query: dict[str, Any] = {k: v[0] for k, v in found.items() if v[0]}
    for key, pattern in (("team", TEAM), ("member", IDENTIFIER), ("task", IDENTIFIER)):
        if key in query and not pattern.match(query[key]):
            raise DashboardError("invalid_query")
    if "after" in query and not IDENTIFIER.match(query["after"]):
        raise DashboardError("invalid_query")
    limit = query.get("limit", str(RUNS_PAGE))
    if not (limit.isascii() and limit.isdigit() and 1 <= int(limit) <= 50):
        raise DashboardError("invalid_query")
    query["limit"] = int(limit)
    return query


def team_state(view: dict[str, Any]) -> str:
    """`paused`, `running`, `stalled` or `idle`, from one overview.

    Paused: the group is not active, or every member is disabled. Running: a member has a run
    in progress. Stalled: nothing runs, yet mail waits, mail was dead-lettered or a claim is
    held. Otherwise idle.
    """
    members_ = [m for pool in view.get("pools", []) for m in pool["members"]]
    if view.get("state") != "active" or (members_ and all(m["disabled"] for m in members_)):
        return "paused"
    if any(m["active_task_id"] for m in members_):
        return "running"
    mail = view.get("mailbox", {}).get("addresses", [])
    if any(a["pending"] or a["dead"] for a in mail) or any(m["claims"] for m in members_):
        return "stalled"
    return "idle"


def roles(view: dict[str, Any]) -> list[str]:
    return [str(pool["name"]) for pool in view.get("pools", [])]


def members_of(view: dict[str, Any]) -> list[dict[str, Any]]:
    return [m for pool in view.get("pools", []) for m in pool["members"]]


def members(view: dict[str, Any]) -> set[str]:
    return {str(m["principal_id"]) for pool in view.get("pools", []) for m in pool["members"]}


class FixtureBackend:
    """Serves a recorded overview; a message is recorded here and goes nowhere."""

    def __init__(self, view: dict[str, Any], replies: list[dict[str, Any]] | None = None) -> None:
        self.view = view
        self.recorded = replies or []
        self.sent: dict[str, dict[str, Any]] = {}

    def overview(self) -> dict[str, Any]:
        return self.view

    def replies(self) -> list[dict[str, Any]]:
        return self.recorded[:MAX_REPLIES]

    def send(self, role: str, text: str, client_key: str) -> dict[str, Any]:
        group = self.view["group"]
        message = {"role": role, "text": text}
        if self.sent.setdefault(client_key, message) != message:
            raise DashboardError("request_conflict", 409)
        return {"message_id": f"fixture-{len(self.sent)}", "to": {"pool": f"{group}.{role}"}}

    def mail(self, query: dict[str, Any]) -> dict[str, Any]:
        return {"total": 0, "items": [], "next": None}


class KernelBackend:
    """The kernel, as the supervisor: `request` is `KernelClient.request`."""

    def __init__(self, request: Callable[..., Any], namespace: str, group: str) -> None:
        self.request, self.namespace, self.group = request, namespace, group

    def overview(self) -> dict[str, Any]:
        view: dict[str, Any] = self.request(
            "GET", f"/groups/{self.group}/overview", params={"namespace": self.namespace}
        )
        return view

    def replies(self) -> list[dict[str, Any]]:
        """The newest messages a group member sent to an instance, as the supervisor sees them."""
        group = members(self.overview())
        found: list[dict[str, Any]] = []
        after = ""
        for _ in range(MAX_MAIL_PAGES):
            page: dict[str, Any] = self.request(
                "GET",
                "/mailbox",
                params={"namespace": self.namespace, "after": after, "limit": MAIL_PAGE},
            )
            found += [
                m for m in page["messages"] if m["sender_id"] in group and "instance" in m["to"]
            ]
            after = page.get("next") or ""
            if not after:
                break
        return found[-MAX_REPLIES:][::-1]

    def mail(self, query: dict[str, Any]) -> dict[str, Any]:
        """One page of the team's mail with full text; the kernel refuses a non-supervisor."""
        params = {k: v for k, v in query.items() if k in MAIL_FILTERS - {"team"}}
        page: dict[str, Any] = self.request(
            "GET",
            f"/groups/{self.group}/status/mail/text",
            params={"namespace": self.namespace, **params},
        )
        return page

    def member_detail(self, member: str) -> dict[str, Any]:
        """One member as the supervisor sees it: card, grants, memory and its recent runs.

        Every part is a read the kernel checks live for the supervisor (`groups.manage`).
        """
        found = next((m for m in members_of(self.overview()) if m["principal_id"] == member), None)
        if found is None:
            raise DashboardError("member_not_found", 404)
        detail: dict[str, Any] = self.request(
            "GET",
            f"/groups/{self.group}/status/members/{member}",
            params={"namespace": self.namespace},
        )
        runs = self.runs(member, "", RUNS_PAGE)
        return {
            "member": member,
            "pool": found["pool"],
            "state": found["state"],
            "disabled": found["disabled"],
            "active_task_id": found["active_task_id"],
            "claims": found["claims"],
            "capabilities": detail["capabilities"],
            "capabilities_truncated": detail["capabilities_truncated"],
            "memory": detail["memory"],
            "runs": runs["items"],
            "runs_next": runs["next"],
        }

    def runs(self, member: str, after: str, limit: int) -> dict[str, Any]:
        page: dict[str, Any] = self.request(
            "GET",
            f"/groups/{self.group}/status/runs",
            params={"namespace": self.namespace, "member": member, "after": after, "limit": limit},
        )
        return page

    def calls(self, task_id: str, after: str, limit: int) -> dict[str, Any]:
        """One run's receipted tool calls, newest first (the kernel checks the run)."""
        page: dict[str, Any] = self.request(
            "GET",
            f"/groups/{self.group}/status/calls",
            params={
                "namespace": self.namespace,
                "task_id": task_id,
                "after": after,
                "limit": limit,
            },
        )
        return page

    def send(self, role: str, text: str, client_key: str) -> dict[str, Any]:
        return self.send_to({"pool": f"{self.group}.{role}"}, text, client_key)

    def send_to(self, to: dict[str, str], text: str, client_key: str) -> dict[str, Any]:
        """One text message to an address: a pool, `{"pool": "<group>.<role>"}`, or one member,
        `{"instance": "<principal id>"}`. The kernel checks the supervisor's `mail:` grant."""
        sent: dict[str, Any] = self.request(
            "POST",
            "/mailbox",
            json={
                "namespace": self.namespace,
                "client_key": client_key,
                "to": to,
                "parts": [{"text": text}],
            },
        )
        return sent


def send_message(backend: Backend, body: object) -> dict[str, Any]:
    """Validate one message-box submission and hand it to the backend."""
    if not isinstance(body, dict) or set(body) != {"role", "text", "client_key"}:
        raise DashboardError("invalid_message")
    role, text, key = body["role"], body["text"], body["client_key"]
    if not (isinstance(role, str) and isinstance(text, str) and isinstance(key, str)):
        raise DashboardError("invalid_message")
    if not IDENTIFIER.match(key):
        raise DashboardError("invalid_client_key")
    if not text.strip() or len(text) > MAX_MESSAGE_CHARS:
        raise DashboardError("invalid_text")
    if role not in roles(backend.overview()):
        raise DashboardError("unknown_role", 404)
    return backend.send(role, text, key)


def handler(
    backend: Backend,
    port: int,
    static: dict[str, tuple[str, str]] = STATIC,
    writable: bool = True,
) -> type[BaseHTTPRequestHandler]:
    hosts = {f"{LOOPBACK}:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "cardea-dashboard"

        def reply(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def json(self, status: int, document: object) -> None:
            self.reply(status, json.dumps(document).encode(), "application/json")

        def local(self) -> bool:
            """Refuse a rebound DNS name and a page from another origin."""
            origin = self.headers.get("Origin")
            return self.headers.get("Host") in hosts and (
                origin is None or origin.removeprefix("http://") in hosts
            )

        def guarded(self, action: Callable[[], None]) -> None:
            if not self.local():
                return self.json(HTTPStatus.FORBIDDEN, {"code": "forbidden_origin"})
            try:
                action()
            except DashboardError as error:
                self.json(error.status, {"code": error.code})
            except Exception as error:  # the kernel's refusal, or it being unreachable
                status = getattr(error, "status", 502)
                code = str(getattr(error, "code", None) or "kernel_unavailable")
                self.json(status if 400 <= status < 600 else 502, {"code": code})

        def do_GET(self) -> None:
            def get() -> None:
                url = urlsplit(self.path)
                if url.path == "/api/mail":
                    return self.json(HTTPStatus.OK, backend.mail(mail_query(url.query)))
                if url.path in ("/api/member", "/api/calls") and hasattr(backend, "detail"):
                    found = cast(Any, backend).detail(url.path[5:], detail_query(url.query))
                    return self.json(HTTPStatus.OK, found)
                if url.path == "/api/settings" and hasattr(backend, "settings_current"):
                    current = cast(Any, backend).settings_current(mail_query(url.query))
                    return self.json(HTTPStatus.OK, current)
                if self.path in static:
                    name, kind = static[self.path]
                    return self.reply(HTTPStatus.OK, (HERE / name).read_bytes(), kind)
                if self.path == "/api/status":
                    return self.json(HTTPStatus.OK, backend.overview())
                if self.path == "/api/replies":
                    return self.json(HTTPStatus.OK, backend.replies())
                self.json(HTTPStatus.NOT_FOUND, {"code": "not_found"})

            self.guarded(get)

        def do_POST(self) -> None:
            def post() -> None:
                step = self.path.removeprefix("/api/settings/")
                if step in ("preview", "apply") and hasattr(backend, "settings_change"):
                    pass
                elif self.path != "/api/message" or not writable:
                    return self.json(HTTPStatus.NOT_FOUND, {"code": "not_found"})
                # A cross-site form cannot send JSON, and a script cannot do so unpreflighted.
                if self.headers.get_content_type() != "application/json":
                    raise DashboardError("unsupported_media_type", 415)
                size = int(self.headers.get("Content-Length") or -1)
                if not 0 <= size <= MAX_BODY_BYTES:
                    raise DashboardError("invalid_message", 413)
                try:
                    body = json.loads(self.rfile.read(size))
                except ValueError:
                    raise DashboardError("invalid_message") from None
                if step in ("preview", "apply"):
                    return self.json(HTTPStatus.OK, cast(Any, backend).settings_change(step, body))
                self.json(HTTPStatus.CREATED, send_message(backend, body))

            self.guarded(post)

        def log_message(self, format: str, *args: Any) -> None:
            sys.stderr.write(f"dashboard: {format % args}\n")

    return Handler


def server(
    backend: Backend,
    port: int = DEFAULT_PORT,
    bind: str = LOOPBACK,
    static: dict[str, tuple[str, str]] = STATIC,
    writable: bool = True,
) -> ThreadingHTTPServer:
    """A server on the loopback interface; `port` 0 picks a free port."""
    if bind != LOOPBACK:
        raise DashboardError("bind_not_loopback")
    httpd = ThreadingHTTPServer((bind, port), BaseHTTPRequestHandler)
    httpd.RequestHandlerClass = handler(backend, httpd.server_port, static, writable)
    return httpd


def serve(backend: Backend, port: int = DEFAULT_PORT, **options: Any) -> None:
    httpd = server(backend, port, **options)
    print(f"dashboard on http://{LOOPBACK}:{httpd.server_port}/", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fixture", type=Path, required=True, help="a recorded overview")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    serve(FixtureBackend(json.loads(args.fixture.read_text())), args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
