"""Confirm and apply of team settings (deploy/team-kit/teamapply.py and the dashboard's
/api/settings routes): no change without the preview's confirm token, applied through the kit's
provision and apply, audited privately, idempotent, resume refused, secrets never logged, and
authority left to the kernel's live check."""

import importlib.util
import json
import logging
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from test_dashboard import KIT, Supervisor, dashboard
from test_dashboard import call as raw_call
from test_mailboxes import kernel, session
from test_team_kit import host as kernel_host
from test_team_kit import team
from test_team_settings import host_file, secret

from cardea.service.client import ClientError

__all__ = ["kernel", "session", "team", "host_file"]  # the fixtures the tests below ask for

WANTED = {"run_limits": {"roles": {"dev": 15}}, "developers": 3}


class Kernelish:
    """A kernel client that records its requests, or refuses them as the kernel would."""

    def __init__(self, refuse: ClientError | None = None) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.refuse = refuse

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        self.calls.append((method, path, kwargs.get("params")))
        if self.refuse:
            raise self.refuse
        return {}

    def close(self) -> None:
        pass


class Steps:
    """The kit's provision and apply, recorded; `fail` makes the named step fail."""

    def __init__(self, kernel: Kernelish, fail: str | None = None) -> None:
        self.kernel, self.fail, self.log = kernel, fail, []
        self.seen: list[dict[str, Any]] = []

    def provision(self, host: dict[str, Any], *, paused: bool) -> None:
        self.log.append(f"provision paused={paused}")
        self.seen.append(host)
        if self.fail == "provision" and len(self.log) == 1:
            raise RuntimeError("provision failed")

    def pause(self, host: dict[str, Any], reason: str | None = None) -> list[str]:
        self.log.append(f"pause {reason}")
        return []

    def apply(self, host: dict[str, Any], connect: Any) -> list[str]:
        self.log.append("apply")
        if self.fail == "apply":
            raise ClientError("kernel_unavailable", 502)
        return []


def teamapply() -> ModuleType:
    spec = importlib.util.spec_from_file_location("teamapply", KIT / "teamapply.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make(
    host_file: Path, refuse: ClientError | None = None, fail: str | None = None
) -> tuple[Any, Steps, Kernelish]:
    kernel_ = Kernelish(refuse)
    steps = Steps(kernel_, fail)
    settings = teamapply().TeamSettings(
        host_file,
        lambda host: kernel_,
        provision=steps.provision,
        pause=steps.pause,
        apply_team=steps.apply,
    )
    return settings, steps, kernel_


def audit_lines(host_file: Path) -> list[dict[str, Any]]:
    path = host_file.parent / "dev-team.settings-audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def refused(settings: Any, body: Any, confirm: Any, code: str) -> None:
    with pytest.raises(teamapply().ApplyError) as raised:
        settings.apply(body, confirm)
    assert raised.value.code == code


def test_a_preview_offers_a_confirm_token_and_changes_nothing(host_file: Path) -> None:
    settings, steps, kernel_ = make(host_file)
    before = host_file.read_bytes()
    result = settings.preview(WANTED)
    assert result["confirm"] and len(result["changes"]) == 2
    assert settings.preview({"developers": 2})["confirm"] is None  # nothing to confirm
    assert host_file.read_bytes() == before and not steps.log and not kernel_.calls
    assert audit_lines(host_file) == []


def test_nothing_changes_without_the_previews_confirm_token(host_file: Path) -> None:
    settings, steps, kernel_ = make(host_file)
    before = host_file.read_bytes()
    token = settings.preview(WANTED)["confirm"]
    for confirm in (None, "", "yes", True, token[::-1]):
        refused(settings, WANTED, confirm, "confirmation_required")
    # A token for one change does not confirm another.
    refused(settings, {"developers": 4}, token, "confirmation_required")
    assert host_file.read_bytes() == before and not steps.log and not kernel_.calls
    assert audit_lines(host_file) == []


