"""The multi-team dashboard (deploy/team-kit/dashboard_teams.py): read-only, loopback-only,
one team failing does not hide the others, and no credential reaches the page."""

import importlib.util
import json
from pathlib import Path
from typing import Any, cast

import pytest
from test_dashboard import KIT, Supervisor, fixture
from test_dashboard import call as raw_call
from test_team_status import message as plant
from test_team_status import setup, team

from cardea.service.client import ClientError
from cardea.service.kernel import Kernel

__all__ = ["setup", "team"]  # the fixtures the tests below ask for

TOKEN = "s3cr3t-token-value"


def call(
    backend: Any,
    method: str,
    path: str,
    body: Any = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, Any]:
    return raw_call(backend, method, path, body, headers, build=teams().server)


def teams() -> Any:
    spec = importlib.util.spec_from_file_location("dashboard_teams", KIT / "dashboard_teams.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def aerarium() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads((KIT / "dashboard.fixture.aerarium.json").read_text()))


def backend(**extra: Any) -> Any:
    module = teams()
    sources = {"dev": lambda: fixture(), "aerarium": lambda: aerarium(), **extra}
    return module.TeamsBackend(sources)


@pytest.mark.loopback
def test_two_teams_render_side_by_side() -> None:
    result = call(backend(), "GET", "/api/status")
    assert result[0] == 200
    names = [t["name"] for t in result[1]["teams"]]
    assert names == ["dev", "aerarium"]
    assert result[1]["teams"][0]["view"] == fixture()
    assert result[1]["teams"][1]["view"]["group"] == "aerarium-team"
    status, page = call(backend(), "GET", "/")
    assert status == 200 and '<script src="/dashboard_teams.js">' in page
    script = (KIT / "dashboard_teams.js").read_text()
    assert "innerHTML" not in script
    assert "display: flex" in (KIT / "dashboard_teams.html").read_text()


def raising(error: Exception) -> Any:
    def read() -> dict[str, Any]:
        raise error

    return read


@pytest.mark.loopback
def test_an_unreachable_team_shows_an_error_and_hides_nothing() -> None:
    down = backend(aerarium=raising(OSError(f"connect failed, Bearer {TOKEN}")))
    status, body = call(down, "GET", "/api/status")
    assert status == 200
    dev, other = body["teams"]
    assert dev["view"] == fixture() and "error" not in dev
    assert other == {"name": "aerarium", "error": "unreachable"}
    refused = backend(dev=raising(ClientError("forbidden", 403)))
    teams_ = call(refused, "GET", "/api/status")[1]["teams"]
    assert teams_[0]["error"] == "forbidden" and teams_[1]["view"] == aerarium()


@pytest.mark.loopback
def test_no_token_appears_in_any_output() -> None:
    down = backend(
        aerarium=raising(ClientError(f"Bearer {TOKEN}", 401)),
        third=raising(RuntimeError(TOKEN)),
    )
    outputs = [call(down, "GET", path)[1] for path in ("/", "/dashboard_teams.js", "/api/status")]
    assert all(TOKEN not in json.dumps(o) for o in outputs)
    assert call(down, "GET", "/api/status")[1]["teams"][1]["error"] == "unreachable"
    # The page code has no path for a credential: it reads only /api/status.
    script = (KIT / "dashboard_teams.js").read_text()
    assert script.count("fetch(") == 1 and "token" not in script.lower()
    assert "authorization" not in script.lower() and "bearer" not in script.lower()


@pytest.mark.loopback
def test_the_page_is_read_only() -> None:
    b = backend()
    for method in ("POST", "PUT", "DELETE"):
        assert call(b, method, "/api/message", {"role": "dev"})[0] in (404, 501)
    page = (KIT / "dashboard_teams.html").read_text()
    assert "<form" not in page and "<button" not in page and "<a " not in page
    assert "method:" not in (KIT / "dashboard_teams.js").read_text()
    assert call(b, "GET", "/api/status", headers={"Host": "evil.example"})[0] == 403


def test_a_non_loopback_bind_is_refused() -> None:
    module = teams()
    for bind in ("0.0.0.0", "192.168.1.5", "", "::"):
        with pytest.raises(module.DashboardError):
            module.server(backend(), 0, bind)


def test_the_cli_refuses_a_non_loopback_bind(tmp_path: Path) -> None:
    module = teams()
    one = tmp_path / "a.json"
    one.write_text(json.dumps(fixture()))
    assert module.main(["--fixtures", f"dev={one}", "--bind", "0.0.0.0"]) == 2


def test_the_teams_file_is_validated(tmp_path: Path) -> None:
    module = teams()
    path = tmp_path / "teams.json"
    path.write_text(json.dumps({"teams": [{"name": "dev", "host": "d.json"}]}))
    assert module.read_teams(path) == [("dev", tmp_path / "d.json")]
    bads: list[Any] = [
        {"teams": []},
        {"teams": [{"name": "dev", "host": "d.json", "token": TOKEN}]},
        {"teams": [{"name": "a b", "host": "d.json"}]},
        {"teams": [{"name": "dev", "host": "x"}, {"name": "dev", "host": "y"}]},
    ]
    for bad in bads:
        path.write_text(json.dumps(bad))
        with pytest.raises(module.DashboardError):
            module.read_teams(path)


# The mail view


def eng(kernel: Kernel, who: str = "sup-eng") -> Any:
    """The dashboard's teams backend over the real kernel, reading as `who`."""
    module = teams()
    read = module.base.KernelBackend(Supervisor(kernel, who).request, "eng", "eng-team")
    return module.TeamsBackend({"eng": read.overview}, {"eng": read.mail})


def mail(backend: Any, **query: str) -> tuple[int, Any]:
    return call(backend, "GET", "/api/mail?" + "&".join(f"{k}={v}" for k, v in query.items()))


MARKUP = '<script>alert(1)</script><img src=x onerror=alert(2)> <a href="javascript:x">y</a>'


@pytest.mark.loopback
def test_the_mail_view_serves_full_text_to_the_supervisor(team: tuple[Kernel, str, str]) -> None:
    kernel, _, _ = team
    plant(
        kernel,
        "00000000-0000-0000-0000-000000000009",
        "eng-team.dev.0",
        "instance",
        "eng-team.lead.0",
        "2026-11-22T11:00:00+00:00",
        MARKUP,
        {"collection": "work", "record_id": "rec-a"},
    )
    status, page = mail(eng(kernel), team="eng")
    assert status == 200 and page["total"] == 2
    # The text arrives exactly as sent: it is escaped by the page, which only uses textContent.
    assert page["items"][0]["parts"] == [{"text": MARKUP}]
    assert page["items"][1]["parts"] == [{"text": "Item a is ready."}]
    assert "ck-secret" not in json.dumps(page) and "client_key" not in json.dumps(page)


@pytest.mark.loopback
def test_the_mail_view_is_refused_to_anyone_but_the_groups_supervisor(
    team: tuple[Kernel, str, str],
) -> None:
    kernel, _, _ = team
    for who in ("sup-desk", "runtime"):
        status, body = mail(eng(kernel, who), team="eng")
        assert (status, body) == (404, {"code": "group_not_found"})
    # Authority is checked live: a supervisor disabled since the page opened is refused.
    backend = eng(kernel)
    assert mail(backend, team="eng")[0] == 200
    with kernel.database.engine.begin() as conn:
        conn.exec_driver_sql("UPDATE principals SET disabled=1 WHERE principal_id='sup-eng'")
    assert mail(backend, team="eng") == (403, {"code": "principal_disabled"})


@pytest.mark.loopback
def test_the_mail_view_pages_and_filters(team: tuple[Kernel, str, str]) -> None:
    kernel, _, _ = team
    second = "00000000-0000-0000-0000-000000000009"
    plant(
        kernel,
        second,
        "eng-team.dev.0",
        "instance",
        "eng-team.lead.0",
        "2026-11-22T11:00:00+00:00",
        "Done.",
        {"collection": "work", "record_id": "rec-a"},
    )
    backend = eng(kernel)
    first = mail(backend, team="eng", limit="1")[1]
    assert [m["message_id"] for m in first["items"]] == [second] and first["next"] == second
    rest = mail(backend, team="eng", limit="1", after=first["next"])[1]
    assert [m["parts"] for m in rest["items"]] == [[{"text": "Item a is ready."}]]
    assert rest["next"] is None
    by_member = mail(backend, team="eng", member="eng-team.dev.0")[1]
    assert [m["message_id"] for m in by_member["items"]] == [second]
    by_item = mail(backend, team="eng", item="work/rec-a")[1]
    assert by_item["total"] == 1 and mail(backend, team="eng", item="work/rec-bb")[1]["items"] == []


@pytest.mark.loopback
def test_the_mail_query_is_validated_and_the_team_must_exist(
    team: tuple[Kernel, str, str],
) -> None:
    kernel, _, _ = team
    backend = eng(kernel)
    bad = ["team=eng&limit=0", "team=eng&limit=51", "team=eng&limit=x", "team=eng&item=a%20b",
           "team=eng&member=%3Cb%3E", "team=eng&after=nope", "team=eng&extra=1", "team=eng&team=x",
           "team=eng&%"]  # fmt: skip
    for query in bad:
        status, body = call(backend, "GET", f"/api/mail?{query}")
        assert (status, body) == (400, {"code": "invalid_query"}), query
    assert call(backend, "GET", "/api/mail?team=nope") == (404, {"code": "unknown_team"})
    assert call(backend, "GET", "/api/mail") == (404, {"code": "unknown_team"})
    assert call(backend, "POST", "/api/mail", {})[0] == 404
    assert call(backend, "GET", "/api/mail?team=eng", headers={"Host": "evil.example"})[0] == 403


@pytest.mark.loopback
def test_each_team_reports_its_state() -> None:
    base = teams().base
    view = fixture()
    assert base.team_state(view) in {"running", "stalled", "idle"}

    def members(v: dict[str, Any]) -> list[dict[str, Any]]:
        return [m for pool in v["pools"] for m in pool["members"]]

    running = json.loads(json.dumps(view))
    members(running)[0]["active_task_id"] = "task-1"
    assert base.team_state(running) == "running"
    stalled = json.loads(json.dumps(view))
    for m in members(stalled):
        m["active_task_id"] = None
    stalled["mailbox"]["addresses"] = [{"address": "x", "pending": 2, "in_flight": 0, "dead": 0}]
    assert base.team_state(stalled) == "stalled"
    idle = json.loads(json.dumps(stalled))
    idle["mailbox"]["addresses"] = []
    for m in members(idle):
        m["claims"] = []
    assert base.team_state(idle) == "idle"
    for m in members(running):
        m["disabled"] = True
    assert base.team_state(running) == "paused"
    inactive = json.loads(json.dumps(view))
    inactive["state"] = "deleting"
    assert base.team_state(inactive) == "paused"
    states = {t["name"]: t["state"] for t in call(backend(), "GET", "/api/status")[1]["teams"]}
    assert set(states) == {"dev", "aerarium"} and "paused" not in states.values()
    assert (
        "state"
        not in call(backend(aerarium=raising(OSError())), "GET", "/api/status")[1]["teams"][1]
    )


def test_the_page_renders_mail_as_text_and_fits_a_phone() -> None:
    script = (KIT / "dashboard_teams.js").read_text()
    page = (KIT / "dashboard_teams.html").read_text()
    for unsafe in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "href",
    ):
        assert (
            unsafe not in script
            and unsafe not in page.replace("<style>", "", 1).split("</style>")[1]
        )
    assert "createElement" in script and "textContent" in script
    assert 'name="viewport" content="width=device-width, initial-scale=1"' in page
    assert "@media (max-width: 600px)" in page and "overflow-x: auto" in page
    assert "pre-wrap" in page and "overflow-wrap: anywhere" in page
    # Tabs show the team state, and the server sends the policy that forbids inline script.
    assert 'role", "tab"' in script and "team.state" in script
