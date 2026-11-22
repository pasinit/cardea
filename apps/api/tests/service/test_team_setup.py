"""The one-command team setup (deploy/team-kit/teamsetup.py, ADR-040): the steps converge a
fresh host, a repeated run changes nothing, a killed run resumes at its step, and every
step prints its verify result. The host is simulated; nothing here runs on Debian."""

import importlib.util
import json
import os
import re
import shutil
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

from cardea.service.client import ClientError

KIT = Path(__file__).resolve().parents[4] / "deploy" / "team-kit"
ROOT = KIT.parents[1]


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, KIT / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


setup = load("teamsetup")
STEP_NAMES = [step.name for step in setup.steps()]


class Crash(BaseException):
    """A kill: not an Exception, so nothing in the run can catch it and record a failure."""


class FakeKernel:
    def __init__(self, groups: dict[str, Any]) -> None:
        self.groups = groups

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        if method == "PUT" and path.startswith("/groups/"):
            self.groups[path.removeprefix("/groups/")] = kwargs["json"]
        elif (
            method == "GET"
            and path.startswith("/groups/")
            and path.removeprefix("/groups/") not in self.groups
        ):
            raise ClientError("not_found", 404)
        return {}

    def close(self) -> None:
        return None


class FakeHost:
    """A Debian host that remembers what commands did. `changes` lists every effect."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.users: set[str] = set()
        self.packages: set[str] = set()
        self.active: set[str] = set()
        self.imports: set[str] = set()
        self.venv = False
        self.entries: list[tuple[str, str]] = []
        self.groups: dict[str, Any] = {}
        self.changes: list[str] = []
        self.crash_at: int | None = None
        self.ready = True
        self.socket = base / "kernel.sock"

    # Reads

    def run(self, argv: list[str], *, input: str | None = None, cwd: Path | None = None) -> Any:
        head = argv[0]
        if head == "id":
            return setup.Result(0 if argv[2] in self.users else 1, "1001\n")
        if head == "dpkg":
            return setup.Result(0 if argv[2] in self.packages else 1, "")
        if head == "systemctl":
            return setup.Result(0 if argv[-1] in self.active else 3, "")
        if head.endswith("python") and "-c" in argv:
            module = argv[-1].removeprefix("import ").strip()
            known = self.venv and (
                module in self.imports or module == "cardea.hosts.operation_worker"
            )
            return setup.Result(0 if known else 1, "")
        if head == "openssl" and "-checkend" in argv:
            return setup.Result(0, "Certificate will not expire\n")
        if head.endswith("spire-server") and argv[1] == "entry":
            lines = [f"SPIFFE ID        : {i}\nParent ID        : {p}\n" for i, p in self.entries]
            return setup.Result(0, "".join(lines))
        if head.endswith("spire-agent"):
            return setup.Result(0 if "spire-agent" in self.active else 1, "Agent is healthy.")
        if head == "sudo":
            return setup.Result(0, '{"profile": "ok"}')
        raise AssertionError(f"unsimulated read {argv}")

    # Effects

    def _count(self, what: str) -> None:
        self.changes.append(what)
        if self.crash_at is not None and len(self.changes) == self.crash_at:
            raise Crash(what)

    def change(self, argv: list[str], *, input: str | None = None, cwd: Path | None = None) -> Any:
        self._count(" ".join(argv))
        head = argv[0]
        if head == "apt-get":
            self.packages |= set(argv[3:])
        elif head == "useradd":
            self.users.add(argv[-1])
        elif head == "uv" and argv[1] == "sync":
            self.venv = True
        elif head == "uv":
            self.imports.add(Path(argv[-1]).name.replace("-", "_"))
        elif head.endswith("python") and "sysconfig" in argv[-1]:
            return setup.Result(0, f"{self.base}/site\n")
        elif head == "sudo" and "init" in argv:
            config = json.loads(Path(argv[argv.index("--config") + 1]).read_text())
            (Path(config["data_dir"]) / "kernel.sqlite3").touch()
        elif head == "install" and argv[1] == "-d":
            target = Path(argv[-1])
            target.mkdir(parents=True, exist_ok=True)
            target.chmod(int(argv[argv.index("-m") + 1], 8))
        elif head == "systemctl" and argv[1] in ("enable", "restart"):
            self.active.add(argv[-1])
            if argv[-1] == "cardea-kernel":
                self.socket.touch()
        elif head == "systemctl" and argv[1] == "disable":
            self.active.discard(argv[-1])
        elif head == "openssl":
            for flag in ("-keyout", "-out"):
                if flag in argv:
                    Path(argv[argv.index(flag) + 1]).write_text("pem\n")
        elif head.endswith("spire-server") and argv[1] == "bundle":
            return setup.Result(0, "bundle\n")
        elif head.endswith("spire-server") and argv[1] == "entry":
            self.entries.append(
                (argv[argv.index("-spiffeID") + 1], argv[argv.index("-parentID") + 1])
            )
        elif head == "git":
            Path(argv[-1]).mkdir(parents=True)
        return setup.Result(0, "")

    def install(
        self, path: Path, content: str, mode: int = 0o600, owner: str | None = None
    ) -> bool:
        written = cast(bool, setup.HostSystem().install(path, content, mode))
        if written and path.name != "state.json":
            self._count(f"write {path}")
        return written


class Store:
    """A secret store that obtains missing secrets, as a prompting one would."""

    def __init__(self, files: dict[str, Path]) -> None:
        self.inner = setup.FileSecretStore(files)
        self.files = files
        self.provided: list[str] = []

    def status(self, name: str) -> Any:
        return self.inner.status(name)

    def provide(self, name: str) -> None:
        self.provided.append(name)
        self.files[name].parent.mkdir(parents=True, exist_ok=True)
        self.files[name].write_text("secret\n")
        self.files[name].chmod(0o600)


class Probe:
    def __init__(self, ready: bool = True) -> None:
        self.ready = ready
        self.calls = 0

    def check(self, alias: str) -> Any:
        self.calls += 1
        return setup.Verdict(self.ready, f"{alias}: ready" if self.ready else f"{alias}: not ready")


def host_document(base: Path) -> dict[str, Any]:
    document = cast(dict[str, Any], json.loads((KIT / "team.host.example.json").read_text()))
    document["credentials"]["reviewer_login"] = "reviewer-bot"
    document["credentials"]["expires_on"] = (
        (datetime.now(UTC) + timedelta(days=90)).date().isoformat()
    )
    document["kernel"]["socket"] = str(base / "kernel.sock")
    document["provisioning"].update(
        {
            "base": str(base / "etc/provision.base.json"),
            "out": str(base / "etc/provision.json"),
            "config": str(base / "etc/config.json"),
            "cardea": [str(ROOT / "apps/api/.venv/bin/python"), "-I", "-m", "cardea.service"],
        }
    )
    document["workers_config"] = str(base / "etc/workers.json")
    document["repositories_config"] = str(base / "code/repositories.json")
    # The session profile's wall in the small-host template is 1800 s.
    document["run_timeouts"] = {"pm": 1800, "tl": 900, "dev": 1800, "pe": 1800}
    document["setup"] = {
        "source": str(ROOT),
        "spire_dir": str(base / "spire"),
        "spire_bin": str(base / "spire-bin/bin"),
        "etc": str(base / "etc"),
        "data_dir": str(base / "data"),
        "systemd_dir": str(base / "systemd"),
        "operations_dir": str(base / "etc/operations"),
        "credentials_dir": str(base / "etc/credentials"),
        "vcs_config": str(base / "vcs/vcs.json"),
        "email_config": str(base / "etc/email.json"),
        "render_dir": str(base / "etc/team"),
        "state": str(base / "state/state.json"),
        "helper": str(base / "libexec/cardea-host"),
        "host_config": str(base / "etc/cardea-host.json"),
        "sudoers": str(base / "etc/sudoers"),
        "squid_config": str(base / "etc/squid.conf"),
        "repository": {
            "clone_url": "https://example.org/cardea.git",
            "entry": {
                "source": str(base / "code/repos/cardea.git"),
                "gates": [
                    {"name": "lint", "argv": ["/usr/bin/make", "lint"], "timeout_seconds": 600}
                ],
                "environment": {
                    "toolchain": {"path": "/opt/t", "sha256": "ab" * 32},
                    "scenarios": {s: {"argv": ["/usr/bin/make", s]} for s in ("try", "teams")},
                },
            },
        },
        "email": {
            "transport": {
                "kind": "agentmail",
                "inbox_id": "team@agentmail.test",
                "api_key_file": str(base / "etc/agentmail.key"),
            },
            "recipients": {"owner": "owner@mail.test"},
        },
    }
    return document


class Rig:
    def __init__(self, base: Path) -> None:
        base.mkdir(parents=True, exist_ok=True)
        self.base = base
        self.host = host_document(base)
        self.host_path = base / "team.host.json"
        self.host_path.write_text(json.dumps(self.host))
        self.host_path.chmod(0o600)
        for tool in ("spire-server", "spire-agent"):
            (base / "spire-bin/bin").mkdir(parents=True, exist_ok=True)
            (base / "spire-bin/bin" / tool).touch()
        self.fake = FakeHost(base)
        self.lines: list[str] = []
        self.store: Store | None = None
        self.probe = Probe()

    def context(self) -> Any:
        kit = setup.load_kit()
        layout = setup.Layout(self.host, kit)
        self.store = self.store or Store(layout.secret_files())
        return setup.Context(
            host=self.host,
            host_path=self.host_path,
            layout=layout,
            kit=kit,
            system=self.fake,
            secrets=self.store,
            probe=self.probe,
            state=setup.State(layout.state, self.fake),
            connect=lambda host: FakeKernel(self.fake.groups),
        )

    def apply(self) -> bool:
        return cast(bool, setup.apply_all(self.context(), self.lines.append))

    def plan(self) -> bool:
        return cast(bool, setup.plan(self.context(), self.lines.append))


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def snapshot(base: Path) -> dict[str, tuple[str, float]]:
    return {
        str(p): (p.read_text() if p.is_file() else "", p.stat().st_mtime_ns)
        for p in sorted(base.rglob("*"))
        if p.is_file()
    }


def test_a_fresh_host_converges_and_every_step_prints_its_verify(rig: Rig) -> None:
    assert rig.apply() is True, "\n".join(rig.lines)
    for name in STEP_NAMES:
        assert any(re.match(rf"\d\d {name}: .*verify (ok|FAILED)", line) for line in rig.lines), (
            name
        )
    assert not any("FAILED" in line for line in rig.lines)
    state = json.loads((rig.base / "state/state.json").read_text())
    assert {n: s["status"] for n, s in state["steps"].items()} == dict.fromkeys(STEP_NAMES, "done")
    assert (rig.base / "etc/team/group.json").exists()
    assert "dev-team" in rig.fake.groups
    assert ("spiffe://cardea.internal/supervisor/dev-team") in [i for i, _ in rig.fake.entries]
    # x509pop is the node attestor on both sides, and the entries hang off its agent.
    assert "x509pop" in (rig.base / "spire/server.conf").read_text()
    assert "join_token" not in (rig.base / "spire/agent.conf").read_text()
    assert all(p.endswith("/spire/agent/x509pop/cardea") for _, p in rig.fake.entries)
    # Secrets were asked for through the store and never read by setup.
    assert rig.store is not None and sorted(rig.store.provided) == [
        "email:key",
        "vcs:github",
        "vcs:github-reviewer",
    ]


def test_running_twice_changes_nothing(rig: Rig) -> None:
    assert rig.apply()
    before, effects, files = len(rig.fake.changes), list(rig.fake.changes), snapshot(rig.base)
    rig.lines.clear()
    assert rig.apply() is True
    assert rig.fake.changes[before:] == [], rig.fake.changes[before:]
    assert rig.fake.changes == effects
    assert snapshot(rig.base) == files
    for name in STEP_NAMES:
        assert any(
            re.match(rf"\d\d {name}: already done; verify ok", line) for line in rig.lines
        ), name
    assert rig.plan() is True


def test_plan_changes_nothing_and_reports_what_would_run(rig: Rig) -> None:
    before = snapshot(rig.base)
    assert rig.plan() is False
    assert rig.fake.changes == []
    assert snapshot(rig.base) == before
    assert any("would run" in line for line in rig.lines)
    assert len(rig.lines) == len(STEP_NAMES)
    assert all("verify " in line for line in rig.lines)
    assert any("repository-ready" in line and "not probed yet" in line for line in rig.lines)
    assert rig.probe.calls == 0
    assert not (rig.base / "state").exists()
    # Plan after a full apply says everything is done, and still prints each verify.
    assert rig.apply()
    rig.lines.clear()
    calls = rig.probe.calls
    assert rig.plan() is True
    assert rig.probe.calls == calls
    assert all("done; verify ok" in line for line in rig.lines) and len(rig.lines) == len(
        STEP_NAMES
    )


def test_plan_on_an_empty_host_reports_every_step_without_crashing(tmp_path: Path) -> None:
    # Nothing exists: no checkout, no SPIRE, no workers file, no state, no kernel socket.
    empty = Rig(tmp_path)
    for tool in (tmp_path / "spire-bin/bin").iterdir():
        tool.unlink()
    empty.host["setup"]["source"] = str(tmp_path / "missing-checkout")
    empty.host["kernel"]["socket"] = str(tmp_path / "missing/kernel.sock")
    empty.host_path.unlink()
    context = empty.context()
    context.connect = lambda host: (_ for _ in ()).throw(ConnectionRefusedError())
    before = snapshot(tmp_path)

    assert setup.plan(context, empty.lines.append) is False

    assert len(empty.lines) == len(STEP_NAMES)
    assert all("would run" in line for line in empty.lines), empty.lines
    assert all(re.search(r"verify (ok|FAILED): ", line) for line in empty.lines), empty.lines
    assert empty.fake.changes == [] and snapshot(tmp_path) == before


def test_every_check_and_verify_is_false_not_an_error_on_an_empty_host(tmp_path: Path) -> None:
    empty = Rig(tmp_path)
    for tool in (tmp_path / "spire-bin/bin").iterdir():
        tool.unlink()
    empty.host["setup"]["source"] = str(tmp_path / "missing-checkout")
    empty.host_path.unlink()
    context = empty.context()
    context.connect = lambda host: (_ for _ in ()).throw(ConnectionRefusedError())
    for step in setup.steps():
        if step.name == "repository-ready":
            continue  # its check is the probe, which the rig's Probe answers
        assert step.check(context) is False, step.name
        assert step.verify(context).ok is False, step.name


@pytest.mark.parametrize("error", [FileNotFoundError, ValueError, KeyError, TypeError])
def test_a_step_that_cannot_observe_checks_false_and_verifies_failed(
    rig: Rig, error: type[Exception]
) -> None:
    base: Any = setup.Step

    class Broken(base):  # type: ignore[misc]
        name = "broken"
        title = "broken"

        def is_done(self, ctx: Any) -> bool:
            raise error("gone")

        def action(self, ctx: Any) -> None:
            return None

        def observe(self, ctx: Any) -> Any:
            raise error("gone")

    context = rig.context()
    assert Broken().check(context) is False
    verdict = Broken().verify(context)
    assert verdict.ok is False
    assert verdict.detail.startswith("cannot observe: ")


def test_checks_and_verifies_are_bools_with_credentials_gone_and_malformed_provisioning(
    rig: Rig,
) -> None:
    assert rig.apply()
    context = rig.context()
    shutil.rmtree(rig.base / "etc/credentials", ignore_errors=True)
    Path(rig.host["provisioning"]["base"]).write_text("[]")
    Path(rig.host["provisioning"]["out"]).write_text("[]")
    for step in setup.steps():
        if step.name == "repository-ready":
            continue
        assert isinstance(step.check(context), bool), step.name
        assert isinstance(step.verify(context).ok, bool), step.name


@pytest.mark.parametrize("fraction", [0.1, 0.35, 0.6, 0.85, 0.99])
def test_killed_mid_run_then_rerun_completes(rig: Rig, fraction: float) -> None:
    probe = Rig(rig.base / "probe")
    assert probe.apply()
    total = len(probe.fake.changes)
    rig.fake.crash_at = max(1, int(total * fraction))
    with pytest.raises(Crash):
        rig.apply()
    state = json.loads((rig.base / "state/state.json").read_text())
    started = [n for n, s in state["steps"].items() if s["status"] == "started"]
    assert len(started) == 1
    rig.fake.crash_at = None
    rig.lines.clear()
    assert rig.apply() is True, "\n".join(rig.lines)
    # Steps finished before the kill are not repeated; the interrupted one resumes.
    assert any(f"{started[0]}: resuming after a started run" in line for line in rig.lines)
    done_before = STEP_NAMES[: STEP_NAMES.index(started[0])]
    for name in done_before:
        assert any(re.match(rf"\d\d {name}: already done", line) for line in rig.lines), name
    state = json.loads((rig.base / "state/state.json").read_text())
    assert {s["status"] for s in state["steps"].values()} == {"done"}


def test_a_failing_step_stops_the_run_and_the_next_run_resumes_there(rig: Rig) -> None:
    assert rig.store is None
    rig.context()
    assert rig.store is not None
    rig.store.provide = lambda name: (_ for _ in ()).throw(
        setup.SecretUnavailable(f"{name}: create the file")
    )
    assert rig.apply() is False
    assert any(
        re.match(r"\d\d tokens: FAILED: vcs:github: create the file", line) for line in rig.lines
    )
    assert not any(
        line.split(":")[0].endswith(("email", "render", "provision")) for line in rig.lines
    )
    state = json.loads((rig.base / "state/state.json").read_text())
    assert state["steps"]["tokens"]["status"] == "failed"
    assert "email" not in state["steps"]
    # The owner (or another store) supplies the secrets; the run resumes at the token step.
    for path in rig.store.files.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("secret\n")
        path.chmod(0o600)
    rig.lines.clear()
    assert rig.apply() is True, "\n".join(rig.lines)
    assert any("tokens: resuming after a failed run" in line for line in rig.lines)


def test_the_default_secret_store_never_prompts_or_writes(tmp_path: Path) -> None:
    target = tmp_path / "credentials" / "github"
    store = setup.FileSecretStore({"vcs:github": target})
    assert store.status("vcs:github").ok is False
    with pytest.raises(setup.SecretUnavailable, match="create .*github"):
        store.provide("vcs:github")
    assert not target.exists()
    target.parent.mkdir()
    target.write_text("t")
    target.chmod(0o644)
    assert "readable by others" in store.status("vcs:github").detail
    target.chmod(0o600)
    assert store.status("vcs:github").ok


def test_tokens_expiring_within_a_week_fail_the_step(rig: Rig) -> None:
    rig.host["credentials"]["expires_on"] = (
        (datetime.now(UTC) + timedelta(days=3)).date().isoformat()
    )
    assert rig.apply() is False
    assert any(re.match(r"\d\d tokens: FAILED: .*expire on", line) for line in rig.lines)


def test_a_placeholder_host_file_stops_before_the_host_is_touched(rig: Rig) -> None:
    rig.host["credentials"]["reviewer_login"] = "reviewer-login.example"
    assert rig.apply() is False
    assert rig.fake.changes == []
    assert any("team-host: FAILED" in line and "placeholder" in line for line in rig.lines)


def test_a_loose_host_file_is_tightened_and_a_session_wall_overrun_is_refused(rig: Rig) -> None:
    rig.host_path.chmod(0o644)
    assert rig.apply()
    assert stat.S_IMODE(rig.host_path.stat().st_mode) == 0o600
    rig2 = Rig(rig.base / "second")
    rig2.host["run_timeouts"] = {"dev": 7200}
    assert rig2.apply() is False
    assert any("session wall" in line or "wall_seconds" in line for line in rig2.lines)


def test_a_repository_that_is_not_ready_fails_with_its_report(rig: Rig) -> None:
    rig.probe = Probe(ready=False)
    assert rig.apply() is False
    assert any("repository-ready: FAILED: cardea: not ready" in line for line in rig.lines)
    assert "dev-team" not in rig.fake.groups


def test_drift_is_repaired_and_a_changed_budget_is_applied_again(rig: Rig) -> None:
    assert rig.apply()
    rig.fake.active.discard("cardea-kernel")
    (rig.base / "etc/operations/env.try.json").unlink()
    rig.host["budgets"] = {**rig.host["budgets"], "team": {"runs": 10, "usd": 5.0}}
    rig.fake.changes.clear()
    rig.lines.clear()
    assert rig.apply()
    joined = "\n".join(rig.fake.changes)
    assert "systemctl enable --now cardea-kernel" in joined
    assert any(re.match(r"\d\d apply: the collections", line) for line in rig.lines)
    assert any(
        c.startswith("write ") and c.endswith("operations/env.try.json") for c in rig.fake.changes
    )


def test_grants_nearing_expiry_are_provisioned_again(rig: Rig) -> None:
    assert rig.apply()
    out = rig.base / "etc/provision.json"
    document = json.loads(out.read_text())
    soon = (datetime.now(UTC) + timedelta(hours=5)).isoformat()
    for permission in document["permissions"]:
        permission["expires_at"] = soon
    out.write_text(json.dumps(document))
    rig.lines.clear()
    assert rig.plan() is False
    assert any(re.match(r"\d\d provision: would run", line) for line in rig.lines)


def test_the_cli_plan_and_apply_print_per_step_lines(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    # `plan` on an unprepared host exits 2 and `apply` on a bad host file exits 1; the real
    # System is used, so the host file is made to fail the first step before anything runs.
    rig.host["credentials"]["reviewer_login"] = "x.example"
    rig.host_path.write_text(json.dumps(rig.host))
    assert setup.main(["apply", "--host", str(rig.host_path)]) == 1
    assert "01 team-host: FAILED" in capsys.readouterr().out
    assert os.path.exists(rig.base / "state/state.json")