def test_a_confirmed_change_goes_through_provision_and_apply_and_is_audited(
    host_file: Path,
) -> None:
    settings, steps, kernel_ = make(host_file)
    token = settings.preview(WANTED)["confirm"]
    result = settings.apply(WANTED, token)
    assert result["change"] == "applied"
    document = json.loads(host_file.read_text())
    assert document["developers"] == 3 and document["budgets"]["roles"]["dev"]["runs"] == 15
    assert document["credentials"]["author"] == "github"  # the rest of the file is kept
    assert steps.log == ["provision paused=False", "apply"]
    assert steps.seen[0]["developers"] == 3  # provisioned from the new file
    # The kernel's authority was asked first, as a dry run that changes nothing.
    assert kernel_.calls == [("PUT", "/groups/dev-team", {"dry_run": True})]
    (entry,) = audit_lines(host_file)
    assert entry["who"] == "sup-dev" and entry["outcome"] == "applied" and entry["at"]
    changes = {c["field"]: (c["from"], c["to"]) for c in entry["changes"]}
    assert changes == {"developers": (2, 3), "run_limits.roles.dev": (14, 15)}
    audit = host_file.parent / "dev-team.settings-audit.jsonl"
    assert audit.stat().st_mode & 0o777 == 0o600
    assert not [p for p in host_file.parent.iterdir() if p.name.startswith(".")]


def test_applying_the_same_change_again_writes_and_audits_nothing(host_file: Path) -> None:
    settings, steps, _ = make(host_file)
    settings.apply(WANTED, settings.preview(WANTED)["confirm"])
    steps.log.clear()
    log = host_file.parent / "dev-team.settings-audit.jsonl"
    written, audited, kept = host_file.stat().st_mtime_ns, log.read_bytes(), host_file.read_bytes()
    again = settings.apply(WANTED, None)  # nothing to confirm: no change, so no token needed
    assert again["change"] == "unchanged" and again["changes"] == []
    assert host_file.read_bytes() == kept and host_file.stat().st_mtime_ns == written
    assert log.read_bytes() == audited and steps.log == []


def test_resume_is_refused_here(host_file: Path, tmp_path: Path) -> None:
    settings, steps, kernel_ = make(host_file)
    record = tmp_path / "dev-team.pause.json"
    record.write_text(json.dumps({"reason": "freeze"}))
    resume = {"paused": {"paused": False}}
    token = settings.preview(resume)["confirm"]
    assert token  # the preview shows it, the apply refuses it
    for confirm in (token, None):
        refused(settings, resume, confirm, "resume_by_host_owner")
    refused(settings, {**resume, "developers": 3}, token, "resume_by_host_owner")
    assert json.loads(record.read_text()) == {"reason": "freeze"}
    assert not steps.log and not kernel_.calls and audit_lines(host_file) == []


def test_a_paused_team_is_not_changed_here(host_file: Path, tmp_path: Path) -> None:
    settings, steps, kernel_ = make(host_file)
    token = settings.preview(WANTED)["confirm"]
    (tmp_path / "dev-team.pause.json").write_text(json.dumps({"reason": "freeze"}))
    before = host_file.read_bytes()
    refused(settings, WANTED, token, "team_paused")
    assert host_file.read_bytes() == before and not steps.log and not kernel_.calls


def test_a_pause_goes_through_the_kits_pause(host_file: Path) -> None:
    settings, steps, _ = make(host_file)
    body = {"paused": {"paused": True, "reason": "release freeze"}}
    assert settings.apply(body, settings.preview(body)["confirm"])["change"] == "applied"
    assert steps.log == ["pause release freeze"]
    assert audit_lines(host_file)[0]["changes"][0]["to"] is True


def test_a_refusal_by_the_kernel_changes_and_audits_nothing(host_file: Path) -> None:
    denied = ClientError("group_supervisor", 403)
    settings, steps, _ = make(host_file, refuse=denied)
    before = host_file.read_bytes()
    with pytest.raises(ClientError) as raised:
        settings.apply(WANTED, settings.preview(WANTED)["confirm"])
    assert raised.value.code == "group_supervisor"
    assert host_file.read_bytes() == before and not steps.log
    assert audit_lines(host_file) == []


