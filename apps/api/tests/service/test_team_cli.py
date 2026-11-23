"""`cardea team` (cardea.service.team_cli): every command, the narrow privilege path to the
cardea-kernel account, and the invariants: authority stays with the kernel, no credential is
shown, the host file is fixed and trusted, and the package ships one version of everything."""

import io
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from test_team_settings import host_file as host_file  # noqa: F401  (fixture)
from test_team_settings import secret

from cardea.service import team_cli
from cardea.service.client import ClientError

ROOT = Path(__file__).resolve().parents[4]
DEBIAN = ROOT / "packaging" / "debian"
COMMANDS = (
    "status",
    "pause",
    "resume",
    "stop",
    "apply",
    "render",
    "provision",
    "settings",
    "set",
    "mail",
)
OVERVIEW: dict[str, Any] = json.loads((ROOT / "deploy/team-kit/dashboard.fixture.json").read_text())


class Kernel:
    """The kernel as the supervisor sees it: records calls, can refuse every one."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.refuse: Exception | None = None
        self.mail: list[dict[str, Any]] = []

    def request(self, method: str, path: str, **options: Any) -> dict[str, Any]:
        self.calls.append((method, path, options))
        if self.refuse:
            raise self.refuse
        if path.endswith("/overview"):
            return OVERVIEW
        if path.endswith("/status/mail/text"):
            return {"total": len(self.mail), "items": self.mail, "next": None}
        if path == "/mailbox":
            return {"message_id": "m-1"}
        return {}

    def close(self) -> None:
        pass


class Env:
    def __init__(self, path: Path, kernel: Kernel, kit: Any) -> None:
        self.path, self.kernel, self.kit = path, kernel, kit
        self.provisioned: list[bool] = []
        self.out, self.err = io.StringIO(), io.StringIO()

    def run(self, *argv: str, tty: bool = False, answer: str = "", stdin: str = "") -> int:
        self.out, self.err = io.StringIO(), io.StringIO()
        source = io.StringIO(stdin)
        source.isatty = lambda: tty  # type: ignore[method-assign]
        return team_cli.main(
            list(argv),
            send=lambda request, host: team_cli.execute(request, self.path, who="tester"),
            ask=lambda question: answer,
            stdin=source,
            out=self.out,
            err=self.err,
        )

    def host(self) -> dict[str, Any]:
        found: dict[str, Any] = json.loads(self.path.read_text())
        return found

    @property
    def shown(self) -> str:
        return self.out.getvalue() + self.err.getvalue()


@pytest.fixture
def env(host_file: Path, monkeypatch: pytest.MonkeyPatch) -> Env:  # noqa: F811
    kit = team_cli.kit_modules()[0]
    kernel = Kernel()
    made = Env(host_file, kernel, kit)

    def provision(host: dict[str, Any], *, paused: bool, **_: Any) -> Path:
        made.provisioned.append(paused)
        return Path("provision.json")

    monkeypatch.setattr(kit, "client", lambda host: kernel)
    monkeypatch.setattr(kit, "provision", provision)
    return made


# Every command


def test_every_command_is_in_help_and_the_man_page(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        team_cli.parser().parse_args(["--help"])
    listed = capsys.readouterr().out
    manual = (DEBIAN / "cardea-team.1").read_text()
    for name in COMMANDS:
        assert re.search(rf"^\s+{name}\b", listed, re.M), name
        assert re.search(rf"^\.(B|BR|BI|TP)\b.*\b{name}\b|^\.B {name}\b", manual, re.M), name
    assert "mail list" in manual
    for key in ("runs.team", "usd.run", "schedule.ROLE", "email.rate", "developers", "goals"):
        assert key in manual
    assert "cardea-operators" in manual


def test_status(env: Env) -> None:
    assert env.run("status") == 0
    assert env.out.getvalue().startswith("repository ") or "as of" in env.out.getvalue()
    assert ("GET", "/groups/dev-team/overview") in [(m, p) for m, p, _ in env.kernel.calls]


def test_status_while_paused_reads_the_pause_record(env: Env) -> None:
    env.kernel.refuse = ClientError("principal_disabled", 403)
    env.kit.pause_record_path(env.kit.load_host(env.path)).write_text(
        json.dumps({"reason": "release freeze", "paused_at": "2026-11-23T09:00:00+00:00"})
    )
    assert env.run("status") == 0
    assert "team paused" in env.out.getvalue() and "release freeze" in env.out.getvalue()


def test_pause_resume_and_stop(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    reasons: list[str | None] = []
    kit = env.kit

    def pause(host: dict[str, Any], reason: str | None = None) -> list[str]:
        reasons.append(reason)
        return ["task-1"]

    monkeypatch.setattr(kit, "pause", pause)
    assert env.run("pause", "release", "freeze") == 0
    assert reasons == ["release freeze"] and "cancelled its 1 active run" in env.out.getvalue()
    assert env.run("pause") == 0 and reasons[-1] is None
    assert env.run("resume") == 0
    assert env.provisioned == [False] and "enabled" in env.out.getvalue()


def test_stop_asks_first_and_deletes_the_group(env: Env) -> None:
    assert env.run("stop") == 1  # no terminal, no --yes
    assert env.kernel.calls == [] and "nothing changed" in env.err.getvalue()
    assert env.run("stop", tty=True, answer="n") == 1 and env.kernel.calls == []
    assert env.run("stop", tty=True, answer="y") == 0
    assert ("DELETE", "/groups/dev-team") in [(m, p) for m, p, _ in env.kernel.calls]
    assert env.provisioned == [True]  # the supervisor always ends disabled
    assert "group deleted" in env.out.getvalue()


def test_a_failed_group_delete_still_disables_the_supervisor(env: Env) -> None:
    env.kernel.refuse = ClientError("forbidden", 403)
    assert env.run("stop", "--yes") == 1
    assert env.provisioned == [True] and "not deleted" in env.out.getvalue()


def test_apply_puts_the_collections_and_the_group(env: Env) -> None:
    assert env.run("apply") == 0, env.shown
    puts = [p for m, p, _ in env.kernel.calls if m == "PUT"]
    assert puts == ["/collections/work", "/collections/decisions", "/groups/dev-team"]


def test_provision_and_provision_paused(env: Env) -> None:
    assert env.run("provision") == 0 and env.run("provision", "--paused") == 0
    assert env.provisioned == [False, True]


def test_render_prints_or_writes_the_documents(env: Env, tmp_path: Path) -> None:
    assert env.run("render") == 0
    assert "== group.json ==" in env.out.getvalue()
    assert env.run("render", "--out", str(tmp_path / "out")) == 0
    written = {p.name for p in (tmp_path / "out").iterdir()}
    assert {"group.json", "collection.work.json"} <= written
    json.loads((tmp_path / "out" / "group.json").read_text())
    assert env.kernel.calls == []  # render touches no kernel


def test_settings_in_plain_words(env: Env) -> None:
    assert env.run("settings") == 0
    text = env.out.getvalue()
    for words in (
        "Developers: 2",
        "Model: claude-sonnet-4-5",
        "Runs a day: team 30",
        "Spend a day: team $20.00",
        "Schedule, product manager:",
        "at most 10 every 1 day(s)",
        "Product goals:",
        "cardea team set",
    ):
        assert words in text, words
    assert "{" not in text  # words, not JSON


def test_set_previews_confirms_then_applies(env: Env) -> None:
    before = env.path.read_bytes()
    assert env.run("set", "developers", "3", tty=True, answer="n") == 1
    assert "'developers': 2 -> 3" not in env.out.getvalue()
    assert "developers: 2 -> 3" in env.out.getvalue() and "nothing applied" in env.out.getvalue()
    assert env.path.read_bytes() == before and env.provisioned == []
    assert env.run("set", "developers", "3", tty=True, answer="yes") == 0
    assert env.host()["developers"] == 3 and env.provisioned == [False]
    assert "applied" in env.out.getvalue()
    assert ("PUT", "/groups/dev-team") in [(m, p) for m, p, _ in env.kernel.calls]


def test_set_without_a_terminal_changes_nothing_unless_yes(env: Env) -> None:
    before = env.path.read_bytes()
    assert env.run("set", "runs.team", "35") == 1
    assert env.path.read_bytes() == before and "--yes" in env.err.getvalue()
    assert env.run("set", "runs.team", "35", "--yes") == 0
    assert env.host()["budgets"]["team"]["runs"] == 35


def test_set_again_is_a_no_op(env: Env) -> None:
    assert env.run("set", "developers", "2") == 0
    assert "already in effect" in env.out.getvalue() and env.provisioned == []


@pytest.mark.parametrize(
    ("key", "value", "check"),
    [
        ("runs.dev", "20", lambda h: h["budgets"]["roles"]["dev"]["runs"] == 20),
        ("usd.team", "25.5", lambda h: h["budgets"]["team"]["usd"] == 25.5),
        ("usd.run", "$2", lambda h: h["budgets"]["max_cost_usd_per_run"] == 2.0),
        ("usd.dev", "10", lambda h: h["budgets"]["roles"]["dev"]["usd"] == 10.0),
        ("schedule.dev", "*/30 * * * *; 0 3 * * *", lambda h: len(h["schedules"]["dev"]) == 2),
        ("schedule.pm", "none", lambda h: h["schedules"]["pm"] == []),
        ("email.rate", "5/2", lambda h: h["email"]["rate_limit"] == {"count": 5, "window_days": 2}),
        ("model", "claude-opus-5-5", lambda h: h["model"] == "claude-opus-5-5"),
        ("goals", "Ship the thing.", lambda h: h["product"]["goals"] == "Ship the thing."),
    ],
)
def test_each_kind_of_setting_applies(env: Env, key: str, value: str, check: Any) -> None:
    assert env.run("set", key, value, "--yes") == 0, env.shown
    assert check(env.host())


def test_goals_from_a_file_keep_the_non_goals_when_only_non_goals_change(
    env: Env, tmp_path: Path
) -> None:
    text = tmp_path / "goals.txt"
    text.write_text("Goals from a file.\nSecond line.")
    assert env.run("set", "goals", f"@{text}", "--yes") == 0
    assert env.run("set", "non-goals", "No mobile app.", "--yes") == 0
    product = env.host()["product"]
    assert product == {"goals": "Goals from a file.\nSecond line.", "non_goals": "No mobile app."}
    assert env.run("set", "goals", "@/nonexistent/goals", "--yes") == 1


@pytest.mark.parametrize(
    ("key", "value", "field"),
    [
        ("developers", "0", "developers"),
        ("developers", "many", "developers"),
        ("runs.team", "0", "run_limits.team"),
        ("usd.run", "-1", "spend_limits.per_run_usd"),
        ("schedule.pm", "every day", "schedules.pm[0]"),
        ("email.rate", "5", "COUNT/DAYS"),
        ("email.rate", "5000/1", "email_rate_limit.count"),
        ("email.rate", "5/91", "email_rate_limit.window_days"),
        ("model", "bad model!", "model"),
        ("nonsense", "1", "not a setting key"),
        ("runs.ux", "1", "not a setting key"),
    ],
)
def test_set_validates_like_the_dashboard(env: Env, key: str, value: str, field: str) -> None:
    before = env.path.read_bytes()
    assert env.run("set", key, value, "--yes") == 1
    assert field in env.err.getvalue()
    assert env.path.read_bytes() == before and env.provisioned == []


def test_a_budget_the_schema_refuses_is_reported_not_applied(env: Env) -> None:
    assert env.run("set", "runs.dev", "9999", "--yes") == 1
    assert "exceeds" in env.err.getvalue() and env.provisioned == []


def test_a_stale_confirmation_applies_nothing(env: Env) -> None:
    request = {"command": "set", "key": "developers", "value": "3"}
    token = team_cli.execute(request, env.path)["confirm"]
    other = {**request, "value": "4", "confirm": token}
    assert team_cli.execute(other, env.path)["status"] == "needs_confirmation"
    assert env.host()["developers"] == 2


# Mail


def test_mail_to_a_role_pool(env: Env) -> None:
    for role in ("tl", "pm", "pe", "ux"):
        assert env.run("mail", role, "Please", "look at", "item 3") == 0, role
    sent = [o["json"] for m, p, o in env.kernel.calls if p == "/mailbox"]
    assert [s["to"] for s in sent] == [{"pool": f"dev-team.{r}"} for r in ("tl", "pm", "pe", "ux")]
    assert sent[0]["parts"] == [{"text": "Please look at item 3"}]
    assert sent[0]["namespace"] == "dev" and len({s["client_key"] for s in sent}) == 4
    assert "message m-1" in env.out.getvalue()


def test_mail_to_one_developer_by_number(env: Env) -> None:
    assert env.run("mail", "dev.1", "hello") == 0
    (sent,) = [o["json"] for _, p, o in env.kernel.calls if p == "/mailbox"]
    assert sent["to"] == {"instance": "dev-team.dev.1"}
    assert env.run("mail", "dev.2", "hello") == 1  # the team has two developers: 0 and 1
    assert "no dev.2" in env.err.getvalue()


def test_the_supervisor_is_granted_mail_to_each_developer(env: Env) -> None:
    fragment = env.kit.provisioning_fragment(env.kit.load_host(env.path))
    granted = {p["operation"] for p in fragment["permissions"]}
    assert {"mail:dev-team.dev.0", "mail:dev-team.dev.1", "mail:dev-team.pm"} <= granted
    assert "mail:dev-team.dev.2" not in granted


@pytest.mark.parametrize("target", ["dev", "dev.01", "dev.x", "everyone", "TL", "tl.0", ""])
def test_mail_refuses_other_addresses(env: Env, target: str) -> None:
    assert env.run("mail", target or "''", "hi") == 1
    assert not [c for c in env.kernel.calls if c[1] == "/mailbox"]


def test_mail_text_is_checked(env: Env) -> None:
    assert env.run("mail", "pm") == 1  # no text
    assert env.run("mail", "pm", "x" * 4001) == 1
    assert env.run("mail", "pm", "bad \x1b[31m escape") == 1
    assert env.run("mail", "pm", "-", stdin="from stdin\nline two") == 0
    (sent,) = [o["json"] for _, p, o in env.kernel.calls if p == "/mailbox"]
    assert sent["parts"] == [{"text": "from stdin\nline two"}]


def test_mail_list_prints_full_text_and_no_terminal_escapes(env: Env) -> None:
    env.kernel.mail = [
        {
            "message_id": "m-9",
            "sent_at": "2026-11-22T11:00:00+00:00",
            "from": {"address": "dev-team.dev.0"},
            "to": {"address": "dev-team.tl"},
            "item": {"collection": "work", "record_id": "rec-a"},
            "parts": [{"text": "Item a is ready.\n\x1b]0;owned\x07 second\x9b line"}],
        }
    ]
    assert env.run("mail", "list", "--limit", "5", "--item", "work/rec-a") == 0
    text = env.out.getvalue()
    assert "dev-team.dev.0 -> dev-team.tl [work/rec-a]" in text
    assert "Item a is ready." in text and "1 of 1 message(s)" in text
    assert "\x1b" not in text and "\x07" not in text and "\x9b" not in text
    (call,) = [c for c in env.kernel.calls if c[1].endswith("/status/mail/text")]
    assert call[2]["params"]["limit"] == 5 and call[2]["params"]["item"] == "work/rec-a"


def test_mail_list_checks_its_filters(env: Env) -> None:
    assert env.run("mail", "list", "--limit", "51") == 1
    assert env.run("mail", "list", "--item", "not an item") == 1
    assert env.run("mail", "list", "some text") == 1
    assert env.run("mail", "pm", "hi", "--limit", "3") == 1
    assert env.kernel.calls == []


# Invariants


def test_authority_is_the_kernels_and_a_refusal_is_reported_by_code_only(env: Env) -> None:
    env.kernel.refuse = ClientError("forbidden", 403)
    for argv in (["status"], ["apply"], ["mail", "pm", "hi"], ["mail", "list"]):
        assert env.run(*argv) == 1
        assert "ClientError (forbidden)" in env.err.getvalue()
    env.kernel.refuse = PermissionError(f"denied for {secret()}")
    assert env.run("mail", "pm", "hi") == 1
    assert "PermissionError" in env.err.getvalue() and secret() not in env.shown


def test_no_credential_is_shown_by_any_command(env: Env) -> None:
    for argv in (["status"], ["settings"], ["set", "developers", "3", "--yes"], ["mail", "list"]):
        env.run(*argv)
        assert secret() not in env.shown and "/run/" not in env.shown, argv


def test_the_team_commands_decide_nothing_with_a_model() -> None:
    source = Path(team_cli.__file__).read_text()
    assert not re.search(
        r"^\s*(from|import) cardea\.(runtime|service\.(harnesses|agent))", source, re.M
    )


def test_the_helper_refuses_unknown_commands_and_untrusted_host_files(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert team_cli.execute({"command": "shell"}, env.path) == {
        "ok": False,
        "error": "unknown command",
    }
    env.path.chmod(0o666)
    assert "writable by no one else" in team_cli.execute({"command": "status"}, env.path)["error"]
    env.path.chmod(0o600)
    link = tmp_path / "link.json"
    link.symlink_to(env.path)
    assert not team_cli.execute({"command": "status"}, link)["ok"]
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)  # owned by someone else
    assert not team_cli.execute({"command": "status"}, env.path)["ok"]
    assert env.kernel.calls == []


def test_the_host_file_cannot_be_chosen_through_the_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[Any] = []

    def runner(argv: list[str], **options: Any) -> Any:
        sent.append((argv, options))
        return subprocess.CompletedProcess(argv, 0, stdout=b'{"ok": true, "lines": ["fine"]}')

    monkeypatch.setattr(team_cli, "_kernel_uid", lambda: os.geteuid() + 1)
    with pytest.raises(team_cli.TeamError, match="--host is only for cardea-kernel"):
        team_cli.deliver({"command": "status"}, Path("/tmp/other.json"), runner=runner)
    assert sent == []
    found = team_cli.deliver({"command": "status"}, runner=runner)
    assert found["lines"] == ["fine"]
    ((argv, options),) = sent
    # No sudo -u for the user to type, and the helper takes no arguments: the request is stdin.
    assert argv == ["/usr/bin/sudo", "-n", "-u", "cardea-kernel", "/usr/libexec/cardea-team-run"]
    assert json.loads(options["input"]) == {"command": "status"}


def test_a_user_outside_the_operators_group_is_told_what_to_do(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(team_cli, "_kernel_uid", lambda: os.geteuid() + 1)
    runner = lambda argv, **options: subprocess.CompletedProcess(argv, 1, stdout=b"")  # noqa: E731
    with pytest.raises(team_cli.TeamError, match="cardea-operators"):
        team_cli.deliver({"command": "status"}, runner=runner)


def test_the_helper_answers_one_json_object_and_nothing_else() -> None:
    src = Path(team_cli.__file__).resolve().parents[2]
    code = (
        f"import sys; sys.path.insert(0, {str(src)!r}); "
        "from cardea.service import team_cli; team_cli.helper_main()"
    )
    for body, expected in ((b"not json", "not valid"), (b'{"command": "status"}', "")):
        done = subprocess.run(
            [sys.executable, "-I", "-c", code], input=body, capture_output=True, check=True
        )
        answer = json.loads(done.stdout)  # stdout is the answer and only the answer
        assert answer["ok"] is False and expected in answer["error"]


def test_a_front_end_never_prints_what_the_helper_returns_unfiltered(env: Env) -> None:
    def send(request: dict[str, Any], host: Path | None) -> dict[str, Any]:
        return {"ok": True, "lines": ["clean \x1b[2J screen"]}

    out = io.StringIO()
    assert team_cli.main(["status"], send=send, out=out) == 0
    assert "\x1b" not in out.getvalue()


# The package


def test_the_sudoers_rule_is_narrow() -> None:
    lines = [
        line.strip()
        for line in (DEBIAN / "cardea-team").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    rules = [line for line in lines if not line.startswith("Defaults")]
    assert rules == [
        '%cardea-operators ALL=(cardea-kernel) NOPASSWD: /usr/libexec/cardea-team-run ""'
    ]
    assert not any("*" in line or "ALL=(ALL" in line for line in lines)
    helper = (DEBIAN / "cardea-team-run").read_text()
    assert '"$@"' not in helper and "$*" not in helper  # no argument reaches the helper
    assert "-I" in helper and "/opt/venvs/cardea-kernel/bin/python" in helper


def test_the_package_installs_the_helper_the_rule_the_kit_and_the_man_page() -> None:
    install = (DEBIAN / "cardea-kernel.install").read_text().splitlines()
    for line in (
        "debian/cardea-team etc/sudoers.d",
        "debian/cardea-team-run usr/libexec",
        "team-kit usr/lib/cardea",
    ):
        assert line in install
    assert (DEBIAN / "cardea-kernel.manpages").read_text().split() == ["debian/cardea-team.1"]
    postinst = (DEBIAN / "cardea-kernel.postinst").read_text()
    assert "visudo -cf /etc/sudoers.d/cardea-team" in postinst
    assert "chmod 0440 /etc/sudoers.d/cardea-team" in postinst
    assert "addgroup --system cardea-operators" in postinst


def test_the_stage_ships_the_kit_and_the_helper_without_fixtures(tmp_path: Path) -> None:
    fake_uv = tmp_path / "uv"
    fake_uv.write_text("#!/bin/sh\nexit 0\n")
    fake_uv.chmod(0o755)
    stage = tmp_path / "stage"
    subprocess.run(
        [sys.executable, str(ROOT / "packaging" / "stage.py"), str(stage), "--uv", str(fake_uv)],
        check=True,
        capture_output=True,
    )
    shipped = {p.name for p in (stage / "team-kit").iterdir()}
    assert {"teamkit.py", "teamsettings_apply.py", "dashboard.py", "teamschema.py"} <= shipped
    assert not [n for n in shipped if "fixture" in n or n.endswith(".md")]
    helper = stage / "debian" / "cardea-team-run"
    assert stat.S_IMODE(helper.stat().st_mode) == 0o755
    manifest = json.loads((stage / "source-manifest.json").read_text())
    assert "team-kit/teamkit.py" in manifest and "debian/cardea-team" in manifest
    # What `cardea team` loads is the staged kit, beside the kernel's own code.
    assert (stage / "src" / "cardea" / "service" / "team_cli.py").is_file()


def test_installed_the_kit_provisions_with_the_kernels_own_code(
    env: Env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Under /usr/lib/cardea the host file's `provisioning.cardea` is ignored: provisioning is
    the package's `main.py` run by the interpreter that runs the kit, not a separate checkout."""
    kit = env.kit
    kit_path = Path(kit.__file__).resolve()
    monkeypatch.setattr(kit, "PACKAGED_ROOT", kit_path.parent)
    monkeypatch.setattr(kit, "PACKAGED_MAIN", kit_path.parent / "main.py")
    other = {"cardea": ["/opt/cardea/apps/api/.venv/bin/python", "-I", "-m", "cardea.service"]}
    assert kit.provision_command(other) == [sys.executable, "-I", str(kit_path.parent / "main.py")]
    monkeypatch.setattr(kit, "PACKAGED_ROOT", tmp_path)  # a checkout: the host file decides
    assert kit.provision_command(other) == other["cardea"]


def test_the_kit_is_found_beside_the_kernel_code() -> None:
    assert (team_cli.kit_dir() / "teamkit.py").is_file()
    entry = (ROOT / "apps/api/src/cardea/service/__main__.py").read_text()
    assert 'sys.argv[1:2] == ["team"]' in entry
