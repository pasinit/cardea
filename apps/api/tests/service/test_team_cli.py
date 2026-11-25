"""`cardea team` (deploy/team-kit/teamcli.py): every command on the helper side (as the kernel's
account, against a fake kernel) and on the front end (the person's command line, confirmation,
files), the sudo hand-off, and the package files that make it work for a plain user."""

import copy
import io
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from test_team_schema import KIT, load
from test_team_settings import SETTING_FILES, fixture, secret
from test_team_settings import host_file as host_file  # noqa: F401  (fixture)
from test_team_settings_apply import Env
from test_team_settings_apply import env as env  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[4]
DEBIAN = ROOT / "packaging" / "debian"
GROUP = "dev-team"
CLIENT = "-".join(["sentinel", "client", "key"])


def cli() -> ModuleType:
    return load("teamcli")


def overview() -> dict[str, Any]:
    view = json.loads((KIT / "dashboard.fixture.json").read_text())
    members = {"pm": 1, "tl": 1, "dev": 2, "pe": 1, "ux": 1}
    view["pools"] = [
        {
            "name": pool,
            "members": [
                {
                    "principal_id": f"{GROUP}.{pool}.{n}",
                    "disabled": False,
                    "active_task_id": None,
                    "claims": [],
                    "last_run": None,
                }
                for n in range(count)
            ],
        }
        for pool, count in members.items()
    ]
    return dict(view)


