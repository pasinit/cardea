"""The dashboard's settings page (deploy/team-kit/dashboard_teams.py, dashboard_settings.*):
every setting can be previewed and confirmed through the page's API, cancel and a declined
confirmation change nothing, only the team's supervisor gets through (the kernel decides, live),
and no secret reaches the page, the API or the audit."""

import json
from pathlib import Path
from typing import Any

import pytest
from test_dashboard import KIT, Supervisor
from test_dashboard_teams import call, teams
from test_team_settings import SETTING_FILES, fixture, secret
from test_team_settings import host_file as host_file  # noqa: F401  (fixture)
from test_team_settings_apply import Env, env  # noqa: F401  (fixtures)
from test_team_status import setup, team

from cardea.service.kernel import Kernel

__all__ = ["env", "setup", "team"]  # the fixtures the tests below ask for

FLOWS = Path(__file__).parent / "data" / "dashboard_settings"


def flow(name: str) -> dict[str, Any]:
    document: dict[str, Any] = json.loads((FLOWS / f"{name}.json").read_text())
    return document


def page_backend(env: Env, who: Any = None) -> Any:
    """The dashboard backend for the fixture host file, reading as the team's supervisor unless
    `who` (a source function) says otherwise."""
    read = who or (lambda: {"group": "dev-team"})
    return teams().TeamsBackend({"dev": read}, hosts={"dev": env.path}, connect=env.connect)


def overview(kernel: Kernel, who: str) -> Any:
    """The team's overview read from the real kernel as `who`: the page's authority check."""
    return teams().base.KernelBackend(Supervisor(kernel, who).request, "eng", "eng-team").overview


def preview(backend: Any, settings: object) -> tuple[int, Any]:
    return call(backend, "POST", "/api/settings/preview", {"team": "dev", "settings": settings})


def apply(backend: Any, settings: object, confirm: object) -> tuple[int, Any]:
    body = {"team": "dev", "settings": settings, "confirm": confirm}
    return call(backend, "POST", "/api/settings/apply", body)


@pytest.mark.loopback
@pytest.mark.parametrize("name", SETTING_FILES)
def test_each_setting_is_previewed_then_confirmed(env: Env, name: str) -> None:  # noqa: F811
    settings = fixture(name)["valid"]
    backend = page_backend(env)
    before = env.path.read_bytes()
    status, shown = preview(backend, settings)
    assert status == 200 and shown["status"] == "needs_confirmation" and shown["changes"]
    assert env.path.read_bytes() == before and env.audit() == []  # a preview changes nothing
    assert {c["setting"] for c in shown["changes"]} == {name}
    status, done = apply(backend, settings, shown["confirm"])
    assert (status, done["status"]) == (200, "applied")
    assert [e["outcome"] for e in env.audit()] == ["started", "applied"]
    # What is in force now is what the page's settings read shows, and re-applying is a no-op.
    status, now = call(backend, "GET", "/api/settings?team=dev")
    assert status == 200
    if name == "paused":  # the kit's pause record is the state; `current` does not hold the reason
        assert now["settings"]["paused"] == {"paused": True}
    else:
        assert preview(backend, settings)[1]["status"] == "unchanged"


@pytest.mark.loopback
def test_cancel_changes_nothing(env: Env) -> None:  # noqa: F811
    backend = page_backend(env)
    before = env.path.read_bytes()
    status, shown = preview(backend, flow("cancel")["settings"])
    assert status == 200 and shown["status"] == "needs_confirmation"
    # Cancel is the page dropping the token: no request is made, so there is nothing to undo.
    script = (KIT / "dashboard_settings.js").read_text()
    cancel = script.split("function cancelChange()")[1].split("\n}\n")[0]
    assert "call(" not in cancel and "fetch(" not in cancel
    assert env.path.read_bytes() == before
    assert env.provisioned == [] and env.audit() == []


@pytest.mark.loopback
def test_a_declined_confirmation_is_refused_and_changes_nothing(env: Env) -> None:  # noqa: F811
    case = flow("decline")
    backend = page_backend(env)
    before = env.path.read_bytes()
    for bad in case["bad_confirms"]:
        status, body = apply(backend, case["settings"], bad)
        if len(bad) == 64:  # well formed, but not this change's token
            assert (status, body) == (409, {"code": "confirmation_mismatch"})
        else:
            assert (status, body) == (400, {"code": "invalid_confirm"})
    # A well-formed token for a different change is the same refusal, as is one gone stale.
    token = preview(backend, case["other_settings"])[1]["confirm"]
    assert apply(backend, case["settings"], token) == (409, {"code": "confirmation_mismatch"})
    stale = preview(backend, case["settings"])[1]["confirm"]
    env.confirmed({"developers": 5})  # the file moves on after the preview
    assert apply(backend, case["settings"], stale)[0] == 409
    assert env.host()["developers"] == 5  # only the change made in between
    assert [e["outcome"] for e in env.audit()] == ["started", "applied"]
    assert before != env.path.read_bytes()


