"""`cardea team` (cardea.service.team_cli): each command works through the team kit's own code,
as a plain user through the narrow helper and as `cardea-kernel` directly; `set` previews,
confirms and applies exactly what was shown; the helper reads only the fixed host file; and a
secret never reaches the output."""

import importlib.util
import io
import json
import stat
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from test_team_schema import KIT, load
from test_team_settings import host_file as host_file  # noqa: F401  (fixture)
from test_team_settings import secret

from cardea.service import team_cli

VIEW: dict[str, Any] = json.loads((KIT / "dashboard.fixture.json").read_text())
GROUP, NAMESPACE = "dev-team", "dev"


class KernelRefused(Exception):
    """What the kernel client raises: a code, and a message that must not be shown."""

    code = "forbidden"


class FakeKernel:
    def __init__(self, env: "Env") -> None:
        self.env = env

    def request(self, method: str, path: str, **options: Any) -> dict[str, Any]:
        self.env.requests.append((method, path, options))
        if self.env.refuse == method:
            raise KernelRefused(f"denied for {secret()}")
        if method == "POST":
            return {"message_id": "m-1"}
        if path.endswith("/status/mail/text"):
            return self.env.page
        return {}

    def close(self) -> None:
        pass


class Env:
    def __init__(self, path: Path, kit: ModuleType, state: Path) -> None:
        self.path, self.kit, self.state = path, kit, state
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        self.provisioned: list[bool] = []
        self.refuse: str | None = None
        self.page: dict[str, Any] = {"total": 0, "items": [], "next": None}
        self.answers: list[str] = []
        self.asked: list[str] = []

    def ask(self, prompt: str) -> str:
        self.asked.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)

    def run(self, *argv: str) -> int:
        transport = team_cli.direct_transport(self.path)
        return team_cli.main(list(argv), transport=transport, ask=self.ask)

    def host(self) -> dict[str, Any]:
        found: dict[str, Any] = json.loads(self.path.read_text())
        return found


