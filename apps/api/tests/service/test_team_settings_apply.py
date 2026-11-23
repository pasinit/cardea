"""Confirm and apply of team settings (deploy/team-kit/teamsettings_apply.py): nothing changes
without the confirmation token, a confirmed change goes through the kit's render, provision and
apply, the kernel's refusal leaves the old settings in place, every attempt is audited (who,
when, before and after), re-applying is a no-op, and no secret reaches a result, the audit, a
log or the output."""

import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from test_team_schema import load
from test_team_settings import fixture, secret
from test_team_settings import host_file as host_file  # noqa: F401  (fixture)

CHANGE: dict[str, Any] = {"developers": 3, "run_limits": {"team": 35}, "model": "claude-opus-5-5"}
WHEN = datetime(2026, 11, 23, 9, 0, tzinfo=UTC)


class FakeKernel:
    def __init__(self, log: list[tuple[str, str]], refuse: str | None = None) -> None:
        self.log, self.refuse = log, refuse

    def request(self, method: str, path: str, **_: Any) -> dict[str, Any]:
        self.log.append((method, path))
        if self.refuse and method == self.refuse:
            raise PermissionError(f"denied for {secret()}")  # a lower layer's message
        return {}

    def close(self) -> None:
        self.log.append(("CLOSE", ""))


class Env:
    def __init__(self, module: ModuleType, kit: ModuleType, path: Path) -> None:
        self.module, self.kit, self.path = module, kit, path
        self.kernel_log: list[tuple[str, str]] = []
        self.provisioned: list[tuple[dict[str, Any], bool]] = []
        self.paused_with: list[str | None] = []
        self.refuse: str | None = None
        self.fail_provision = False

    def connect(self, host: dict[str, Any]) -> FakeKernel:
        return FakeKernel(self.kernel_log, self.refuse)

    def go(self, settings: object, confirm: str | None = None, **kw: Any) -> dict[str, Any]:
        found: dict[str, Any] = self.module.apply_settings(
            self.path, settings, confirm=confirm, connect=self.connect, now=lambda: WHEN, **kw
        )
        return found

    def confirmed(self, settings: object, **kw: Any) -> dict[str, Any]:
        return self.go(settings, self.go(settings)["confirm"], **kw)

    def host(self) -> dict[str, Any]:
        found: dict[str, Any] = json.loads(self.path.read_text())
        return found

    def audit(self) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = self.module.read_audit(
            self.kit, self.kit.load_host(self.path)
        )
        return found


@pytest.fixture
def env(host_file: Path, monkeypatch: pytest.MonkeyPatch) -> Env:  # noqa: F811
    module = load("teamsettings_apply")
    kit = module._modules()[0]
    made = Env(module, kit, host_file)

    def provision(host: dict[str, Any], *, paused: bool, **_: Any) -> Path:
        if made.fail_provision:
            raise RuntimeError(f"root failed with {secret()}")
        made.provisioned.append((host, paused))
        return Path("provision.json")

    def pause(host: dict[str, Any], reason: str | None = None) -> list[str]:
        made.paused_with.append(reason)
        pause_file = kit.pause_record_path(host)
        pause_file.write_text(json.dumps({"reason": reason}))
        return []

    def resume(host: dict[str, Any]) -> None:
        provision(host, paused=False)
        kit.pause_record_path(host).unlink(missing_ok=True)

    monkeypatch.setattr(kit, "provision", provision)
    monkeypatch.setattr(kit, "pause", pause)
    monkeypatch.setattr(kit, "resume", resume)
    return made


def test_without_the_token_nothing_changes(env: Env) -> None:
    before = env.path.read_bytes()
    for confirm in (None, "", "0" * 64, "yes", "true"):
        found = env.go(CHANGE, confirm)
        assert found["status"] == "needs_confirmation"
        assert [c["field"] for c in found["preview"]["changes"]]
    assert env.path.read_bytes() == before
    assert env.provisioned == [] and env.kernel_log == [] and env.audit() == []
    assert not list(env.path.parent.glob("*.jsonl"))


def test_a_confirmed_change_goes_through_render_provision_and_apply(env: Env) -> None:
    mode = env.path.stat().st_mode & 0o777
    found = env.confirmed(CHANGE)
    assert found["status"] == "applied"
    host = env.host()
    assert (host["developers"], host["model"], host["budgets"]["team"]["runs"]) == (
        3,
        "claude-opus-5-5",
        35,
    )
    assert [paused for _, paused in env.provisioned] == [False]
    assert env.provisioned[0][0]["developers"] == 3  # provisioned from the new host file
    assert ("PUT", "/collections/work") in env.kernel_log
    assert ("PUT", "/groups/dev-team") in env.kernel_log
    assert env.kernel_log[0][0] == "GET"  # authority read live before the first write
    assert env.path.stat().st_mode & 0o777 == mode


def test_the_token_is_bound_to_what_was_previewed(env: Env) -> None:
    token = env.go(CHANGE)["confirm"]
    other = {**CHANGE, "developers": 4}
    assert env.go(other, token)["status"] == "needs_confirmation"
    env.confirmed({"developers": 2, "model": "claude-haiku-4-5"})  # the file moves on
    assert env.go(CHANGE, token)["confirm"] != token
    assert {c["field"] for c in env.audit()[-1]["changes"]} == {"model"}