@pytest.mark.parametrize("fail", ["provision", "apply"])
def test_a_failed_step_puts_the_host_file_back_and_says_so_in_the_audit(
    host_file: Path, fail: str
) -> None:
    settings, steps, _ = make(host_file, fail=fail)
    before = host_file.read_bytes()
    refused(settings, WANTED, settings.preview(WANTED)["confirm"], "apply_failed")
    assert host_file.read_bytes() == before
    assert [e["outcome"] for e in audit_lines(host_file)] == ["reverted"]
    assert steps.log[-1].startswith("provision")  # provisioned again from the restored file
    # The same change can be confirmed again once the cause is fixed.
    steps.fail = None
    assert settings.apply(WANTED, settings.preview(WANTED)["confirm"])["change"] == "applied"


def test_a_change_the_host_file_would_refuse_is_not_applied(host_file: Path) -> None:
    settings, steps, _ = make(host_file)
    body = {"run_limits": {"roles": {"dev": 31}}}
    with pytest.raises(teamapply().ApplyError) as raised:
        settings.apply(body, settings.preview(body)["confirm"])
    assert raised.value.code == "settings_refused" and "exceeds" in str(raised.value.detail)
    assert not steps.log


def test_the_audit_keeps_goals_by_size_and_digest(host_file: Path) -> None:
    settings, _, _ = make(host_file)
    body = {"product_goals": {"goals": "Ship the thing."}}
    settings.apply(body, settings.preview(body)["confirm"])
    text = (host_file.parent / "dev-team.settings-audit.jsonl").read_text()
    assert "Ship the thing." not in text and "Original goals." not in text
    (entry,) = audit_lines(host_file)
    goals = next(c for c in entry["changes"] if c["field"] == "product_goals.goals")
    assert goals["to"]["bytes"] == 15 and len(goals["to"]["sha256"]) == 64


