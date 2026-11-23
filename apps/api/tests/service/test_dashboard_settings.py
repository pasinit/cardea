"""The multi-team dashboard's Settings section (deploy/team-kit/dashboard_teams.py): a change is
previewed, then confirmed or dropped, through the kit's confirm-and-apply path; the kernel's
supervisor-only read comes first, whatever the change; goals show by size; no secret is shown."""

import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from test_dashboard import KIT, Supervisor
from test_dashboard import call as raw_call
from test_dashboard_teams import teams as load_teams
from test_team_settings import SETTING_FILES, fixture, secret
from test_team_settings import host_file as host_file  # noqa: F401  (fixture)
from test_team_status import setup, team

from cardea.service.kernel import Kernel

__all__ = ["setup", "team"]  # the fixtures the tests below ask for

PREVIEW, APPLY = "/api/settings/preview", "/api/settings/apply"
CHANGE: dict[str, Any] = {"developers": 3, "run_limits": {"team": 35}, "model": "claude-opus-5-5"}
GOALS = "Help small teams ship."


class FakeKernel:
    """The kernel the apply writes to; the supervisor-only read goes to the real one."""

    def __init__(self, log: list[tuple[str, str]], refuse: str | None) -> None:
        self.log, self.refuse = log, refuse

    def request(self, method: str, path: str, **_: Any) -> dict[str, Any]:
        self.log.append((method, path))
        if self.refuse and method == self.refuse:
            raise PermissionError(f"denied for {secret()}")
        return {}

    def close(self) -> None:
        pass


class Env:
    def __init__(self, module: ModuleType, kernel: Kernel, path: Path) -> None:
        self.module, self.kernel, self.path = module, kernel, path
        self.log: list[tuple[str, str]] = []
        self.paused_with: list[str | None] = []
        self.refuse: str | None = None

    def backend(self, who: str = "sup-eng") -> Any:
        request = Supervisor(self.kernel, who).request
        reader = self.module.base.KernelBackend(request, "eng", "eng-team")
        managed = self.module.TeamSettings(
            self.path,
            lambda: reader.mail({"limit": 1}),
            lambda host: FakeKernel(self.log, self.refuse),
        )
        return self.module.TeamsBackend({"dev": lambda: {}}, managed={"dev": managed})

    def post(
        self, path: str, settings: Any, confirm: str | None = None, who: str = "sup-eng"
    ) -> tuple[int, Any]:
        body = {"team": "dev", "settings": settings}
        if confirm is not None:
            body["confirm"] = confirm
        return raw_call(self.backend(who), "POST", path, body, build=self.module.server)

    def confirmed(self, settings: Any) -> tuple[int, Any]:
        confirm = self.post(PREVIEW, settings)[1]["confirm"]
        return self.post(APPLY, settings, confirm)

    def host(self) -> dict[str, Any]:
        found: dict[str, Any] = json.loads(self.path.read_text())
        return found

    def audit(self) -> list[dict[str, Any]]:
        kit = self.module.apply._modules()[0]
        found: list[dict[str, Any]] = self.module.apply.read_audit(kit, kit.load_host(self.path))
        return found