def test_reapplying_the_same_change_is_idempotent(env: Env) -> None:
    env.confirmed(CHANGE)
    after_first = env.path.read_bytes()
    audited, provisioned, calls = len(env.audit()), len(env.provisioned), len(env.kernel_log)
    again = env.confirmed(CHANGE)
    assert again["status"] == "unchanged" and again["preview"]["changes"] == []
    assert env.path.read_bytes() == after_first
    assert (len(env.audit()), len(env.provisioned), len(env.kernel_log)) == (
        audited,
        provisioned,
        calls,
    )


def test_the_audit_records_who_when_before_and_after(env: Env) -> None:
    env.confirmed(CHANGE, by="alex")
    started, done = env.audit()
    assert (started["outcome"], done["outcome"]) == ("started", "applied")
    assert started["id"] == done["id"]
    assert done["by"] == "alex" and done["at"] == WHEN.isoformat(timespec="seconds")
    changes = {c["field"]: (c["before"], c["after"]) for c in done["changes"]}
    assert changes["developers"] == (2, 3)
    assert changes["run_limits.team"] == (30, 35)
    assert changes["model"] == ("claude-sonnet-4-5", "claude-opus-5-5")
    assert done["team"] == "dev/dev-team"
    assert (env.path.parent / "dev-team.settings-audit.jsonl").stat().st_mode & 0o777 == 0o600


def test_the_audit_names_the_supervisor_by_default_and_cleans_a_given_name(env: Env) -> None:
    env.confirmed({"developers": 3})
    assert env.audit()[-1]["by"] == "sup-dev"
    env.confirmed({"developers": 4}, by="x\ny" + "z" * 500)
    assert "\n" not in env.audit()[-1]["by"] and len(env.audit()[-1]["by"]) <= 128


def test_goals_are_audited_by_size_and_digest(env: Env) -> None:
    env.confirmed({"product_goals": {"goals": "Private plans."}})
    text = (env.path.parent / "dev-team.settings-audit.jsonl").read_text()
    assert "Private plans." not in text and "Original goals." not in text
    change = next(c for c in env.audit()[-1]["changes"] if c["field"] == "product_goals.goals")
    assert change["after"]["bytes"] == 14 and len(change["after"]["sha256"]) == 64
    assert "Private plans." in env.host()["product"]["goals"]


def test_a_kernel_refusal_restores_the_old_settings(env: Env) -> None:
    before = env.host()
    env.refuse = "PUT"
    with pytest.raises(env.module.ApplyError, match="not applied") as raised:
        env.confirmed(CHANGE)
    assert "the old settings are back" in str(raised.value)
    assert secret() not in str(raised.value)
    assert env.host() == before
    assert [paused for _, paused in env.provisioned][-1] is False
    assert env.provisioned[-1][0]["developers"] == 2  # the old team provisioned again
    started, refused = env.audit()
    assert refused["outcome"] == "refused" and refused["restored"] is True
    assert started["id"] == refused["id"]
    assert secret() not in json.dumps(env.audit())


def test_a_supervisor_the_kernel_refuses_stops_the_change_before_any_write(env: Env) -> None:
    before = env.path.read_bytes()
    env.refuse = "GET"
    with pytest.raises(env.module.ApplyError):
        env.confirmed(CHANGE)
    assert env.path.read_bytes() == before
    assert env.provisioned == []
    assert not [1 for m, _ in env.kernel_log if m == "PUT"]


def test_a_provisioning_failure_is_reported_and_leaves_the_file_alone(env: Env) -> None:
    before = env.host()
    env.fail_provision = True
    with pytest.raises(env.module.ApplyError, match="RuntimeError") as raised:
        env.confirmed(CHANGE)
    assert "NOT be restored" in str(raised.value)  # restoring needs provisioning too
    assert env.host() == before
    assert secret() not in str(raised.value) and secret() not in json.dumps(env.audit())
    assert env.audit()[-1]["restored"] is False


def test_a_change_the_kit_cannot_render_is_refused_before_the_file_changes(env: Env) -> None:
    before = env.path.read_bytes()
    with pytest.raises(env.module.ApplyError, match="refused"):
        env.go({"run_limits": {"roles": {"dev": 31}}})
    assert env.path.read_bytes() == before and env.audit() == []


def test_an_invalid_or_secret_bearing_change_is_refused(env: Env) -> None:
    settings = env.module._modules()[1]
    for bad in ({"developers": 0}, {"credentials": {"reviewer_login": secret()}}, {}):
        with pytest.raises(settings.SettingsError):
            env.go(bad)
    assert env.audit() == [] and env.provisioned == []