def test_secrets_are_never_logged_audited_or_returned(
    host_file: Path, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    settings, steps, kernel_ = make(host_file)
    seen: list[str] = []
    # A secret as a setting's name or value is refused without being echoed.
    for body in (
        {"credentials": {"token": secret()}},
        {"model": secret() + " bad model"},
        {"developers": secret()},
        {"paused": {"paused": True, "reason": "x\x00" + secret()}},
    ):
        with pytest.raises(teamapply().ApplyError) as raised:
            settings.apply(body, "x")
        seen += [str(raised.value), repr(raised.value.detail)]
    # A refused and an applied change, with the host file holding secrets all along.
    refused(settings, WANTED, "wrong", "confirmation_required")
    result = settings.apply(WANTED, settings.preview(WANTED)["confirm"])
    seen += [json.dumps(result), json.dumps(settings.preview(WANTED)), json.dumps(kernel_.calls)]
    printed = capsys.readouterr()
    seen += [caplog.text, printed.out, printed.err]
    seen += [(host_file.parent / "dev-team.settings-audit.jsonl").read_text()]
    assert secret() not in "\n".join(seen) and "reviewer_login" not in "\n".join(seen)
    assert "team settings applied: developers, run_limits.roles.dev" in caplog.text
    # The host file keeps its secrets, privately and untouched.
    assert json.loads(host_file.read_text())["credentials"]["reviewer_login"] == secret()


def test_the_kernel_checks_authority_live_on_the_real_group(
    team: tuple[Any, Any], tmp_path: Path
) -> None:
    """A dry-run PUT of the new group: the supervisor passes, anyone else is refused."""
    kernel_, _ = team
    document = kernel_host()
    document["provisioning"] = {"out": str(tmp_path / "provision.json")}
    document["credentials"]["reviewer_login"] = "reviewer-login"  # not the example's placeholder
    path = tmp_path / "team.host.json"
    path.write_text(json.dumps(document))
    steps_for = {}

    def connect_as(principal: str) -> Any:
        class Client(Supervisor):
            def close(self) -> None:
                pass

        steps_for[principal] = Client(kernel_, principal)
        return lambda host: steps_for[principal]

    def settings_as(principal: str) -> Any:
        steps = Steps(Kernelish())
        return (
            teamapply().TeamSettings(
                path,
                connect_as(principal),
                provision=steps.provision,
                pause=steps.pause,
                apply_team=steps.apply,
            ),
            steps,
        )

    body = {"run_limits": {"roles": {"dev": 3}}}
    other, other_steps = settings_as("not-the-supervisor")
    before = path.read_bytes()
    with pytest.raises(ClientError) as raised:
        other.apply(body, other.preview(body)["confirm"])
    assert raised.value.status in (401, 403)
    assert path.read_bytes() == before and not other_steps.log
    supervisor, steps = settings_as("sup-dev")
    assert supervisor.apply(body, supervisor.preview(body)["confirm"])["change"] == "applied"
    assert json.loads(path.read_text())["budgets"]["roles"]["dev"]["runs"] == 3
    assert steps.log == ["provision paused=False", "apply"]


# The dashboard's routes


def served(host_file: Path, **options: Any) -> tuple[Any, Steps, Kernelish]:
    settings, steps, kernel_ = make(host_file, **options)
    backend = dashboard().KernelBackend(kernel_.request, "dev", "dev-team", settings)
    return backend, steps, kernel_


def post(backend: Any, path: str, body: Any, **extra: Any) -> tuple[int, Any]:
    return raw_call(backend, "POST", path, body, **extra)


@pytest.mark.loopback
def test_preview_and_apply_over_http(host_file: Path) -> None:
    backend, steps, _ = served(host_file)
    status, shown = post(backend, "/api/settings/preview", {"settings": WANTED})
    assert status == 200 and shown["confirm"] and not steps.log
    assert json.loads(host_file.read_text())["developers"] == 2
    status, answer = post(backend, "/api/settings/apply", {"settings": WANTED, "confirm": None})
    assert (status, answer["code"]) == (428, "confirmation_required")
    assert json.loads(host_file.read_text())["developers"] == 2
    body = {"settings": WANTED, "confirm": shown["confirm"]}
    status, done = post(backend, "/api/settings/apply", body)
    assert status == 200 and done["change"] == "applied"
    assert json.loads(host_file.read_text())["developers"] == 3
    assert post(backend, "/api/settings/apply", body)[1]["change"] == "unchanged"
    assert len(audit_lines(host_file)) == 1


@pytest.mark.loopback
def test_http_refuses_resume_and_bad_settings(host_file: Path) -> None:
    backend, steps, _ = served(host_file)
    resume = {"settings": {"paused": {"paused": False}}, "confirm": "x"}
    status, answer = post(backend, "/api/settings/apply", resume)
    assert (status, answer) == (403, {"code": "resume_by_host_owner"})
    status, answer = post(backend, "/api/settings/preview", {"settings": {"developers": 0}})
    assert status == 422 and answer["code"] == "invalid_settings"
    assert "developers" in answer["detail"]
    assert not steps.log


@pytest.mark.loopback
def test_http_applies_the_same_guards_as_the_message_box(host_file: Path) -> None:
    backend, steps, _ = served(host_file)
    body = {"settings": WANTED}
    path = "/api/settings/preview"
    assert post(backend, path, body, headers={"Origin": "http://evil.example"})[0] == 403
    assert post(backend, path, body, headers={"Host": "evil.example"})[0] == 403
    assert post(backend, path, body, headers={"Content-Type": "text/plain"})[0] == 415
    assert post(backend, path, {"settings": WANTED, "extra": 1})[0] == 400
    assert post(backend, "/api/settings/apply", body)[0] == 400  # no confirm key at all
    assert raw_call(backend, "GET", path)[0] in (404, 501)
    assert not steps.log


@pytest.mark.loopback
def test_http_without_settings_wired_is_not_found(host_file: Path) -> None:
    fixture = json.loads((KIT / "dashboard.fixture.json").read_text())
    for backend in (
        dashboard().FixtureBackend(fixture),
        dashboard().KernelBackend(Kernelish().request, "dev", "dev-team"),
    ):
        for path in ("/api/settings/preview", "/api/settings/apply"):
            body = {"settings": WANTED, "confirm": "x"}
            status, answer = post(backend, path, body if "apply" in path else {"settings": WANTED})
            assert (status, answer["code"]) == (404, "not_found")
