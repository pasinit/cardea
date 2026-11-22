"""The team dashboard (deploy/team-kit/dashboard.py): a read-only page of the group overview
and a message box that mails a role once, as the supervisor, however often it is submitted."""

import importlib.util
import io
import json
import re
from pathlib import Path
from types import ModuleType
from typing import Any, cast

from fastapi.testclient import TestClient
from test_mailboxes import kernel, session, team
from test_team_kit import example, kit

from cardea.service import mailbox
from cardea.service.app import create_app
from cardea.service.client import ClientError
from cardea.service.database import borrow
from cardea.service.kernel import Kernel
from cardea.service.models import now
from cardea.service.responses import GroupOverview

__all__ = ["kernel", "session"]  # the fixtures the tests below ask for

KIT = Path(__file__).resolve().parents[4] / "deploy" / "team-kit"
SECTIONS = ("members", "collections", "mailbox", "approvals", "spend", "journal")


def dashboard() -> ModuleType:
    spec = importlib.util.spec_from_file_location("dashboard", KIT / "dashboard.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads((KIT / "dashboard.fixture.json").read_text()))


class Supervisor:
    """`KernelClient.request` over the in-process app, as a principal."""

    def __init__(self, kernel: Kernel, principal: str) -> None:
        self.http, self.principal = TestClient(create_app(kernel)), principal

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self.http.request(
            method,
            "/v0.2" + path,
            headers={"Authorization": f"Bearer {self.principal}", "X-Cardea-Auth": "spiffe"},
            **kwargs,
        )
        if response.status_code >= 400:
            raise ClientError(str(response.json().get("code")), response.status_code)
        return response.json()


class Connection:
    """A request as raw bytes in and the raw response out: the handler without a socket."""

    def __init__(self, request: bytes) -> None:
        self.request, self.response = io.BytesIO(request), io.BytesIO()

    def makefile(self, mode: str, buffering: int = -1) -> io.BytesIO:
        return self.request

    def sendall(self, data: bytes) -> None:
        self.response.write(data)

    def close(self) -> None:
        pass


def call(
    backend: Any,
    method: str,
    path: str,
    body: Any = None,
    headers: dict[str, str] | None = None,
    build: Any = None,
) -> tuple[int, Any]:
    httpd = (build or dashboard().server)(backend, 0)
    port = httpd.server_port
    data = b"" if body is None else json.dumps(body).encode()
    sent = {"Host": f"127.0.0.1:{port}", "Content-Length": str(len(data))}
    if body is not None:
        sent["Content-Type"] = "application/json"
    sent.update(headers or {})
    head = "".join(f"{k}: {v}\r\n" for k, v in sent.items())
    connection = Connection(f"{method} {path} HTTP/1.1\r\n{head}\r\n".encode() + data)
    try:
        httpd.RequestHandlerClass(connection, ("127.0.0.1", 1), httpd)
    finally:
        httpd.server_close()
    raw = connection.response.getvalue()
    top, _, content = raw.partition(b"\r\n\r\n")
    status = int(top.split(b" ", 2)[1])
    kind = b"application/json" in top
    return status, json.loads(content) if kind else content.decode()


def message(
    role: str = "dev", text: str = "Please look at rec-1.", key: str = "k-1"
) -> dict[str, str]:
    return {"role": role, "text": text, "client_key": key}


def test_the_fixture_is_a_valid_overview() -> None:
    GroupOverview.model_validate(fixture())


def test_the_page_has_a_section_for_every_part_of_the_overview() -> None:
    page, script = (KIT / "dashboard.html").read_text(), (KIT / "dashboard.js").read_text()
    for name in SECTIONS:
        assert f'<section id="{name}">' in page
        assert f'fill("{name}"' in script
    # Each key of the schema's body is rendered by the script, which never uses innerHTML.
    for key in ("pools", "collections", "mailbox", "approvals", "spend", "journal"):
        assert f"view.{key}" in script
    assert "innerHTML" not in script and "insertAdjacentHTML" not in script


def test_the_page_serves_the_fixture_and_nothing_else_writes() -> None:
    backend = dashboard().FixtureBackend(fixture())
    status, page = call(backend, "GET", "/")
    assert status == 200 and '<script src="/dashboard.js">' in page
    assert call(backend, "GET", "/dashboard.js")[0] == 200
    assert call(backend, "GET", "/api/status") == (200, fixture())
    for method in ("PUT", "DELETE", "PATCH"):
        assert call(backend, method, "/api/message", message())[0] == 501
    assert call(backend, "POST", "/api/status", message())[0] == 404
    assert call(backend, "GET", "/api/message")[0] == 404
    assert backend.sent == {}
    # The only form on the page is the message box, and it is the only control that sends.
    page = (KIT / "dashboard.html").read_text()
    assert page.count("<form") == 1 and page.count("<button") == 1
    script = (KIT / "dashboard.js").read_text()
    assert script.count("method:") == 1 and 'method: "POST"' in script


def test_requests_from_another_host_or_origin_are_refused() -> None:
    backend = dashboard().FixtureBackend(fixture())
    assert call(backend, "GET", "/api/status", headers={"Host": "evil.example"})[0] == 403
    other = {"Origin": "http://evil.example"}
    assert call(backend, "POST", "/api/message", message(), headers=other)[0] == 403
    assert backend.sent == {}
    # The page's own origin is accepted.
    port = dashboard().server(backend, 0)
    own = {"Origin": f"http://127.0.0.1:{port.server_port}"}
    port.server_close()
    assert call(backend, "GET", "/api/status", headers=own)[0] == 403  # another server's port
    assert call(backend, "GET", "/api/status")[0] == 200


def test_a_bad_message_is_refused_before_the_kernel() -> None:
    backend = dashboard().FixtureBackend(fixture())
    bad = [
        (message(role="nobody"), 404),
        (message(text="  "), 400),
        (message(text="x" * 4001), 400),
        (message(key="has space"), 400),
        ({**message(), "to": "all"}, 400),
        ([message()], 400),
    ]
    for body, status in bad:
        assert call(backend, "POST", "/api/message", body)[0] == status, body
    plain = {"Content-Type": "text/plain"}
    assert call(backend, "POST", "/api/message", message(), headers=plain)[0] == 415
    assert backend.sent == {}


def test_a_double_submit_delivers_to_the_role_mailbox_once(kernel: Kernel) -> None:
    team(kernel)
    backend = dashboard().KernelBackend(Supervisor(kernel, "supervisor").request, "test", "team")

    def deliveries() -> list[Any]:
        with kernel.database.read() as conn:
            return list(
                conn.exec_driver_sql(
                    "SELECT m.message_id,m.sender_id,m.supervisor_id,d.state FROM "
                    "mailbox_messages m JOIN mailbox_deliveries d USING(message_id) "
                    "WHERE m.client_key=?",
                    ("dash-1",),
                )
            )

    first = call(backend, "POST", "/api/message", message("dev", key="dash-1"))
    again = call(backend, "POST", "/api/message", message("dev", key="dash-1"))
    assert first[0] == again[0] == 201, (first, again)
    assert again[1]["message_id"] == first[1]["message_id"]
    assert first[1]["to"] == {"pool": "team.dev"}
    (row,) = deliveries()
    # Authenticated as the supervisor, and waiting in the role's mailbox.
    assert (row[1], row[2], row[3]) == ("supervisor", "supervisor", "pending")
    # The same key with another body is a conflict, not a second message.
    other = call(backend, "POST", "/api/message", message("dev", "Else", key="dash-1"))
    assert (other[0], other[1]["code"]) == (409, "request_conflict")
    assert len(deliveries()) == 1
    # The overview shows it waiting.
    _, view = call(backend, "GET", "/api/status")
    GroupOverview.model_validate(view)
    waiting = {a["address"]: a["pending"] for a in view["mailbox"]["addresses"]}
    assert waiting["team.dev"] == 1


def test_the_kernel_decides_whether_the_caller_may_mail_a_role(kernel: Kernel) -> None:
    team(kernel)
    # Someone who is not the group's supervisor is refused by the kernel; nothing is stored.
    backend = dashboard().KernelBackend(Supervisor(kernel, "bob").request, "test", "team")
    status, body = call(backend, "POST", "/api/message", message("dev", key="dash-2"))
    assert status >= 400 and "code" in body
    with kernel.database.read() as conn:
        count = conn.exec_driver_sql(
            "SELECT count(*) FROM mailbox_messages WHERE client_key='dash-2'"
        ).scalar()
    assert count == 0


def test_replies_to_the_supervisor_are_the_newest_from_group_members(kernel: Kernel) -> None:
    team(kernel)
    backend = dashboard().KernelBackend(Supervisor(kernel, "supervisor").request, "test", "team")
    member = sorted(dashboard().members(backend.overview()))[0]
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        for sender, to, text in [
            *[(member, {"instance": "supervisor"}, f"reply {n}") for n in range(23)],
            (member, {"pool": "team.dev"}, "to the pool"),
            ("supervisor", {"instance": member}, "from the supervisor"),
        ]:
            mailbox.send(
                conn,
                tx,
                namespace="test",
                sender_id=sender,
                supervisor_id="supervisor",
                to=mailbox.Address(**to),
                parts=[{"text": text}],
                runtime_id="runtime",
                at=now(),
                from_kernel=True,  # lets the test address the supervisor, as a notice may
            )
    status, replies = call(backend, "GET", "/api/replies")
    assert status == 200 and len(replies) == 20
    texts = [m["parts"][0]["text"] for m in replies]
    assert texts == [f"reply {n}" for n in range(22, 2, -1)]
    assert {m["sender_id"] for m in replies} == {member}
    assert call(dashboard().FixtureBackend(fixture()), "GET", "/api/replies") == (200, [])


def test_the_team_supervisor_may_mail_every_role() -> None:
    module = kit()
    host = example()
    rights = {p["operation"] for p in module.provisioning_fragment(host)["permissions"]}
    group = host.get("group", "dev-team")
    assert {f"mail:{group}.{pool}" for pool in module.POOLS} <= rights
    assert re.fullmatch(r"[a-z0-9.-]+", group)
