"""The dashboard's Settings section (POST /api/settings/preview and /api/settings/apply in
deploy/team-kit/dashboard_teams.py): the kernel decides who may change settings, by a read only
the group's supervisor may make, before a preview and before an apply (pause and resume too); a
preview changes nothing; an apply needs the preview's confirmation code and goes through the
kit's `apply_settings` (audit, restore on refusal); goals text is shown by size only; no secret is
shown; and nothing is offered with `--fixtures`."""

import json
from pathlib import Path
from typing import Any

import pytest
from test_dashboard import Supervisor
from test_dashboard_teams import KIT, backend, call, teams
from test_team_settings import SETTING_FILES, fixture, secret
from test_team_settings import host_file as host_file  # noqa: F401  (fixture)
from test_team_status import setup, team

from cardea.service.client import ClientError
from cardea.service.kernel import Kernel

__all__ = ["setup", "team"]  # the fixtures the tests below ask for

pytestmark = pytest.mark.loopback

PREVIEW, APPLY = "/api/settings/preview", "/api/settings/apply"
GOALS = "Ship the " + "-".join(["secret", "looking", "goals"]) + " plan.\nSecond line."


class Gate:
    """The kernel as a principal: reads go to the real kernel, which judges the principal; the
    writes the kit makes are recorded, and refused when `refuse` is set."""

    def __init__(self, kernel: Kernel, who: str, refuse: bool = False) -> None:
        self.inner, self.refuse = Supervisor(kernel, who), refuse
        self.log: list[tuple[str, str]] = []

    def connect(self, host: dict[str, Any]) -> "Gate":
        return self

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        self.log.append((method, path))
        if method == "PUT":
            if self.refuse:
                raise ClientError("forbidden", 403)
            return {}
        return self.inner.request(method, path, **kwargs)

    def close(self) -> None:
        pass

    def writes(self) -> list[tuple[str, str]]:
        return [entry for entry in self.log if entry[0] == "PUT"]


class Env:
    def __init__(self, kernel: Kernel, path: Path, kit: Any) -> None:
        self.kernel, self.path, self.kit = kernel, path, kit
        self.provisioned: list[tuple[dict[str, Any], bool]] = []

    def gate(self, who: str = "sup-eng", refuse: bool = False) -> Gate:
        return Gate(self.kernel, who, refuse)

    def backend(self, gate: Gate) -> Any:
        module = teams()
        read = module.base.KernelBackend(gate.inner.request, "eng", "eng-team")
        edit = module.settings_editor(self.path, gate.connect)
        return module.TeamsBackend({"eng": read.overview}, {"eng": read.mail}, {"eng": edit})

    def post(
        self, gate: Gate, route: str, settings: Any, confirm: str | None = None, **extra: Any
    ) -> tuple[int, Any]:
        body = {"team": "eng", "settings": settings, **extra}
        if confirm is not None:
            body["confirm"] = confirm
        return call(self.backend(gate), "POST", route, body)

    def kit_audit(self) -> list[dict[str, Any]]:
        applier = teams().load("teamsettings_apply")
        found: list[dict[str, Any]] = applier.read_audit(self.kit, self.kit.load_host(self.path))
        return found

    def bytes(self) -> bytes:
        return self.path.read_bytes()