@pytest.fixture
def env(
    team: tuple[Kernel, str, str],
    host_file: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> Env:
    module = load_teams()
    kit = module.apply._modules()[0]
    made = Env(module, team[0], host_file)

    def pause(host: dict[str, Any], reason: str | None = None) -> list[str]:
        made.paused_with.append(reason)
        kit.pause_record_path(host).write_text(json.dumps({"reason": reason}))
        return []

    def resume(host: dict[str, Any]) -> None:
        kit.pause_record_path(host).unlink(missing_ok=True)

    monkeypatch.setattr(kit, "provision", lambda host, **_: Path("provision.json"))
    monkeypatch.setattr(kit, "pause", pause)
    monkeypatch.setattr(kit, "resume", resume)
    return made


@pytest.mark.loopback
@pytest.mark.parametrize("name", SETTING_FILES)
def test_each_setting_is_previewed_then_applied(env: Env, name: str) -> None:
    valid = fixture(name)["valid"]
    before = env.path.read_bytes()
    status, found = env.post(PREVIEW, valid)
    assert status == 200 and found["status"] == "needs_confirmation" and found["changes"]
    assert found["authority"] and found["problems"] == []
    assert env.path.read_bytes() == before and env.audit() == []  # a preview changes nothing
    status, done = env.post(APPLY, valid, found["confirm"])
    assert (status, done["status"]) == (200, "applied")
    assert [c["outcome"] for c in env.audit()] == ["started", "applied"]
    if name == "paused":
        assert env.paused_with == ["release freeze"]
    else:
        assert env.path.read_bytes() != before
    assert env.post(PREVIEW, valid)[1]["status"] == "unchanged"


@pytest.mark.loopback
def test_goals_are_shown_by_size_only(env: Env) -> None:
    status, found = env.post(PREVIEW, {"product_goals": {"goals": GOALS}})
    assert status == 200
    goals = next(c for c in found["changes"] if c["field"] == "product_goals.goals")
    assert goals["to"] == f"{len(GOALS)} bytes"
    done = env.post(APPLY, {"product_goals": {"goals": GOALS}}, found["confirm"])[1]
    shown = json.dumps([found, done])
    assert GOALS not in shown and "Original goals." not in shown
    assert GOALS in env.host()["product"]["goals"]  # the host file has it, the page does not


@pytest.mark.loopback
def test_cancelling_sends_nothing_and_a_dropped_preview_changes_nothing(env: Env) -> None:
    before = env.path.read_bytes()
    assert env.post(PREVIEW, CHANGE)[0] == 200  # the page then drops it: no request follows
    assert env.path.read_bytes() == before and env.audit() == [] and env.log == []
    script = (KIT / "dashboard_teams.js").read_text()
    assert script.count("/api/settings/") == 2 and "Cancel" in script


@pytest.mark.loopback
def test_a_missing_or_stale_confirmation_is_refused_and_changes_nothing(env: Env) -> None:
    token = env.post(PREVIEW, CHANGE)[1]["confirm"]
    before = env.path.read_bytes()
    assert env.post(APPLY, CHANGE, "") == (409, {"code": "confirmation_required"})
    assert env.post(APPLY, CHANGE, "0" * 64) == (409, {"code": "stale_confirmation"})
    assert env.post(APPLY, {**CHANGE, "developers": 4}, token)[1] == {"code": "stale_confirmation"}
    body = {"team": "dev", "settings": CHANGE}  # no confirm at all
    assert raw_call(env.backend(), "POST", APPLY, body, build=env.module.server)[0] == 400
    assert env.confirmed({"developers": 2, "model": "claude-haiku-4-5"})[0] == 200
    assert env.post(APPLY, CHANGE, token)[1] == {"code": "stale_confirmation"}  # the file moved on
    assert env.host()["developers"] == 2 and before != env.path.read_bytes()
    assert [c["outcome"] for c in env.audit()] == ["started", "applied"]


@pytest.mark.loopback
def test_a_kernel_refusal_restores_the_old_settings(env: Env) -> None:
    before = env.path.read_bytes()
    env.refuse = "PUT"
    status, body = env.confirmed(CHANGE)
    assert (status, body["code"]) == (409, "not_applied")
    assert "the old settings are back" in body["detail"] and secret() not in json.dumps(body)
    assert env.path.read_bytes() == before
    assert env.audit()[-1]["outcome"] == "refused" and env.audit()[-1]["restored"] is True


@pytest.mark.loopback
def test_only_the_supervisor_may_preview_or_apply_pause_included(env: Env) -> None:
    pause = {"paused": {"paused": True, "reason": "freeze"}}
    token = env.post(PREVIEW, CHANGE)[1]["confirm"]
    before = env.path.read_bytes()
    for who in ("sup-desk", "runtime"):
        for settings in (CHANGE, pause):
            refused = (404, {"code": "group_not_found"})
            assert env.post(PREVIEW, settings, who=who) == refused
            assert env.post(APPLY, settings, token, who=who) == refused
    assert env.path.read_bytes() == before and env.audit() == [] and env.paused_with == []
    assert env.log == []


@pytest.mark.loopback
def test_a_disabled_supervisor_is_refused_live(env: Env) -> None:
    token = env.post(PREVIEW, CHANGE)[1]["confirm"]
    with env.kernel.database.engine.begin() as conn:
        conn.exec_driver_sql("UPDATE principals SET disabled=1 WHERE principal_id='sup-eng'")
    pause = {"paused": {"paused": True}}
    refused = (403, {"code": "principal_disabled"})
    assert env.post(PREVIEW, CHANGE) == refused
    assert env.post(APPLY, CHANGE, token) == refused
    assert env.post(APPLY, pause, env.post(PREVIEW, pause)[1].get("confirm", "x")) == refused
    assert env.audit() == [] and env.paused_with == [] and env.host()["developers"] == 2


@pytest.mark.loopback
def test_no_secret_is_echoed(env: Env) -> None:
    attempts = [
        {"credentials": {"reviewer_login": secret()}},
        {secret(): 1},
        {"model": {"x": secret()}},
        {"developers": secret()},
    ]
    for settings in attempts:
        status, body = env.post(PREVIEW, settings)
        assert (status, body) == (422, {"code": "invalid_settings"})
    found = env.post(PREVIEW, CHANGE)[1]
    env.refuse = "PUT"
    refused = env.post(APPLY, CHANGE, found["confirm"])[1]
    for text in (json.dumps(found), json.dumps(refused)):
        assert secret() not in text and "reviewer_login" not in text
        assert "github-reviewer" not in text and "kernel.sock" not in text


@pytest.mark.loopback
def test_it_is_not_offered_with_fixtures_and_stays_on_loopback(env: Env) -> None:
    module = env.module
    plain = module.TeamsBackend({"dev": lambda: {}})
    body = {"team": "dev", "settings": CHANGE}
    for path in (PREVIEW, APPLY):
        assert raw_call(plain, "POST", path, body, build=module.server)[0] == 404
    assert raw_call(plain, "GET", "/api/status", build=module.server)[1]["settings"] == []
    assert raw_call(env.backend(), "GET", "/api/status", build=module.server)[1]["settings"] == [
        "dev"
    ]
    # With settings on, the origin, media type and method rules still hold.
    backend = env.backend()
    evil = {"Host": "evil.example"}
    assert raw_call(backend, "POST", PREVIEW, body, evil, build=module.server)[0] == 403
    other = {"Origin": "http://evil.example"}
    assert raw_call(backend, "POST", PREVIEW, body, other, build=module.server)[0] == 403
    assert raw_call(backend, "GET", PREVIEW, build=module.server)[0] == 404
    assert raw_call(backend, "PUT", PREVIEW, body, build=module.server)[0] == 501
    assert raw_call(backend, "POST", "/api/message", body, build=module.server)[0] == 404
    unknown = {"team": "nope", "settings": CHANGE}
    assert raw_call(backend, "POST", PREVIEW, unknown, build=module.server)[0] == 404
    assert raw_call(backend, "POST", PREVIEW, ["x"], build=module.server)[0] == 400