class Kernel:
    """A kernel that answers the calls the commands make and keeps a log of them."""

    def __init__(self) -> None:
        self.log: list[tuple[str, str, dict[str, Any]]] = []
        self.fail: dict[str, Exception] = {}
        self.page: dict[str, Any] = {"total": 0, "items": [], "next": None}

    def request(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        self.log.append((method, path, kw))
        if method in self.fail:
            raise self.fail[method]
        if path.endswith("/overview"):
            return overview()
        if path == "/mailbox":
            return {"message_id": "m-1"}
        if path.endswith("/status/mail/text"):
            return self.page
        return {}

    def close(self) -> None:
        pass

    def calls(self, method: str) -> list[tuple[str, dict[str, Any]]]:
        return [(p, kw) for m, p, kw in self.log if m == method]


class Fixture:
    def __init__(self, env: Env, kernel: Kernel, team: Any) -> None:
        self.env, self.kernel, self.team = env, kernel, team

    def run(self, command: str, **fields: Any) -> dict[str, Any]:
        reply = self.team.run({"command": command, **fields}).document()
        assert json.loads(json.dumps(reply)) == reply  # the helper's reply travels as JSON
        return dict(reply)

    def text(self, reply: dict[str, Any]) -> str:
        return "\n".join([*reply["out"], *reply["err"]])


@pytest.fixture
def team(env: Env, monkeypatch: pytest.MonkeyPatch) -> Fixture:  # noqa: F811
    kernel = Kernel()
    monkeypatch.setattr(env.kit, "client", lambda host: kernel)
    return Fixture(env, kernel, cli().Team(env.path, by="alex", connect=env.kit.client))


# --- the helper's commands -----------------------------------------------------------------


def test_status_prints_the_overview_and_never_the_credentials(team: Fixture) -> None:
    reply = team.run("status")
    text = team.text(reply)
    assert reply["exit"] == 0 and f"{GROUP}.dev.1" in text and "spend" in text
    assert "github" in text and "2027-01-31" in text  # token names and dates, as `status` has
    assert secret() not in text and "/run/" not in text


def test_status_of_a_paused_team_says_why(team: Fixture) -> None:
    from cardea.service.client import ClientError

    team.run("pause", reason="release freeze")
    team.kernel.fail["GET"] = ClientError("principal_disabled", 403)
    text = team.text(team.run("status"))
    assert "team paused" in text and "release freeze" in text


def test_pause_disables_the_supervisor_and_keeps_the_reason(team: Fixture) -> None:
    reply = team.run("pause", reason="release\x1b[2J freeze")
    assert reply["exit"] == 0 and "cancelled its 0 active run(s)" in team.text(reply)
    assert [paused for _, paused in team.env.provisioned] == []  # kit.pause is the fake here
    assert team.env.paused_with == ["release\x1b[2J freeze"]
    assert team.run("pause")["exit"] == 0 and team.env.paused_with[-1] is None


def test_resume_enables_the_supervisor_and_removes_the_pause_record(team: Fixture) -> None:
    team.run("pause", reason="x")
    reply = team.run("resume")
    assert reply["exit"] == 0 and [p for _, p in team.env.provisioned] == [False]
    assert not team.env.kit.read_pause_record(team.env.kit.load_host(team.env.path))


def test_stop_deletes_the_group_then_disables_the_supervisor(
    team: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        team.env.kit, "provision", lambda host, *, paused, **_: calls.append(f"provision {paused}")
    )
    reply = team.run("stop")
    assert reply["exit"] == 0 and "group deleted" in team.text(reply)
    assert team.kernel.calls("DELETE")[0][0] == f"/groups/{GROUP}"
    assert calls == ["provision True"]


def test_stop_reports_a_refused_deletion_but_still_disables(
    team: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        team.env.kit, "provision", lambda host, *, paused, **_: calls.append(f"provision {paused}")
    )
    team.kernel.fail["DELETE"] = PermissionError(f"denied for {secret()}")
    reply = team.run("stop")
    assert reply["exit"] == 1 and calls == ["provision True"]
    assert "not deleted" in team.text(reply) and secret() not in team.text(reply)


def test_apply_puts_the_collections_and_the_group(team: Fixture) -> None:
    reply = team.run("apply")
    assert reply["exit"] == 0
    assert [p for p, _ in team.kernel.calls("PUT")] == [
        "/collections/work",
        "/collections/decisions",
        f"/groups/{GROUP}",
    ]
    assert "group dev-team in dev" in team.text(reply)


def test_provision_merges_the_team_into_the_base_document(team: Fixture) -> None:
    reply = team.run("provision", paused=True)
    assert reply["exit"] == 0 and [p for _, p in team.env.provisioned] == [True]
    assert "supervisor disabled" in team.text(reply)
    assert team.run("provision")["exit"] == 0 and team.env.provisioned[-1][1] is False


def test_render_returns_the_documents_without_writing_anywhere(team: Fixture) -> None:
    before = sorted(p.name for p in team.env.path.parent.iterdir())
    reply = team.run("render")
    assert reply["exit"] == 0
    assert set(reply["files"]) >= {"group.json", "collection.work.json", "provisioning.team.json"}
    assert json.loads(reply["files"]["group.json"])["metadata"]["name"] == GROUP
    assert sorted(p.name for p in team.env.path.parent.iterdir()) == before
    assert "/run/" not in json.dumps(reply)  # the kernel socket is not in any document


def test_settings_are_in_plain_words_and_hold_no_credential(team: Fixture) -> None:
    text = team.text(team.run("settings"))
    assert "Team dev-team in namespace dev" in text and "branch main" in text
    assert "Developers: 2. Model: claude-sonnet-4-5." in text
    assert "the whole team 30 runs" in text and "20.0 USD" in text
    assert "pm: 0 9 * * 1-5" in text and "dev: none" in text
    assert "at most 5 every 2 day(s)" in text
    assert "Product goals: goals 15 bytes, non-goals 19 bytes" in text
    assert "Original goals" not in text  # goals are shown by size
    assert secret() not in text and "github" not in text and "/run/" not in text
    team.run("pause", reason="freeze")
    assert "The team is paused: freeze." in team.text(team.run("settings"))


@pytest.mark.parametrize(
    ("key", "value", "field", "after"),
    [
        ("runs.team", "40", "run_limits.team", 40),
        ("runs.dev", "20", "run_limits.roles.dev", 20),
        ("usd.team", "25.5", "spend_limits.team_usd", 25.5),
        ("usd.per_run", "2", "spend_limits.per_run_usd", 2.0),
        ("usd.dev", "10", "spend_limits.roles_usd.dev", 10.0),
        (
            "schedule.pm",
            "0 10 * * 1-5; 0 16 * * 1-5",
            "schedules.pm",
            ["0 10 * * 1-5", "0 16 * * 1-5"],
        ),
        ("schedule.pm", "none", "schedules.pm", []),
        ("email.rate_limit", "10/1", "email_rate_limit.count", 10),
        ("email.rate_limit", "7/7d", "email_rate_limit.window_days", 7),
        ("email.rate_limit", "3/day", "email_rate_limit.count", 3),
        ("developers", "4", "developers", 4),
        ("model", "claude-opus-5-5", "model", "claude-opus-5-5"),
        ("goals", "New goals.", "product_goals.goals", "New goals."),
        ("non_goals", "No chat.", "product_goals.non_goals", "No chat."),
    ],
)
def test_set_previews_then_applies_exactly_the_previewed_change(
    team: Fixture, key: str, value: str, field: str, after: Any
) -> None:
    before = team.env.path.read_bytes()
    shown = team.run("set", key=key, value=value)
    assert shown["exit"] == 0 and shown["status"] == "needs_confirmation"
    assert re.fullmatch("[0-9a-f]{64}", shown["confirm"])
    assert (
        team.env.path.read_bytes() == before and team.env.audit() == []
    )  # a preview writes nothing
    assert team.kernel.log == [] and team.env.provisioned == []
    done = team.run("set", key=key, value=value, confirm=shown["confirm"])
    assert done["exit"] == 0 and done["status"] == "applied"
    assert [e["outcome"] for e in team.env.audit()] == ["started", "applied"]
    assert team.env.audit()[0]["by"] == "alex"  # who: the caller, not the supervisor
    host = team.env.host()
    flat = {
        "run_limits.team": host["budgets"]["team"]["runs"],
        "run_limits.roles.dev": host["budgets"]["roles"]["dev"]["runs"],
        "spend_limits.team_usd": host["budgets"]["team"]["usd"],
        "spend_limits.per_run_usd": host["budgets"]["max_cost_usd_per_run"],
        "spend_limits.roles_usd.dev": host["budgets"]["roles"]["dev"].get("usd"),
        "schedules.pm": host["schedules"]["pm"],
        "email_rate_limit.count": host["email"]["rate_limit"]["count"],
        "email_rate_limit.window_days": host["email"]["rate_limit"]["window_days"],
        "developers": host["developers"],
        "model": host["model"],
        "product_goals.goals": host["product"]["goals"],
        "product_goals.non_goals": host["product"].get("non_goals"),
    }
    assert flat[field] == after
    assert team.run("set", key=key, value=value)["status"] == "unchanged"


def test_setting_goals_keeps_the_non_goals_and_the_reverse(team: Fixture) -> None:
    shown = team.run("set", key="goals", value="Fresh goals.")
    team.run("set", key="goals", value="Fresh goals.", confirm=shown["confirm"])
    assert team.env.host()["product"] == {
        "goals": "Fresh goals.",
        "non_goals": "Original non-goals.",
    }


def test_set_applies_the_same_validation_as_the_dashboard(
    team: Fixture,
    env: Env,  # noqa: F811
) -> None:
    for name in ("developers", "run_limits", "email_rate_limit", "schedules"):
        assert name in SETTING_FILES
    bad = [
        ("developers", "0"),
        ("developers", "65"),
        ("developers", "two"),
        ("runs.team", "0"),
        ("runs.ux", "3"),  # the dashboard's settings do not list ux either
        ("runs.nobody", "3"),
        ("usd.team", "-1"),
        ("usd.team", "lots"),
        ("schedule.pm", "soon"),
        ("schedule.pm", "0 9 * * *; 0 9 * * *"),
        ("email.rate_limit", "0/1"),
        ("email.rate_limit", "1001/1"),
        ("email.rate_limit", "10/91"),
        ("email.rate_limit", "ten"),
        ("model", "bad name!"),
        ("goals", "   "),
        ("unknown.key", "1"),
        ("credentials", "token"),
    ]
    before = team.env.path.read_bytes()
    for key, value in bad:
        reply = team.run("set", key=key, value=value)
        assert reply["exit"] == 1 and reply["err"][0].startswith("refused:"), (key, value)
        assert reply["status"] is None
    assert team.env.path.read_bytes() == before and team.env.provisioned == []


def test_set_refuses_what_the_host_cannot_take(team: Fixture) -> None:
    host = team.env.host()
    del host["email"]
    team.env.path.write_text(json.dumps(host))
    reply = team.run("set", key="email.rate_limit", value="10/1")
    assert reply["exit"] == 1 and "needs an email alias" in team.text(reply)
    host["product"] = {}
    team.env.path.write_text(json.dumps({k: v for k, v in host.items() if k != "product"}))
    assert "set goals first" in team.text(team.run("set", key="non_goals", value="x"))


def test_a_stale_token_changes_nothing(team: Fixture) -> None:
    shown = team.run("set", key="developers", value="3")
    other = team.run("set", key="developers", value="4")
    reply = team.run("set", key="developers", value="3", confirm=other["confirm"])
    assert reply["status"] == "needs_confirmation" and team.env.host()["developers"] == 2
    assert (
        team.run("set", key="developers", value="3", confirm=shown["confirm"])["status"]
        == "applied"
    )


def test_a_kernel_refusal_restores_the_old_settings_and_leaks_nothing(team: Fixture) -> None:
    team.env.refuse = "PUT"
    team.kernel.fail["PUT"] = PermissionError(f"denied for {secret()}")
    shown = team.run("set", key="developers", value="3")
    reply = team.run("set", key="developers", value="3", confirm=shown["confirm"])
    assert reply["exit"] == 1 and "old settings are back" in team.text(reply)
    assert team.env.host()["developers"] == 2 and secret() not in json.dumps(reply)


def test_mail_to_a_role_goes_to_its_pool(team: Fixture) -> None:
    for role in ("tl", "pm", "pe", "ux"):
        reply = team.run("mail", target=role, text="Please look at the release.", client_key=CLIENT)
        assert reply["exit"] == 0 and f"sent to {role} (message m-1)" in team.text(reply)
        path, kw = team.kernel.calls("POST")[-1]
        assert path == "/mailbox" and kw["json"] == {
            "namespace": "dev",
            "client_key": CLIENT,
            "to": {"pool": f"{GROUP}.{role}"},
            "parts": [{"text": "Please look at the release."}],
        }


def test_mail_to_a_developer_goes_to_that_member(team: Fixture) -> None:
    reply = team.run("mail", target="dev.1", text="Rebase first.", client_key=CLIENT)
    assert reply["exit"] == 0
    assert team.kernel.calls("POST")[-1][1]["json"]["to"] == {"instance": f"{GROUP}.dev.1"}


@pytest.mark.parametrize(
    ("target", "text", "why"),
    [
        ("dev.2", "hi", "no developer dev.2"),
        ("dev.01", "hi", "tl, pm, pe, ux or dev.N"),
        ("dev", "hi", "tl, pm, pe, ux or dev.N"),
        ("ceo", "hi", "tl, pm, pe, ux or dev.N"),
        ("pm", "  ", "1 to 4000 characters"),
        ("pm", "x" * 4001, "1 to 4000 characters"),
        ("pm", "bad\x1b[31m", "control characters"),
    ],
)
def test_a_bad_mail_is_refused_before_anything_is_sent(
    team: Fixture, target: str, text: str, why: str
) -> None:
    reply = team.run("mail", target=target, text=text, client_key=CLIENT)
    assert reply["exit"] == 1 and why in team.text(reply)
    assert team.kernel.calls("POST") == []


def test_the_kernels_refusal_of_mail_is_reported_by_code_only(team: Fixture) -> None:
    from cardea.service.client import ClientError

    team.kernel.fail["POST"] = ClientError("permission_denied", 403)
    reply = team.run("mail", target="pm", text="hi", client_key=CLIENT)
    assert reply["exit"] == 1 and "ClientError (permission_denied)" in team.text(reply)


def test_mail_list_shows_the_team_mail_as_plain_text(team: Fixture) -> None:
    team.kernel.page = {
        "total": 3,
        "next": "0" * 8 + "-0000-0000-0000-" + "0" * 12,
        "items": [
            {
                "message_id": "m-9",
                "sent_at": "2026-11-23T09:00:00Z",
                "from": {"address": f"{GROUP}.pm.0", "role": "pm"},
                "to": {"address": f"{GROUP}.tl", "role": "tl"},
                "item": {"collection": "work", "record_id": "r1"},
                "parts": [{"text": "Line one\n\x1b[2Jline two\r‮evil"}, {"data": {"a": 1}}],
            }
        ],
    }
    reply = team.run("mail_list", member=f"{GROUP}.pm.0", item="work/r1", limit=5)
    assert reply["exit"] == 0
    text = team.text(reply)
    assert "1 of 3 message(s), newest first" in text
    assert f"{GROUP}.pm.0 -> {GROUP}.tl about work/r1  [m-9]" in text
    assert (
        "    Line one" in text
        and "[non-text part]" in text
        and "more: cardea team mail list --after" in text
    )
    assert not re.search("[\x1b\r‮]", json.dumps(reply, ensure_ascii=False))
    path, kw = team.kernel.calls("GET")[-1]
    assert path == f"/groups/{GROUP}/status/mail/text"
    assert kw["params"] == {
        "namespace": "dev",
        "member": f"{GROUP}.pm.0",
        "item": "work/r1",
        "limit": 5,
    }


@pytest.mark.parametrize(
    "fields", [{"member": "bad id"}, {"item": "nope"}, {"after": "x"}, {"limit": 0}, {"limit": 51}]
)
def test_mail_list_checks_its_filters(team: Fixture, fields: dict[str, Any]) -> None:
    reply = team.run("mail_list", **fields)
    assert reply["exit"] == 1 and not [c for c in team.kernel.log if "mail/text" in c[1]]


@pytest.mark.parametrize(
    "request_",
    [
        None,
        [],
        {},
        {"command": 7},
        {"command": "reboot"},
        {"command": "status", "host": "/etc/shadow"},
        {"command": "set", "key": "developers"},
        {"command": "set", "key": 1, "value": "2"},
        {"command": "mail", "target": "pm", "text": "hi"},
        {"command": "provision", "paused": "yes"},
        {"command": "mail_list", "limit": True},
        {"command": "pause", "reason": "x" * 2000},
        {"command": "set", "key": "model", "value": "a" * 10000},
    ],
)
def test_the_helper_refuses_a_request_that_is_not_exactly_a_command(
    team: Fixture, request_: object
) -> None:
    reply = cli().Team.run(team.team, request_).document()
    assert reply["exit"] == 1 and reply["err"][0].startswith("refused:")
    assert team.kernel.log == [] and team.env.provisioned == []


def test_a_failure_is_reported_by_type_and_code_never_by_message(
    team: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(host: dict[str, Any]) -> list[str]:
        raise RuntimeError(f"leaks {secret()}")

    monkeypatch.setattr(team.env.kit, "status", boom)
    reply = team.run("status")
    assert reply["err"] == ["refused: RuntimeError"] and secret() not in json.dumps(reply)


def test_an_unreadable_or_broken_host_file_is_named_without_its_content(
    team: Fixture, tmp_path: Path
) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text(f'{{"namespace": "{secret()}",')
    reply = cli().Team(broken).run({"command": "status"}).document()
    assert "is not valid JSON" in reply["err"][0] and secret() not in reply["err"][0]
    missing = cli().Team(tmp_path / "none.json").run({"command": "settings"}).document()
    assert "cannot read the host file" in missing["err"][0]


def test_terminal_control_characters_never_leave_the_helper() -> None:
    result = cli().Result()
    result.out.append("ok \x1b[31mred\x07 ‮")
    result.err.append("\x9b0m")
    document = result.document()
    assert document["out"] == ["ok �[31mred� �"] and document["err"] == ["�0m"]


# --- the helper as the kernel's account ----------------------------------------------------


@pytest.fixture
def as_kernel(
    host_file: Path,
    monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> tuple[ModuleType, Path]:
    module = cli()
    monkeypatch.setattr(module, "kernel_uid", os.geteuid)
    monkeypatch.setattr(module, "DEFAULT_HOST", host_file)
    monkeypatch.setattr(module.pwd, "getpwnam", lambda name: os.stat(host_file))  # uid/gid
    return module, host_file


def helper(
    module: ModuleType, request: object, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> Any:
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(request).encode())))
    assert module.run_helper([]) == 0
    return json.loads(capsys.readouterr().out)


def test_the_helper_reads_one_request_and_uses_only_the_fixed_host_file(
    as_kernel: tuple[ModuleType, Path], monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    module, host_file = as_kernel  # noqa: F811
    monkeypatch.setenv("SUDO_USER", "alex\x1b[31m")
    reply = helper(module, {"command": "settings"}, monkeypatch, capsys)
    assert reply["exit"] == 0 and any("Team dev-team" in line for line in reply["out"])
    other = host_file.with_name("other.json")
    other.write_text(host_file.read_text())
    reply = helper(module, {"command": "settings", "host": str(other)}, monkeypatch, capsys)
    assert reply["exit"] == 1 and "does not have the fields" in reply["err"][0]


def test_the_helper_only_runs_as_the_kernels_account(
    as_kernel: tuple[ModuleType, Path], monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    module, _ = as_kernel
    monkeypatch.setattr(module, "kernel_uid", lambda: os.geteuid() + 1)
    reply = helper(module, {"command": "settings"}, monkeypatch, capsys)
    assert reply["exit"] == 1 and "runs as cardea-kernel" in reply["err"][0]


@pytest.mark.parametrize("raw", [b"", b"not json", b"[]", b"x" * 70000])
def test_the_helper_refuses_a_bad_stdin(
    as_kernel: tuple[ModuleType, Path], monkeypatch: pytest.MonkeyPatch, capsys: Any, raw: bytes
) -> None:
    module, _ = as_kernel
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(raw)))
    assert module.run_helper([]) == 0
    assert json.loads(capsys.readouterr().out)["exit"] == 1


def test_the_helper_refuses_an_unsafe_host_file(
    as_kernel: tuple[ModuleType, Path], monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    module, host_file = as_kernel  # noqa: F811
    host_file.chmod(0o666)
    reply = helper(module, {"command": "settings"}, monkeypatch, capsys)
    assert reply["exit"] == 1 and "world-writable" in reply["err"][0]
    host_file.chmod(0o600)
    link = host_file.with_name("link.json")
    link.symlink_to(host_file)
    monkeypatch.setattr(module, "DEFAULT_HOST", link)
    assert (
        "not a regular file"
        in helper(module, {"command": "settings"}, monkeypatch, capsys)["err"][0]
    )
    monkeypatch.setattr(module, "DEFAULT_HOST", host_file.with_name("absent.json"))
    assert (
        "cannot read the host file"
        in helper(module, {"command": "settings"}, monkeypatch, capsys)["err"][0]
    )


def test_the_default_host_file_is_the_documented_one() -> None:
    assert str(cli().DEFAULT_HOST) == "/etc/cardea/team.host.json"


# --- the person's side ---------------------------------------------------------------------


class Wire:
    """A stand-in for the sudo hand-off: requests in, the helper's replies out."""

    def __init__(self, team: Fixture) -> None:
        self.team, self.requests = team, []  # type: ignore[var-annotated]

    def __call__(self, request: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(request)
        return dict(self.team.team.run(request).document())


@pytest.fixture
def wire(team: Fixture) -> Wire:
    return Wire(team)


def ask_never(prompt: str) -> bool:
    raise AssertionError(f"asked: {prompt}")


def type_(wire: Wire, *argv: str, answer: bool | None = None) -> int:
    asked: list[str] = []

    def confirm(prompt: str) -> bool:
        asked.append(prompt)
        assert answer is not None, prompt
        return answer

    code: int = cli().main(list(argv), send=wire, confirm=confirm)
    wire.asked = asked  # type: ignore[attr-defined]
    return code


def test_help_lists_every_command_and_every_key(capsys: Any) -> None:
    module = cli()
    for argv in (["--help"], ["status", "--help"], []):
        assert module.main(argv) == 0
        text = capsys.readouterr().out
        for usage, _ in module.COMMANDS:
            assert usage in text
        for key, _, _ in module.SET_KEYS:
            assert key in text
    assert module.main(["fly"]) == 2 and "usage: cardea team" in capsys.readouterr().err


def test_the_man_page_lists_every_command_and_key() -> None:
    module = cli()
    page = (
        (DEBIAN / "cardea-team.1")
        .read_text()
        .replace("\\-", "-")
        .replace("\\fB", "")
        .replace("\\fR", "")
    )
    for usage, _ in module.COMMANDS:
        assert usage.split()[0] + (" list" if usage.startswith("mail list") else "") in page
    for key, _, _ in module.SET_KEYS:
        assert key.replace("<role>", "").split("<")[0].rstrip(".") in page.replace(
            "\\fI", ""
        ).replace("role", "")
    for name in (
        "cardea-operators",
        "/etc/cardea/team.host.json",
        "cardea-kernel",
        "--dry-run",
        "--yes",
    ):
        assert name in page


def test_each_plain_command_sends_one_request(wire: Wire) -> None:
    for command in ("status", "resume", "apply", "settings"):
        assert type_(wire, command) == 0 and wire.requests[-1] == {"command": command}
    assert type_(wire, "provision", "--paused") == 0
    assert wire.requests[-1] == {"command": "provision", "paused": True}
    assert type_(wire, "provision") == 0 and wire.requests[-1]["paused"] is False
    assert type_(wire, "pause", "release", "freeze") == 0
    assert wire.requests[-1] == {"command": "pause", "reason": "release freeze"}
    assert type_(wire, "pause") == 0 and wire.requests[-1] == {"command": "pause"}


def test_output_is_printed_and_a_failure_exits_nonzero(wire: Wire, capsys: Any) -> None:
    type_(wire, "status")
    assert "dev-team.dev.1" in capsys.readouterr().out
    wire.team.kernel.fail["GET"] = RuntimeError("x")
    assert type_(wire, "status") == 1 and "refused: RuntimeError" in capsys.readouterr().err


def test_stop_asks_first(wire: Wire, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    monkeypatch.setattr(wire.team.env.kit, "provision", lambda host, *, paused, **_: None)
    assert type_(wire, "stop", answer=False) == 1 and wire.requests == []
    assert "nothing changed" in capsys.readouterr().out and "Stop the team?" in wire.asked[0]  # type: ignore[attr-defined]
    assert type_(wire, "stop", "--yes") == 0 and wire.requests == [{"command": "stop"}]
    assert type_(wire, "stop", answer=True) == 0


def test_render_prints_the_documents_or_writes_private_files(
    wire: Wire, tmp_path: Path, capsys: Any
) -> None:
    assert type_(wire, "render") == 0
    shown = capsys.readouterr().out
    assert "==> group.json <==" in shown and "==> provisioning.team.json <==" in shown
    out = tmp_path / "review"
    assert type_(wire, "render", "--out", str(out)) == 0
    written = sorted(p.name for p in out.iterdir())
    assert "group.json" in written and all(
        (out / n).stat().st_mode & 0o777 == 0o600 for n in written
    )
    assert str(out / "group.json") in capsys.readouterr().out
    assert json.loads((out / "group.json").read_text())["metadata"]["name"] == GROUP


def test_render_files_are_written_as_the_person_and_never_through_a_link(tmp_path: Path) -> None:
    module = cli()
    out = tmp_path / "out"
    out.mkdir()
    (out / "group.json").symlink_to(tmp_path / "target")
    with pytest.raises(OSError):
        module.write_files({"group.json": "{}"}, out)
    assert not (tmp_path / "target").exists()
    with pytest.raises(module.Refusal):
        module.write_files({"../escape": "{}"}, out)


def test_set_shows_the_change_asks_and_then_applies(wire: Wire, capsys: Any) -> None:
    assert type_(wire, "set", "developers", "3", answer=True) == 0
    shown = capsys.readouterr().out
    assert "developers: 2 -> 3" in shown and shown.rstrip().endswith("applied")
    assert [r.get("confirm") is not None for r in wire.requests] == [False, True]
    assert wire.team.env.host()["developers"] == 3 and wire.asked == ["Apply this change? [y/N] "]  # type: ignore[attr-defined]


def test_set_declined_dry_run_and_yes(wire: Wire, capsys: Any) -> None:
    before = wire.team.env.path.read_bytes()
    assert type_(wire, "set", "developers", "3", answer=False) == 1
    assert "not applied; nothing changed" in capsys.readouterr().out
    assert type_(wire, "set", "--dry-run", "developers", "3") == 0
    assert "dry run" in capsys.readouterr().out and wire.team.env.path.read_bytes() == before
    assert type_(wire, "set", "--yes", "developers", "3") == 0
    assert wire.team.env.host()["developers"] == 3
    assert type_(wire, "set", "developers", "3") == 0
    assert "already in effect" in capsys.readouterr().out


def test_set_with_no_terminal_is_not_confirmed(wire: Wire, monkeypatch: pytest.MonkeyPatch) -> None:
    module = cli()
    monkeypatch.setattr(module, "ask", module.ask)
    monkeypatch.setattr("builtins.open", lambda *a, **k: (_ for _ in ()).throw(OSError("no tty")))
    assert module.ask("Apply? ") is False


def test_set_refused_by_validation_prints_the_field_and_asks_nothing(
    wire: Wire, capsys: Any
) -> None:
    assert type_(wire, "set", "developers", "0") == 1
    assert "developers must be a whole number" in capsys.readouterr().err
    assert len(wire.requests) == 1


def test_set_goals_reads_a_file_or_stdin_as_the_person(
    wire: Wire, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    goals = tmp_path / "goals.txt"
    goals.write_text("- Help owners.\n- Be fast.\n")
    assert type_(wire, "set", "--yes", "goals", f"@{goals}") == 0
    assert wire.team.env.host()["product"]["goals"] == "- Help owners.\n- Be fast."
    monkeypatch.setattr(sys, "stdin", io.StringIO("From stdin."))
    assert type_(wire, "set", "--yes", "goals", "-") == 0
    assert wire.team.env.host()["product"]["goals"] == "From stdin."
    assert type_(wire, "set", "--yes", "goals", str(tmp_path / "missing")) == 0  # plain text


def test_set_joins_unquoted_words(wire: Wire) -> None:
    assert type_(wire, "set", "--yes", "schedule.pm", "0", "10", "*", "*", "1-5") == 0
    assert wire.team.env.host()["schedules"]["pm"] == ["0 10 * * 1-5"]


def test_a_value_may_start_with_a_dash_after_double_dash(wire: Wire) -> None:
    assert type_(wire, "mail", "pm", "--", "-5", "degrees") == 0
    assert wire.requests[-1]["text"] == "-5 degrees"


def test_mail_sends_the_words_once_under_one_client_key(wire: Wire, capsys: Any) -> None:
    assert type_(wire, "mail", "dev.1", "Rebase", "first.") == 0
    request = wire.requests[-1]
    assert (request["target"], request["text"]) == ("dev.1", "Rebase first.")
    assert re.fullmatch("[0-9a-f]{32}", request["client_key"])
    assert "sent to dev.1" in capsys.readouterr().out
    assert type_(wire, "mail", "pm") == 1 and "needs a target and a text" in capsys.readouterr().err
    assert type_(wire, "mail", "pm", "hi") == 0
    assert wire.requests[-1]["client_key"] != request["client_key"]


def test_mail_list_passes_its_filters(wire: Wire) -> None:
    assert type_(wire, "mail", "list") == 0 and wire.requests[-1] == {"command": "mail_list"}
    assert type_(wire, "mail", "list", "--limit", "5", "--item", "work/r1") == 0
    assert wire.requests[-1] == {"command": "mail_list", "item": "work/r1", "limit": 5}


# --- the sudo hand-off ---------------------------------------------------------------------


def run_with(
    monkeypatch: pytest.MonkeyPatch, stdout: str, stderr: str = "", code: int = 0
) -> list[Any]:
    seen: list[Any] = []

    def run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        seen.append((argv, kw))
        return subprocess.CompletedProcess(argv, code, stdout, stderr)

    monkeypatch.setattr(subprocess, "run", run)
    return seen


def test_a_plain_user_reaches_the_helper_by_the_one_sudo_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = cli()
    reply = {"exit": 0, "out": ["hi"], "err": [], "files": {}, "status": None, "confirm": None}
    seen = run_with(monkeypatch, json.dumps(reply))
    assert module.call_helper({"command": "status"}) == reply
    argv, kw = seen[0]
    assert argv == ["/usr/bin/sudo", "-n", "-u", "cardea-kernel", "/usr/libexec/cardea-team"]
    assert json.loads(kw["input"]) == {"command": "status"}  # the request is stdin, not arguments
    assert "shell" not in kw


def test_without_the_group_the_person_is_told_what_to_do(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    module = cli()
    run_with(monkeypatch, "", "sudo: a password is required", 1)
    monkeypatch.setattr(module, "kernel_uid", lambda: os.geteuid() + 1)
    assert module.main(["status"]) == 1
    err = capsys.readouterr().err
    assert "a password is required" in err and "cardea-operators" in err


def test_as_the_kernels_account_there_is_no_sudo(
    as_kernel: tuple[ModuleType, Path], monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    module, _ = as_kernel
    seen = run_with(monkeypatch, "")
    assert module.main(["settings"]) == 0 and seen == []
    assert "Team dev-team" in capsys.readouterr().out


def test_cardea_team_is_a_subcommand_of_the_installed_cardea(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    from cardea.service import __main__ as entry

    assert entry.team_kit() == KIT  # in a checkout; the package finds it beside `cardea/`
    monkeypatch.setattr(sys, "argv", ["cardea", "team", "--help"])
    with pytest.raises(SystemExit) as left:  # teamcli's own exit status is the command's
        entry.main()
    assert left.value.code == 0
    assert "usage: cardea team" in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["cardea", "--help"])
    with pytest.raises(SystemExit):
        entry.main()
    assert "team" in capsys.readouterr().out


# --- the same code as the running kernel ---------------------------------------------------


def test_packaged_team_commands_provision_with_the_installed_cardea(
    host_file: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    kit = load("teamkit")
    document = json.loads(host_file.read_text())
    base = tmp_path / "base.json"
    base.write_text(json.dumps({"principals": [], "permissions": [], "platform": []}))
    document["provisioning"] = {
        "base": str(base),
        "out": str(tmp_path / "out.json"),
        "cardea": ["/opt/cardea/apps/api/.venv/bin/python", "-I", "-m", "cardea.service"],
    }
    host = {**document, "credentials": {}, "namespace": "dev"}
    ran: list[list[str]] = []

    def runner(argv: list[str], **_: Any) -> None:
        ran.append(argv)

    kit.provision(host, paused=True, runner=runner)
    assert ran[-1][:3] == ["/opt/cardea/apps/api/.venv/bin/python", "-I", "-m"]  # a checkout's own
    monkeypatch.setattr(kit, "PACKAGED_DIR", Path(str(kit.__file__)).resolve().parent)
    kit.provision(host, paused=True, runner=runner)
    assert ran[-1][:2] == ["/usr/bin/cardea", "provision"]  # a host file cannot point elsewhere


def test_the_supervisor_may_mail_each_developer_by_its_own_address() -> None:
    kit = load("teamkit")
    host = json.loads((KIT / "team.host.example.json").read_text())
    rights = {p["operation"] for p in kit.provisioning_fragment(host)["permissions"]}
    assert {f"mail:dev-team.dev.{n}" for n in range(2)} <= rights
    host["developers"] = 3
    rights = {p["operation"] for p in kit.provisioning_fragment(host)["permissions"]}
    assert "mail:dev-team.dev.2" in rights and "mail:dev-team.dev.3" not in rights


def test_the_team_commands_run_from_a_copy_that_holds_only_the_staged_modules(
    tmp_path: Path,
) -> None:
    """What `packaging/stage.py` ships is enough: no sibling of deploy/team-kit is imported."""
    stage = load_stage()
    copy_ = tmp_path / "teamkit"
    copy_.mkdir()
    for name in stage.TEAM_KIT:
        shutil.copy(KIT / name, copy_ / name)
    done = subprocess.run(
        [sys.executable, "-I", str(copy_ / "teamcli.py"), "--help"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=True,
    )
    assert "usage: cardea team" in done.stdout
    probe = (
        f"import sys; sys.path.insert(0, {str(copy_)!r}); import teamcli; "
        "kit, model, apply, dash = teamcli.modules(); print(kit.packaged(), kit.__file__)"
    )
    ran = subprocess.run(
        [sys.executable, "-I", "-c", probe], capture_output=True, text=True, check=True
    )
    assert "False" in ran.stdout and str(copy_) in ran.stdout


# --- the package ---------------------------------------------------------------------------


def load_stage() -> ModuleType:
    import importlib.util

    spec = importlib.util.spec_from_file_location("stage", ROOT / "packaging" / "stage.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_sudoers_rule_is_one_narrow_line() -> None:
    lines = [
        line
        for line in (DEBIAN / "cardea-team").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert lines == ['%cardea-operators ALL=(cardea-kernel) NOPASSWD: /usr/libexec/cardea-team ""']
    rule = lines[0]
    assert "*" not in rule and "," not in rule and "(ALL" not in rule and "root" not in rule
    assert (
        not rule.startswith("ALL")
        and "SETENV" not in rule
        and "ALL=(cardea-kernel) NOPASSWD: ALL" not in rule
    )


@pytest.mark.skipif(shutil.which("visudo") is None, reason="visudo is not installed")
def test_visudo_accepts_the_rule() -> None:
    subprocess.run(["visudo", "-cf", str(DEBIAN / "cardea-team")], check=True, capture_output=True)


def test_the_helper_takes_no_arguments_and_runs_the_kernels_python() -> None:
    body = (DEBIAN / "cardea-team-helper").read_text()
    lines = [x for x in body.splitlines() if x.strip() and not x.startswith("#")]
    assert lines == [
        "exec /opt/venvs/cardea-kernel/bin/python -I /usr/lib/cardea/teamkit/teamcli.py --helper"
    ]
    assert '"$@"' not in body and "$*" not in body


def test_the_package_installs_the_rule_the_helper_the_kit_and_the_man_page() -> None:
    install = (DEBIAN / "cardea-kernel.install").read_text().splitlines()
    for line in (
        "debian/cardea-team etc/sudoers.d",
        "debian/cardea-team.1 usr/share/man/man1",
        "teamkit/*.py usr/lib/cardea/teamkit",
    ):
        assert line in install
    rules = (DEBIAN / "rules").read_text()
    assert (
        "install -D -m 0755 debian/cardea-team-helper debian/cardea-kernel/usr/libexec/cardea-team"
        in rules
    )
    post = (DEBIAN / "cardea-kernel.postinst").read_text()
    assert "addgroup --system cardea-operators" in post
    assert "chmod 0440 /etc/sudoers.d/cardea-team" in post
    assert "visudo -cf /etc/sudoers.d/cardea-team" in post
    assert "/etc/sudoers.d/cardea-team" in (ROOT / "packaging" / "container_build.sh").read_text()
    # The package's `cardea` is the one entry point; it has no second tool for the team.
    assert (
        (DEBIAN / "cardea")
        .read_text()
        .splitlines()[-1]
        .startswith("exec /opt/venvs/cardea-kernel/bin/python -I /usr/lib/cardea/main.py")
    )
    assert not (DEBIAN / "cardea-team-cli").exists()


def test_the_stage_carries_the_modules_the_commands_load() -> None:
    names = set(load_stage().TEAM_KIT)
    for name in names:
        assert (KIT / name).is_file()
    loaded = set(
        re.findall(r'load\("(\w+)"\)|_load\("\w+", "(\w+)"\)', (KIT / "teamcli.py").read_text())
    )
    wanted = {a or b for a, b in loaded} | {"teamkit", "teamschema", "teamsettings"}
    assert {f"{n}.py" for n in wanted} <= names


def test_no_personal_data_or_host_is_in_the_new_files() -> None:
    files = [
        KIT / "teamcli.py",
        DEBIAN / "cardea-team",
        DEBIAN / "cardea-team-helper",
        DEBIAN / "cardea-team.1",
    ]
    for path in files:
        text = path.read_text()
        assert not re.search(r"@[a-z0-9-]+\.(com|org|net|io)\b|\b\d{1,3}(\.\d{1,3}){3}\b", text), (
            path
        )


def test_a_copy_of_the_settings_does_not_alias_the_example(host_file: Path) -> None:  # noqa: F811
    """`describe` reads and never changes the host document it is given."""
    module = cli()
    kit, model, _, _ = module.modules()
    host = kit.load_host(host_file)
    before = copy.deepcopy(host)
    module.describe(host, kit, model)
    assert host == before
    assert fixture("host")["model"] == "claude-sonnet-4-5"