@pytest.fixture
def env(
    team: tuple[Kernel, str, str],
    host_file: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> Env:
    document = json.loads(host_file.read_text())
    document.update(namespace="eng", group="eng-team")
    document["supervisor"]["principal_id"] = "sup-eng"
    host_file.write_text(json.dumps(document))
    kit = teams().load("teamsettings_apply")._modules()[0]
    made = Env(team[0], host_file, kit)

    def provision(host: dict[str, Any], *, paused: bool, **_: Any) -> Path:
        made.provisioned.append((host, paused))
        return Path("provision.json")

    def pause(host: dict[str, Any], reason: str | None = None) -> list[str]:
        kit.pause_record_path(host).write_text(json.dumps({"reason": reason}))
        return []

    def resume(host: dict[str, Any]) -> None:
        provision(host, paused=False)
        kit.pause_record_path(host).unlink(missing_ok=True)

    monkeypatch.setattr(kit, "provision", provision)
    monkeypatch.setattr(kit, "pause", pause)
    monkeypatch.setattr(kit, "resume", resume)
    return made


def disable(kernel: Kernel, who: str) -> None:
    with kernel.database.engine.begin() as conn:
        conn.exec_driver_sql(f"UPDATE principals SET disabled=1 WHERE principal_id='{who}'")


@pytest.mark.parametrize("name", SETTING_FILES)
def test_a_change_is_previewed_then_confirmed_and_applied(env: Env, name: str) -> None:
    change = fixture(name)["valid"]
    before = env.bytes()
    gate = env.gate()
    status, found = env.post(gate, PREVIEW, change)
    assert status == 200 and found["status"] == "needs_confirmation"
    assert found["changes"] and found["problems"] == [] and found["audit_id"] is None
    # A preview changes nothing: no file, no write to the kernel, no provisioning, no audit.
    assert env.bytes() == before and gate.writes() == [] and env.provisioned == []
    assert env.kit_audit() == []
    status, done = env.post(gate, APPLY, change, found["confirm"])
    assert (status, done["status"]) == (200, "applied") and done["audit_id"]
    assert done["changes"] == found["changes"]
    entries = env.kit_audit()
    assert [e["outcome"] for e in entries] == ["started", "applied"]
    assert {e["id"] for e in entries} == {done["audit_id"]}
    if name == "paused":
        assert env.bytes() == before and gate.writes() == []
        assert env.kit.read_pause_record(env.kit.load_host(env.path))
    else:
        assert env.bytes() != before and ("PUT", "/groups/eng-team") in gate.writes()
        assert env.provisioned and env.provisioned[-1][1] is False
    # Applying the same change again is a no-op: nothing is written or recorded.
    again = env.post(gate, PREVIEW, change)[1]
    if name != "paused":
        assert again["status"] == "unchanged" and again["changes"] == []
    assert len(env.kit_audit()) == 2


def test_the_page_is_told_which_teams_are_editable(env: Env) -> None:
    entry = call(env.backend(env.gate()), "GET", "/api/status")[1]["teams"][0]
    assert entry["name"] == "eng" and entry["editable"] is True


def test_an_apply_without_the_confirmation_code_is_refused(env: Env) -> None:
    before, gate = env.bytes(), env.gate()
    for confirm in (None, ""):
        status, body = env.post(gate, APPLY, {"developers": 5}, confirm)
        assert (status, body) == (400, {"code": "confirmation_required"})
    status, body = env.post(gate, APPLY, {"developers": 5}, confirm=None, extra=1)
    assert (status, body) == (400, {"code": "invalid_settings"})
    assert env.bytes() == before and env.provisioned == [] and env.kit_audit() == []


def test_a_stale_or_foreign_confirmation_code_changes_nothing(env: Env) -> None:
    before, gate = env.bytes(), env.gate()
    code = env.post(gate, PREVIEW, {"developers": 5})[1]["confirm"]
    # The code is for what was previewed: a different change is not confirmed by it.
    assert env.post(gate, APPLY, {"developers": 6}, code) == (409, {"code": "confirmation_stale"})
    assert env.post(gate, APPLY, {"developers": 5}, "0" * 64)[0] == 409
    assert env.bytes() == before and env.kit_audit() == []
    # The file moves on after the preview (someone else applies a change): the code is stale.
    other = env.post(gate, PREVIEW, {"developers": 7})[1]["confirm"]
    assert env.post(gate, APPLY, {"developers": 7}, other)[0] == 200
    code = env.post(gate, PREVIEW, {"developers": 8, "model": "claude-opus-5-5"})[1]["confirm"]
    assert env.post(gate, APPLY, {"developers": 8, "model": "claude-opus-5-5"}, code)[0] == 200
    assert env.post(gate, APPLY, {"developers": 7}, other) == (
        409,
        {"code": "confirmation_stale"},
    )
    assert json.loads(env.bytes())["developers"] == 8 and len(env.kit_audit()) == 4


def test_a_cancelled_preview_leaves_nothing_behind(env: Env) -> None:
    before, gate = env.bytes(), env.gate()
    assert env.post(gate, PREVIEW, {"model": "claude-opus-5-5"})[0] == 200  # then the user cancels
    assert env.bytes() == before and env.kit_audit() == [] and gate.writes() == []


def test_a_kernel_refusal_puts_the_old_settings_back(env: Env) -> None:
    before = env.bytes()
    gate = env.gate(refuse=True)
    change = {"developers": 5, "model": "claude-opus-5-5"}
    code = env.post(gate, PREVIEW, change)[1]["confirm"]
    status, body = env.post(gate, APPLY, change, code)
    assert status == 409 and body["code"] == "not_applied"
    assert "the old settings are back" in body["detail"] and "forbidden" in body["detail"]
    assert env.bytes() == before  # byte for byte
    assert env.provisioned[-1][0]["developers"] == 2 and env.provisioned[-1][1] is False
    entries = env.kit_audit()
    assert [e["outcome"] for e in entries] == ["started", "refused"]
    assert entries[-1]["restored"] is True


@pytest.mark.parametrize(
    "change",
    [
        {"developers": 5},
        {"paused": {"paused": True, "reason": "freeze"}},
        {"paused": {"paused": False}},
    ],
    ids=["settings", "pause", "resume"],
)
@pytest.mark.parametrize("who", ["sup-desk", "runtime"])
def test_only_the_groups_supervisor_may_preview_or_apply(env: Env, who: str, change: Any) -> None:
    before, gate = env.bytes(), env.gate(who)
    for route in (PREVIEW, APPLY):
        status, body = env.post(gate, route, change, "0" * 64)
        assert (status, body) == (404, {"code": "group_not_found"})
    assert gate.writes() == [] and env.bytes() == before
    assert env.provisioned == [] and env.kit_audit() == []
    assert call(env.backend(gate), "GET", "/api/status")[0] == 200


@pytest.mark.parametrize(
    "change",
    [{"developers": 5}, {"paused": {"paused": True}}, {"paused": {"paused": False}}],
    ids=["settings", "pause", "resume"],
)
def test_a_disabled_supervisor_is_refused_live(env: Env, change: Any) -> None:
    gate = env.gate()
    code = env.post(gate, PREVIEW, change)[1]["confirm"]  # allowed while the supervisor is live
    before = env.bytes()
    disable(env.kernel, "sup-eng")
    for route in (PREVIEW, APPLY):
        status, body = env.post(gate, route, change, code)
        assert (status, body) == (403, {"code": "principal_disabled"})
    assert gate.writes() == [] and env.bytes() == before
    assert env.provisioned == [] and env.kit_audit() == []


def test_the_kernel_is_asked_before_anything_is_read_or_validated(env: Env) -> None:
    gate = env.gate("sup-desk")
    # Not even the validation of the settings is answered to someone who may not read the group.
    status, body = env.post(gate, PREVIEW, {"developers": 0})
    assert (status, body) == (404, {"code": "group_not_found"})
    assert [m for m, _ in gate.log] == ["GET"] and gate.log[0][1].endswith("/status/mail/text")


def test_secrets_are_never_echoed(env: Env, host_file: Path) -> None:  # noqa: F811
    gate, seen = env.gate(), secret()
    outputs: list[Any] = []
    for route, settings in (
        (PREVIEW, {"credentials": {"token": seen}}),
        (PREVIEW, {"kernel": {"socket": seen}}),
        (PREVIEW, {"supervisor": {"principal_id": seen}}),
        (PREVIEW, {"model": f"bad model {seen}"}),
        (PREVIEW, {"developers": seen}),
        (PREVIEW, {"paused": {"paused": True, "reason": seen * 40}}),
    ):
        status, body = env.post(gate, route, settings)
        assert status == 400 and body["code"] == "invalid_settings"
        outputs.append(body)
    outputs.append(call(env.backend(gate), "GET", "/api/status")[1])
    outputs += [call(env.backend(gate), "GET", path)[1] for path in ("/", "/dashboard_teams.js")]
    change = {"product_goals": {"goals": GOALS}, "model": "claude-opus-5-5"}
    found = env.post(gate, PREVIEW, change)[1]
    outputs += [found, env.post(gate, APPLY, change, found["confirm"])[1]]
    assert all(seen not in json.dumps(o) for o in outputs)
    applied = json.dumps(outputs[-2:])
    assert "credentials" not in applied and "socket" not in applied
    assert seen in host_file.read_text()  # the credentials are in the host file, and stay there


def test_goals_text_is_shown_by_size_only(env: Env) -> None:
    gate = env.gate()
    change = {"product_goals": {"goals": GOALS}}
    status, found = env.post(gate, PREVIEW, change)
    (goals,) = [c for c in found["changes"] if c["field"] == "product_goals.goals"]
    assert goals["from"] == {"bytes": len("Original goals.")}
    assert goals["to"] == {"bytes": len(GOALS.encode())}
    done = env.post(gate, APPLY, change, found["confirm"])[1]
    audit = json.dumps(env.kit_audit())
    for text in (json.dumps(found), json.dumps(done), audit):
        assert "Original goals." not in text and "looking" not in text and "Second line" not in text
    page = (KIT / "dashboard_teams.js").read_text()
    assert "bytes" in page and "goals" in page


def test_bad_requests_are_refused_before_the_kernel_is_asked(env: Env) -> None:
    gate = env.gate()
    b = env.backend(gate)
    bad: list[Any] = [None, [], {}, {"team": "eng"}, {"settings": {}}, {"team": 5, "settings": {}},
                      {"team": "eng", "settings": {}, "confirm": 5},
                      {"team": "eng", "settings": {}, "x": 1}]  # fmt: skip
    for body in bad:
        assert call(b, "POST", PREVIEW, body) == (400, {"code": "invalid_settings"})
    assert call(b, "POST", PREVIEW, {"team": "nope", "settings": {}}) == (
        404,
        {"code": "unknown_team"},
    )
    assert gate.log == []
    # Same-origin JSON only, like the message box.
    assert call(b, "POST", PREVIEW, {"team": "eng"}, {"Host": "evil.example"})[0] == 403
    assert call(b, "POST", PREVIEW, {"team": "eng"}, {"Origin": "http://evil.example"})[0] == 403
    assert call(b, "POST", PREVIEW, {"team": "eng"}, {"Content-Type": "text/plain"})[0] == 415
    assert call(b, "GET", PREVIEW)[0] == 404 and call(b, "PUT", PREVIEW, {})[0] in (404, 501)
    assert call(b, "POST", "/api/settings/other", {})[0] == 404
    assert call(b, "POST", "/api/message", {"role": "dev"})[0] == 404  # still no message box
    assert gate.log == []


def test_settings_are_not_offered_with_fixtures() -> None:
    b = backend()
    for route in (PREVIEW, APPLY):
        body = {"team": "dev", "settings": {"developers": 3}, "confirm": "x"}
        assert call(b, "POST", route, body) == (404, {"code": "settings_unavailable"})
    assert all("editable" not in t for t in call(b, "GET", "/api/status")[1]["teams"])


def test_the_fixtures_mode_builds_no_editor(tmp_path: Path) -> None:
    module = teams()
    path = tmp_path / "a.json"
    path.write_text(json.dumps({"group": "g"}))
    served: list[Any] = []

    class Stop(Exception):
        pass

    def capture(built: Any, *args: Any) -> Any:
        served.append(built)
        raise Stop

    module.server = capture
    with pytest.raises(Stop):
        module.main(["--fixtures", f"dev={path}"])
    assert served[0].editors == {}