@pytest.fixture
def env(host_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:  # noqa: F811
    kit = team_cli.load("teamsettings_apply")._modules()[0]
    # The CLI sets package defaults on the kit; keep them out of the other tests.
    monkeypatch.setattr(kit, "PROVISIONING_DEFAULTS", dict(kit.PROVISIONING_DEFAULTS))
    state = tmp_path / "state"
    monkeypatch.setattr(team_cli, "HOST_FILE", host_file)
    monkeypatch.setattr(team_cli, "STATE_DIR", state)
    monkeypatch.setattr(
        team_cli, "PACKAGE_PROVISIONING", {"out": str(state / "provision.json"), "cardea": ["x"]}
    )
    made = Env(host_file, kit, state)

    def provision(host: dict[str, Any], *, paused: bool, **_: Any) -> Path:
        made.provisioned.append(paused)
        return Path("provision.json")

    monkeypatch.setattr(kit, "provision", provision)
    monkeypatch.setattr(kit, "client", lambda host: FakeKernel(made))
    monkeypatch.setattr(kit, "overview", lambda host: VIEW)
    return made


def out_of(capsys: pytest.CaptureFixture[str]) -> tuple[str, str]:
    seen = capsys.readouterr()
    return seen.out, seen.err


# ---- help, identity and the helper ----


def test_help_lists_every_command(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stop:
        team_cli.main(["--help"])
    assert stop.value.code == 0
    text = out_of(capsys)[0]
    flat = " ".join(text.split())
    for name, summary in team_cli.COMMANDS:
        assert name in text and summary.split(" (")[0] in flat, name
    for name, _ in team_cli.COMMANDS:
        with pytest.raises(SystemExit) as stop:
            team_cli.main([name, "--help"])
        assert stop.value.code == 0
    assert "TARGET TEXT" in out_of(capsys)[0]


def test_a_plain_user_goes_through_the_helper_and_only_the_helper(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sudo = tmp_path / "sudo"
    sudo.write_text(
        "#!/bin/sh\n"
        f'echo "$#" > {tmp_path}/argc\ncat > {tmp_path}/stdin\n'
        "echo 'relayed output'\n"
    )
    sudo.chmod(0o755)
    monkeypatch.setattr(team_cli, "SUDO", (str(sudo),))
    monkeypatch.setattr(team_cli, "running_as_kernel", lambda: False)
    assert team_cli.main(["pause", "release", "freeze"]) == 0
    assert out_of(capsys)[0] == "relayed output\n"
    assert (tmp_path / "argc").read_text().strip() == "0"
    assert json.loads((tmp_path / "stdin").read_text()) == {
        "argv": ["pause", "release", "freeze"]
    }


def test_a_refused_sudo_says_who_may_run_team_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sudo = tmp_path / "sudo"
    sudo.write_text("#!/bin/sh\necho 'sudo: a password is required' >&2\nexit 1\n")
    sudo.chmod(0o755)
    monkeypatch.setattr(team_cli, "SUDO", (str(sudo),))
    monkeypatch.setattr(team_cli, "running_as_kernel", lambda: False)
    assert team_cli.main(["status"]) == 1
    assert "cardea-operators" in out_of(capsys)[1]
    monkeypatch.setattr(team_cli, "SUDO", (str(tmp_path / "missing"),))
    assert team_cli.main(["status"]) == 127


def test_host_is_honoured_only_as_the_kernel_account(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    other = tmp_path / "other.host.json"
    other.write_text(env.path.read_text())
    monkeypatch.setattr(team_cli, "running_as_kernel", lambda: False)
    with pytest.raises(SystemExit) as stop:
        team_cli.main(["--host", str(other), "settings"])
    assert stop.value.code == 2 and "cardea-kernel" in out_of(capsys)[1]
    monkeypatch.setattr(team_cli, "running_as_kernel", lambda: True)
    monkeypatch.setattr(team_cli, "HOST_FILE", tmp_path / "unused.json")
    assert team_cli.main(["--host", str(other), "settings"]) == 0
    assert "developers: 2" in out_of(capsys)[0]
    assert team_cli.main(["settings", "--host", str(other)]) == 0  # after the command, too
    assert team_cli.main(["settings"]) == 1  # the default file is the (missing) fixed one
    assert "missing" in out_of(capsys)[1]


def test_the_helper_reads_only_the_fixed_host_file(
    env: Env, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ask = '{"argv": ["settings", "--json"]}'
    assert team_cli.helper_main(io.StringIO(ask)) == 0
    assert json.loads(out_of(capsys)[0])["developers"] == 2
    elsewhere = tmp_path / "elsewhere.json"
    for argv in (["--host", str(elsewhere), "settings"], ["settings", f"--host={elsewhere}"]):
        request = json.dumps({"argv": argv})
        assert team_cli.helper_main(io.StringIO(request)) == 2
        assert "not honoured" in out_of(capsys)[1]
    assert not elsewhere.exists()


@pytest.mark.parametrize(
    "request_text",
    [
        "",
        "not json",
        "[]",
        '{"argv": []}',
        '{"argv": "status"}',
        '{"argv": ["status", 3]}',
        '{"argv": ["status"], "host": "/etc/shadow"}',
        '{"argv": ["--helper"]}',
        json.dumps({"argv": ["status"] * 65}),
        json.dumps({"argv": ["x" * 20000]}),
    ],
)
def test_the_helper_refuses_anything_but_one_well_formed_request(
    env: Env, request_text: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert team_cli.helper_main(io.StringIO(request_text)) != 0
    assert env.requests == [] and env.provisioned == []
    assert out_of(capsys)[0] == ""


def test_the_helper_takes_the_command_line_the_user_typed(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    request = json.dumps({"argv": ["mail", "pm", "hello", "there"]})
    assert team_cli.helper_main(io.StringIO(request)) == 0
    assert env.requests[0][2]["json"]["parts"] == [{"text": "hello there"}]
    assert "sent to dev-team.pm" in out_of(capsys)[0]


def test_the_kit_comes_from_the_installed_package_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = tmp_path / "usr" / "lib" / "cardea"
    service = lib / "cardea" / "service"
    service.mkdir(parents=True)
    (lib / "teamkit").mkdir()
    (lib / "teamkit" / "teamkit.py").write_text("")
    source = Path(team_cli.__file__).read_text()
    (service / "team_cli.py").write_text(source)
    spec = importlib.util.spec_from_file_location("installed_team_cli", service / "team_cli.py")
    assert spec is not None and spec.loader is not None
    installed = importlib.util.module_from_spec(spec)
    sys.modules["installed_team_cli"] = installed
    spec.loader.exec_module(installed)
    assert installed.kit_dir() == (lib / "teamkit").resolve()
    monkeypatch.setattr(team_cli, "KIT_DIR", tmp_path)  # no teamkit.py there: skipped
    assert (team_cli.kit_dir() / "teamkit.py").is_file()
    del sys.modules["installed_team_cli"]


# ---- the team commands ----


def test_status(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    host = env.kit.load_host(env.path)
    assert env.run("status") == 0
    assert out_of(capsys)[0] == "\n".join(env.kit.status(host)) + "\n"


def test_status_of_a_paused_team_gives_the_recorded_reason(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from cardea.service.client import ClientError

    def disabled(host: dict[str, Any]) -> dict[str, Any]:
        raise ClientError("principal_disabled", 403)

    monkeypatch.setattr(env.kit, "overview", disabled)
    assert env.run("pause", "rotating", "tokens") == 0
    capsys.readouterr()
    assert env.run("status") == 0
    text = out_of(capsys)[0]
    assert text.startswith("team paused") and "rotating tokens" in text


def test_pause_resume_and_the_record(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    host = env.kit.load_host(env.path)
    assert env.run("pause", "release", "freeze") == 0
    assert "cancelled its" in out_of(capsys)[0]
    assert env.provisioned == [True]
    assert env.kit.read_pause_record(host)["reason"] == "release freeze"
    assert env.run("resume") == 0
    assert env.provisioned == [True, False]
    assert env.kit.read_pause_record(host) == {}
    assert "triggers admit again" in out_of(capsys)[0]
    assert env.run("pause") == 0
    assert env.kit.read_pause_record(host)["reason"] == "no reason given"


def test_stop_deletes_the_group_and_disables_the_supervisor(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    assert env.run("stop") == 0
    assert ("DELETE", f"/groups/{GROUP}", {"params": {"namespace": NAMESPACE}}) in env.requests
    assert env.provisioned == [True]
    assert "group deleted" in out_of(capsys)[0]


def test_stop_reports_a_group_it_could_not_delete_without_the_lower_layer_message(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    env.refuse = "DELETE"
    assert env.run("stop") == 1
    out, err = out_of(capsys)
    assert env.provisioned == [True]  # disabled even so
    assert "the group was not deleted (KernelRefused)" in err and "disabled" in out
    assert secret() not in out + err


def test_apply_puts_the_collections_and_the_group(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    assert env.run("apply") == 0
    assert [(m, p) for m, p, _ in env.requests] == [
        ("PUT", "/collections/work"),
        ("PUT", "/collections/decisions"),
        ("PUT", f"/groups/{GROUP}"),
    ]
    assert f"group {GROUP} in {NAMESPACE}" in out_of(capsys)[0]


def test_render_writes_the_documents_under_the_state_directory(
    env: Env, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert env.run("render") == 0
    written = sorted(out_of(capsys)[0].split())
    assert written and all(Path(p).parent == env.state / "rendered" for p in written)
    assert {Path(p).name for p in written} >= {"group.json", "provisioning.team.json"}
    assert all(stat.S_IMODE(Path(p).stat().st_mode) == 0o600 for p in written)
    assert env.run("render", "--out", str(tmp_path / "mine")) == 0
    assert (tmp_path / "mine" / "group.json").is_file()


def test_through_the_helper_render_stays_under_the_state_directory(
    env: Env, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for out in (tmp_path / "elsewhere", env.state / ".." / "elsewhere"):
        code = team_cli.run_argv(["render", "--out", str(out)], env.path, helper=True)
        assert code == 1 and "under" in out_of(capsys)[1]
        assert not (tmp_path / "elsewhere").exists()
    inside = env.state / "mine"
    assert team_cli.run_argv(["render", "--out", str(inside)], env.path, helper=True) == 0
    assert (inside / "group.json").is_file()


def test_provision_refuses_to_resume_a_paused_team_by_accident(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    assert env.run("provision") == 0
    assert env.provisioned == [False] and out_of(capsys)[0] == "provision.json\n"
    assert env.run("provision", "--paused") == 0
    env.run("pause")
    env.provisioned.clear()
    capsys.readouterr()
    assert env.run("provision") == 1
    assert "paused" in out_of(capsys)[1] and env.provisioned == []
    assert env.run("provision", "--paused") == 0 and env.provisioned == [True]


def test_settings_shows_the_current_settings_and_no_secret(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    assert env.run("settings") == 0
    text = out_of(capsys)[0]
    for line in (
        "developers: 2",
        'model: "claude-sonnet-4-5"',
        "run_limits.team: 30",
        "email_rate_limit.count: 10",
        "email_rate_limit.window_days: 1",
        "paused.paused: false",
        "product_goals.goals: 15 bytes",
    ):
        assert line in text.splitlines(), line
    assert "Original goals." not in text
    assert secret() not in text and "reviewer" not in text and "kernel.sock" not in text
    assert env.run("settings", "--json") == 0
    shown = json.loads(out_of(capsys)[0])
    assert shown["email_rate_limit"] == {"count": 10, "window_days": 1}
    assert shown["product_goals"]["goals"] == "Original goals."  # JSON is the whole truth


# ---- set ----


def test_set_previews_asks_and_applies_exactly_the_preview(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    env.answers = ["y"]
    assert env.run("set", "developers", "3") == 0
    out = out_of(capsys)[0]
    assert "developers: 2 -> 3" in out and out.rstrip().endswith("applied")
    assert env.asked == ["Apply this change? [y/N] "]
    assert env.host()["developers"] == 3
    assert env.provisioned == [False]  # the kit's provision, as a settings change does
    # The supervisor's live standing is read first; then the collections and the group.
    assert [m for m, _, _ in env.requests] == ["GET", "PUT", "PUT", "PUT"]


def test_declining_changes_nothing(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    before = env.path.read_bytes()
    for answer in ("n", "", "maybe"):
        env.answers = [answer]
        assert env.run("set", "developers", "3") == 1
        assert "nothing changed" in out_of(capsys)[0]
    assert env.run("set", "developers", "3") == 1  # no answer at all: end of input
    assert env.path.read_bytes() == before and env.provisioned == [] and env.requests == []


def test_yes_skips_the_question(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    assert env.run("set", "--yes", "model", "claude-opus-5-5") == 0
    assert env.asked == [] and env.host()["model"] == "claude-opus-5-5"
    assert "applied" in out_of(capsys)[0]


def test_setting_what_is_already_in_effect_changes_nothing(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    before = env.path.read_bytes()
    assert env.run("set", "developers", "2") == 0
    assert "already in effect" in out_of(capsys)[0]
    assert env.path.read_bytes() == before and env.asked == []


def test_a_token_applies_only_the_preview_it_came_from(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    env.answers = ["n"]
    env.run("set", "developers", "3")
    shown = capsys.readouterr().out
    assert shown and env.host()["developers"] == 2
    _, model, applier = team_cli.modules()
    token = applier.token(model.preview(env.path, {"developers": 3}, paused=False)["changes"])
    assert env.run("set", "developers", "4", "--confirm", token) == 1  # a different change
    assert "changed since that preview" in out_of(capsys)[1]
    assert env.host()["developers"] == 2
    assert env.run("set", "developers", "3", "--confirm", token) == 0
    assert env.host()["developers"] == 3


def test_a_file_that_moved_on_after_the_preview_is_not_applied(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    def moves_on(prompt: str) -> str:
        document = env.host()
        document["budgets"]["team"]["runs"] = 31
        env.path.write_text(json.dumps(document))
        return "y"

    env.ask = moves_on  # type: ignore[method-assign]
    assert env.run("set", "run_limits.team", "35") == 1
    assert "changed since that preview" in out_of(capsys)[1]
    assert env.host()["budgets"]["team"]["runs"] == 31 and env.provisioned == []


@pytest.mark.parametrize(
    ("key", "value", "field"),
    [
        ("colour", "red", "colour"),
        ("developers", "0", "developers"),
        ("developers", "many", "JSON"),
        ("model", "has space", "model"),
        ("run_limits.roles.qa", "3", "run_limits.roles.qa"),
        ("spend_limits.per_run_usd", "-1", "spend_limits.per_run_usd"),
        ("email_rate_limit.count", "0", "email_rate_limit.count"),
        ("email_rate_limit.window_days", "91", "email_rate_limit.window_days"),
        ("email_rate_limit", '{"count": 5}', "email_rate_limit.window_days"),
        ("schedules.pm", "every day", "schedules.pm[0]"),
        ("paused", "maybe", "JSON"),
        ("credentials", "{}", "credentials"),
        ("kernel.socket", '"/x"', "kernel"),
        ("developers.", "3", "developers."),
    ],
)
def test_set_validates_like_the_dashboard_and_names_the_field(
    env: Env, capsys: pytest.CaptureFixture[str], key: str, value: str, field: str
) -> None:
    before = env.path.read_bytes()
    assert env.run("set", "--yes", key, value) == 1
    out, err = out_of(capsys)
    assert field in err and out == ""
    assert env.path.read_bytes() == before and env.provisioned == []


def test_a_secret_pasted_as_a_value_is_never_echoed(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    for key in ("model", "developers", "schedules.pm", "email_rate_limit.count"):
        assert env.run("set", "--yes", key, f"{secret()} with spaces") == 1
    assert env.run("set", "--yes", "credentials", secret()) == 1
    out, err = out_of(capsys)
    assert secret() not in out + err


def test_set_covers_every_kind_of_setting(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    changes = [
        ("run_limits.team", "40"),
        ("run_limits.roles.dev", "20"),
        ("spend_limits.team_usd", "25.5"),
        ("spend_limits.per_run_usd", "2"),
        ("spend_limits.roles_usd.dev", "10"),
        ("schedules.pm", "0 10 * * 1-5;*/30 * * * *"),
        ("schedules.dev", '["*/30 * * * *"]'),
        ("email_rate_limit.count", "20"),
        ("product_goals.goals", "New goals."),
    ]
    for key, value in changes:
        assert env.run("set", "-y", key, value) == 0, key
    host = env.host()
    assert host["budgets"]["team"] == {"runs": 40, "usd": 25.5}
    assert host["budgets"]["roles"]["dev"] == {"runs": 20, "usd": 10.0}
    assert host["budgets"]["max_cost_usd_per_run"] == 2.0
    assert host["schedules"]["pm"] == ["0 10 * * 1-5", "*/30 * * * *"]
    assert host["schedules"]["dev"] == ["*/30 * * * *"]
    # Setting one field of a whole setting keeps the others.
    assert host["email"]["rate_limit"] == {"count": 20, "window_days": 1}
    assert host["product"] == {"goals": "New goals.", "non_goals": "Original non-goals."}
    assert env.run("set", "-y", "email_rate_limit", '{"count": 7, "window_days": 3}') == 0
    assert env.host()["email"]["rate_limit"] == {"count": 7, "window_days": 3}
    assert env.kit.email_limit(env.host()) == {"count": 7, "window_days": 3}


def test_set_paused_pauses_and_resumes_through_the_kit(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[str | None] = []

    def pause(host: dict[str, Any], reason: str | None = None) -> list[str]:
        calls.append(reason)
        env.kit.pause_record_path(host).write_text(json.dumps({"reason": reason}))
        return []

    monkeypatch.setattr(env.kit, "pause", pause)
    assert env.run("set", "-y", "paused", "true") == 0
    assert calls == [None] and env.kit.read_pause_record(env.kit.load_host(env.path))
    assert env.run("set", "-y", "paused", "false") == 0
    assert env.provisioned == [False]
    assert not env.kit.read_pause_record(env.kit.load_host(env.path))


def test_the_kernels_refusal_leaves_the_old_settings_and_no_lower_message(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    env.refuse = "PUT"
    before = env.path.read_bytes()
    assert env.run("set", "-y", "developers", "3") == 1
    out, err = out_of(capsys)
    assert "the old settings are back" in err and secret() not in out + err
    assert env.path.read_bytes() == before


def test_the_audit_names_who_asked(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SUDO_USER", "alice")
    assert env.run("set", "-y", "developers", "3") == 0
    applier = team_cli.modules()[2]
    entries = applier.read_audit(env.kit, env.kit.load_host(env.path))
    assert {e["by"] for e in entries} == {"alice"}
    assert [e["outcome"] for e in entries] == ["started", "applied"]


# ---- mail ----


def test_mail_goes_to_a_pool_as_the_supervisor(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    for role in ("tl", "pm", "pe", "ux"):
        assert env.run("mail", role, "Please", "look", "at", "work/1") == 0
    sent = [options["json"] for _, _, options in env.requests]
    assert [s["to"] for s in sent] == [{"pool": f"{GROUP}.{r}"} for r in ("tl", "pm", "pe", "ux")]
    assert {s["namespace"] for s in sent} == {NAMESPACE}
    assert sent[0]["parts"] == [{"text": "Please look at work/1"}]
    assert len({s["client_key"] for s in sent}) == 4  # each command is its own message
    assert all(m == "POST" and p == "/mailbox" for m, p, _ in env.requests)
    assert f"sent to {GROUP}.tl (message m-1)" in out_of(capsys)[0]


def test_mail_goes_to_one_developer(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    assert env.run("mail", "dev.1", "Rebase", "first") == 0
    assert env.requests[0][2]["json"]["to"] == {"instance": f"{GROUP}.dev.1"}
    assert f"sent to {GROUP}.dev.1" in out_of(capsys)[0]


@pytest.mark.parametrize(
    "words",
    [
        ["qa", "hello"],
        ["dev", "hello"],
        ["dev.2", "hello"],  # the team has two developers: dev.0 and dev.1
        ["dev.x", "hello"],
        ["dev.-1", "hello"],
        ["pm"],
        ["pm", "   "],
        ["pm", "x" * 4001],
        ["list", "extra"],
    ],
)
def test_mail_refuses_a_bad_target_or_text(
    env: Env, capsys: pytest.CaptureFixture[str], words: list[str]
) -> None:
    assert env.run("mail", *words) == 1
    assert env.requests == [] and out_of(capsys)[0] == ""


def test_the_kernels_refusal_of_mail_is_reported_by_code_only(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    env.refuse = "POST"
    assert env.run("mail", "pm", "hello") == 1
    err = out_of(capsys)[1]
    assert "KernelRefused (forbidden)" in err and secret() not in err


def test_mail_list_shows_the_teams_mail_with_text_made_safe(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    env.page = {
        "total": 3,
        "next": "00000000-0000-0000-0000-000000000001",
        "items": [
            {
                "message_id": "m-1",
                "sent_at": "2026-11-23T09:00:00+00:00",
                "from": {"address": "sup-dev", "role": "supervisor"},
                "to": {"address": f"{GROUP}.pm", "kind": "pool", "role": "pm"},
                "item": {"collection": "work", "record_id": "rec-1"},
                "parts": [{"text": "Hello\x1b[31m red\nnext line"}, {"data": {"a": 1}}],
            },
            {
                "message_id": "m-2",
                "sent_at": "2026-11-23T08:00:00+00:00",
                "from": {"address": f"{GROUP}.pm.0", "role": "pm"},
                "to": {"address": "sup-dev", "kind": "instance", "role": "supervisor"},
                "item": None,
                "parts": [{"text": "y" * 1000}],
            },
        ],
    }
    assert env.run("mail", "list", "--member", "sup-dev", "--limit", "2") == 0
    method, path, options = env.requests[0]
    assert (method, path) == ("GET", f"/groups/{GROUP}/status/mail/text")
    assert options["params"] == {"namespace": NAMESPACE, "member": "sup-dev", "limit": 2}
    lines = out_of(capsys)[0].splitlines()
    assert lines[0] == (
        f"2026-11-23T09:00:00+00:00 sup-dev -> {GROUP}.pm [work/rec-1]: Hello [31m red next line"
    )
    assert lines[1].endswith("...") and len(lines[1]) < 400
    assert lines[2] == "2 of 3 message(s)"
    assert lines[3] == "more: cardea team mail list --after 00000000-0000-0000-0000-000000000001"
    assert "\x1b" not in "".join(lines)


@pytest.mark.parametrize(
    "options",
    [["--limit", "0"], ["--limit", "51"], ["--member", "a b"], ["--item", "nope"], ["--after", "x"]],
)
def test_mail_list_refuses_a_bad_filter_before_asking_the_kernel(
    env: Env, capsys: pytest.CaptureFixture[str], options: list[str]
) -> None:
    assert env.run("mail", "list", *options) == 1
    assert env.requests == [] and "invalid_query" in out_of(capsys)[1]


# ---- the team kit's defaults ----


def test_the_kit_defaults_are_the_documented_paths() -> None:
    kit = load("teamkit")
    assert kit.PROVISIONING_DEFAULTS == {
        "base": "/etc/cardea/provision.base.json",
        "out": "/etc/cardea/provision.json",
        "config": "/etc/cardea/config.json",
        "cardea": ["/opt/cardea/apps/api/.venv/bin/python", "-I", "-m", "cardea.service"],
    }
    example = json.loads((KIT / "team.host.example.json").read_text())
    assert example["provisioning"] == kit.PROVISIONING_DEFAULTS


def test_provisioning_uses_the_defaults_a_host_file_leaves_out(
    host_file: Path, tmp_path: Path  # noqa: F811
) -> None:
    kit = load("teamkit")  # a fresh copy: its defaults are its own to change
    base = tmp_path / "base.json"
    base.write_text(json.dumps({"principals": [], "permissions": [], "platform": []}))
    out = tmp_path / "state" / "provision.json"
    out.parent.mkdir()
    kit.PROVISIONING_DEFAULTS.update(
        base=str(base), out=str(out), config="/c.json", cardea=["/bin/cardea"]
    )
    document = json.loads(host_file.read_text())
    document.pop("provisioning")
    host_file.write_text(json.dumps(document))
    host = kit.load_host(host_file)
    ran: list[list[str]] = []
    written = kit.provision(host, paused=True, runner=lambda cmd, **_: ran.append(cmd))
    assert written == out and stat.S_IMODE(out.stat().st_mode) == 0o600
    assert ran == [["/bin/cardea", "provision", "--config", "/c.json", str(out)]]
    assert kit.pause_record_path(host) == out.with_name("dev-team.pause.json")
    # What the host file names itself wins over the defaults.
    document["provisioning"] = {"base": str(base), "out": str(tmp_path / "mine.json")}
    host_file.write_text(json.dumps(document))
    assert kit.provision(kit.load_host(host_file), paused=True, runner=lambda *a, **k: None) == (
        tmp_path / "mine.json"
    )


def test_the_package_puts_the_kits_records_where_the_kernel_account_can_write(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = json.loads(env.path.read_text())
    document.pop("provisioning")
    env.path.write_text(json.dumps(document))
    team_cli.modules()
    host = env.kit.load_host(env.path)
    assert env.kit.pause_record_path(host).parent == env.state
    assert env.kit.PROVISIONING_DEFAULTS["cardea"] == ["x"]
    # A host file that names its own paths keeps them.
    document["provisioning"] = {"out": str(tmp_path / "mine.json")}
    env.path.write_text(json.dumps(document))
    assert env.kit.pause_record_path(env.kit.load_host(env.path)).parent == tmp_path