@pytest.mark.loopback
def test_a_refusal_by_the_kernel_leaves_the_old_settings(env: Env) -> None:  # noqa: F811
    backend = page_backend(env)
    token = preview(backend, flow("cancel")["settings"])[1]["confirm"]
    before = env.path.read_bytes()
    env.refuse = "PUT"
    status, body = apply(backend, flow("cancel")["settings"], token)
    assert status == 422 and body["code"].startswith("refused: not applied")
    assert secret() not in json.dumps(body)
    assert env.path.read_bytes() == before


@pytest.mark.loopback
def test_invalid_settings_and_bodies_are_refused(env: Env) -> None:  # noqa: F811
    backend = page_backend(env)
    status, body = preview(backend, {"model": "has space"})
    assert status == 422 and "model" in body["code"]
    status, body = preview(backend, {"credentials": {"token": secret()}})
    assert status == 422 and secret() not in json.dumps(body)
    for bad in ({}, {"team": "dev"}, {"team": "dev", "settings": {}, "x": 1}):
        assert call(backend, "POST", "/api/settings/preview", bad)[0] == 400
    assert call(backend, "POST", "/api/settings/preview", {"team": "x", "settings": {}})[0] == 404
    assert call(backend, "POST", "/api/settings/other", {})[0] == 404
    assert call(backend, "GET", "/api/settings?team=nope")[0] == 404
    # Without a host file (fixtures) the page has nothing to edit.
    bare = teams().TeamsBackend({"dev": lambda: {}})
    assert preview(bare, {"model": "x"}) == (404, {"code": "settings_unavailable"})


@pytest.mark.loopback
def test_only_the_groups_supervisor_gets_through(
    env: Env,  # noqa: F811
    team: tuple[Kernel, str, str],
) -> None:
    kernel, _, _ = team
    before = env.path.read_bytes()
    for who in ("sup-desk", "runtime"):
        backend = page_backend(env, overview(kernel, who))
        refused = (404, {"code": "group_not_found"})
        assert call(backend, "GET", "/api/settings?team=dev") == refused
        assert preview(backend, {"developers": 3}) == refused
        assert apply(backend, {"developers": 3}, "0" * 64) == refused
    assert env.path.read_bytes() == before and env.provisioned == [] and env.audit() == []
    # The supervisor is let through. Once the kernel disables them (a paused team) the preview
    # still reads, as the overview does, but the apply's own writes are refused.
    backend = page_backend(env, overview(kernel, "sup-eng"))
    token = preview(backend, {"developers": 3})[1]["confirm"]
    with kernel.database.engine.begin() as conn:
        conn.exec_driver_sql("UPDATE principals SET disabled=1 WHERE principal_id='sup-eng'")
    assert preview(backend, {"developers": 3})[0] == 200
    assert apply(backend, {"developers": 3}, token) == (403, {"code": "principal_disabled"})
    assert env.path.read_bytes() == before and env.audit() == []


@pytest.mark.loopback
def test_no_secret_reaches_the_page_the_api_or_the_audit(env: Env) -> None:  # noqa: F811
    backend = page_backend(env)
    settings = {"developers": 3, "product_goals": {"goals": "Ship.", "non_goals": "Nothing."}}
    shown = preview(backend, settings)[1]
    done = apply(backend, settings, shown["confirm"])[1]
    outputs = [
        call(backend, "GET", path)[1]
        for path in ("/settings", "/dashboard_settings.js", "/api/settings?team=dev")
    ]
    outputs += [shown, done, env.audit()]
    blob = json.dumps(outputs) + (KIT / "dashboard_settings.html").read_text()
    for text in (secret(), "reviewer_login", "github-reviewer", ".sock", "credentials"):
        assert text not in blob
    assert "goals" in json.dumps(shown["changes"])  # goals are settings; they are not secrets
    script = (KIT / "dashboard_settings.js").read_text()
    assert "innerHTML" not in script and "localStorage" not in script
    assert "authorization" not in script.lower() and "bearer" not in script.lower()
    assert (
        call(backend, "GET", "/api/settings?team=dev", headers={"Host": "evil.example"})[0] == 403
    )
    origin = {"Origin": "http://evil.example"}
    assert call(backend, "POST", "/api/settings/preview", {}, headers=origin)[0] == 403


@pytest.mark.loopback
def test_the_page_is_served_and_the_read_only_page_stays_read_only() -> None:
    backend = teams().TeamsBackend({"dev": lambda: {}})
    status, page = call(backend, "GET", "/settings")
    assert status == 200 and '<script src="/dashboard_settings.js">' in page
    assert call(backend, "GET", "/dashboard_settings.js")[0] == 200
    for read_only in ("dashboard_teams.html", "dashboard_teams.js"):
        assert "settings" not in (KIT / read_only).read_text().lower().replace("dashboard_", "")