def test_an_audit_that_cannot_be_written_stops_the_change(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = env.path.read_bytes()

    def broken(path: Path, entry: dict[str, Any]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(env.module, "_record", broken)
    with pytest.raises(env.module.ApplyError, match="nothing changed"):
        env.confirmed(CHANGE)
    assert env.path.read_bytes() == before and env.provisioned == []


def test_pause_and_resume_go_through_the_kit(env: Env) -> None:
    env.confirmed({"paused": {"paused": True, "reason": "release freeze"}})
    assert env.paused_with == ["release freeze"]
    assert env.audit()[-1]["changes"][-1]["after"] == "release freeze"
    # While paused, other settings wait for a resume.
    with pytest.raises(env.module.ApplyError, match="paused"):
        env.confirmed({"developers": 5})
    assert env.host()["developers"] == 2
    env.confirmed({"paused": {"paused": False}, "developers": 5})
    assert env.host()["developers"] == 5 and env.provisioned[-1][1] is False
    assert ("PUT", "/groups/dev-team") in env.kernel_log
    assert env.confirmed({"paused": {"paused": False}})["status"] == "unchanged"


def test_every_fixture_setting_applies(env: Env) -> None:
    for name in ("developers", "model", "run_limits", "spend_limits", "schedules"):
        assert env.confirmed(fixture(name)["valid"])["status"] == "applied"
        assert env.confirmed(fixture(name)["valid"])["status"] == "unchanged"
    host = env.host()
    assert host["budgets"]["roles"]["dev"]["usd"] == 10.0 and host["schedules"]["dev"]


def test_no_secret_reaches_results_audit_logs_or_output(
    env: Env,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    found = env.confirmed(CHANGE)
    env.refuse = "PUT"
    with pytest.raises(env.module.ApplyError) as raised:
        env.confirmed({"developers": 6})
    shown = json.dumps(found) + str(raised.value) + json.dumps(env.audit())
    shown += (env.path.parent / "dev-team.settings-audit.jsonl").read_text()
    out = capsys.readouterr()
    for text in (shown, out.out, out.err, caplog.text):
        assert secret() not in text
        assert "reviewer_login" not in text and "github-reviewer" not in text
        assert "kernel.sock" not in text
    # The host file keeps its private sections untouched.
    assert env.host()["credentials"]["reviewer_login"] == secret()


def test_the_command_previews_then_applies(
    env: Env, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    change = tmp_path / "change.json"
    change.write_text(json.dumps({"developers": 3}))
    monkeypatch.setattr(env.kit, "client", env.connect)
    args = ["--host", str(env.path), "--settings", str(change)]
    assert env.module.main(args) == 2
    out = capsys.readouterr().out
    assert "developers: 2 -> 3" in out and "nothing changed" in out
    assert env.host()["developers"] == 2
    token = out.split("--confirm ")[1].split()[0]
    assert env.module.main([*args, "--confirm", token]) == 0
    assert env.host()["developers"] == 3
    change.write_text(json.dumps({"kernel": {"socket": secret()}}))
    assert env.module.main(args) == 1
    assert secret() not in capsys.readouterr().err


def test_the_model_module_still_holds_no_kernel_calls() -> None:
    source = (Path(sys.modules["test_team_schema"].KIT) / "teamsettings.py").read_text()
    assert "provision(" not in source.replace("provisioning", "")


def test_a_host_file_in_an_unwritable_directory_is_rewritten_in_place(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`/etc/cardea` is root's: the kernel account owns the host file but cannot create or rename
    files beside it, so the file is rewritten where it is."""
    path = env.path
    path.chmod(0o640)
    before = path.stat()

    def refuse(*_: Any) -> None:
        raise PermissionError("the directory is not writable")

    monkeypatch.setattr(env.module.os, "replace", refuse)
    found = env.confirmed({"developers": 3, "email_rate_limit": {"count": 20, "window_days": 2}})
    assert found["status"] == "applied"
    assert env.host()["developers"] == 3
    assert env.host()["email"]["rate_limit"] == {"count": 20, "window_days": 2}
    after = path.stat()
    assert (after.st_ino, after.st_mode & 0o777) == (before.st_ino, 0o640)
    assert not [p for p in path.parent.iterdir() if p.name.startswith(".team.host.json")]


def test_a_refused_change_is_put_back_in_place_too(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = env.path.read_bytes()
    inode = env.path.stat().st_ino

    def refuse(*_: Any) -> None:
        raise PermissionError("the directory is not writable")

    monkeypatch.setattr(env.module.os, "replace", refuse)
    env.refuse = "PUT"
    with pytest.raises(env.module.ApplyError, match="the old settings are back"):
        env.confirmed(CHANGE)
    assert env.path.read_bytes() == original and env.path.stat().st_ino == inode
    assert [e["outcome"] for e in env.audit()] == ["started", "refused"]


def test_a_host_file_that_cannot_be_written_at_all_is_reported_and_left_alone(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = env.path.read_bytes()

    def refuse(*_: Any) -> None:
        raise PermissionError("no")

    real_open = Path.open

    def open_path(self: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if mode == "r+b":
            raise PermissionError("no")
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(env.module.os, "replace", refuse)
    monkeypatch.setattr(Path, "open", open_path)
    with pytest.raises(env.module.ApplyError, match="PermissionError"):
        env.confirmed({"developers": 3})
    assert env.path.read_bytes() == original
