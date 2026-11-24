"""The host-side readiness check (`cardea-ready.py`) and how a deploy uses it, with fakes:
no kernel, no systemd and no network."""

import importlib.util
import json
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parents[4]
SCRIPTS = ROOT / "deploy/small-host/cardea"
# A fictional token value; it must never appear in any output.
TOKEN = "tok-example-0123456789abcdef"


def load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


ready = load("cardea-ready")
API = "https://api.example.invalid"
REGISTRY = {
    "repositories": {
        "app": {
            "source": "/opt/cardea-code/repos/app.git",
            "owner": "example-org",
            "name": "app",
            "api_url": API,
            "git_url": "https://git.example.invalid/example-org/app.git",
        },
        "docs-only": {"source": "/opt/cardea-code/repos/docs.git"},
    }
}


def vcs(allow_approve: Any = False, pools: dict[str, str] | None = None) -> dict[str, Any]:
    return {
        "allow_approve": allow_approve,
        "credentials": {
            "kind": "personal_access_tokens",
            "tokens": {
                "author": {"expires_on": "2099-01-01"},
                "reviewer": {"expires_on": "2099-01-01"},
            },
            "default": "author",
            "pools": pools or {},
        },
    }


class Host:
    """The repository host: per token value, the account's role and what the token holds."""

    def __init__(
        self,
        *,
        status: int = 200,
        push: bool = True,
        granted: set[str] | None = None,
        scopes: str | None = None,
    ) -> None:
        self.status, self.push, self.scopes = status, push, scopes
        self.granted = granted if granted is not None else {"git/refs", "pulls"}
        self.calls: list[tuple[str, str, bytes | None]] = []

    def __call__(self, method: str, url: str, token: str, body: bytes | None) -> Any:
        assert token == TOKEN
        self.calls.append((method, url, body))
        headers = {} if self.scopes is None else {"x-oauth-scopes": self.scopes}
        if method == "GET":
            document = {"private": True, "permissions": {"push": self.push, "pull": True}}
            return ready.Answer(self.status, headers, document if self.status == 200 else None)
        assert body == b"{}", "a probe never sends anything that could change state"
        path = url.removeprefix(f"{API}/repos/example-org/app/")
        return ready.Answer(422 if path in self.granted else 403, headers, None)


@pytest.fixture
def tokens(tmp_path: Path) -> dict[str, Any]:
    """The worker profile's credential files, as `cardea-host-workers.json` names them."""
    paths = {}
    for name in ("author", "reviewer"):
        path = tmp_path / name
        path.write_text(TOKEN + "\n")
        paths[name] = str(path)
    return {"profiles": {"vcs": {"credentials": paths}}}


def by_name(checks: list[Any]) -> dict[str, Any]:
    return {check.name: check for check in checks}


# Which token needs which permission


def test_default_pushes_and_approving_pools_need_pull_requests() -> None:
    needs = ready.token_needs(vcs(["team.reviewer"], {"team.reviewer": "reviewer"}))
    assert needs == {"author": {"contents"}, "reviewer": {"pull_requests"}}


def test_as_installed_every_caller_pushes_with_the_author_token() -> None:
    assert ready.token_needs(vcs()) == {"author": {"contents"}, "reviewer": set()}


def test_approval_for_everyone_needs_pull_requests_on_every_token() -> None:
    needs = ready.token_needs(vcs(True, {"team.reviewer": "reviewer"}))
    assert needs == {"author": {"contents", "pull_requests"}, "reviewer": {"pull_requests"}}


def test_an_approving_pool_without_its_own_token_approves_with_default() -> None:
    needs = ready.token_needs(vcs(["team.lead"]))
    assert needs["author"] == {"contents", "pull_requests"}


def test_the_token_file_is_the_one_the_profile_loads() -> None:
    workers = {"profiles": {"vcs": {"credentials": {"author": "/etc/example/author"}}}}
    assert ready.credential_path("author", workers) == Path("/etc/example/author")
    assert ready.credential_path("other", workers) == Path("/etc/cardea/credentials/other")


# Token checks


def test_tokens_with_the_permissions_they_need_pass(tokens: dict[str, Any]) -> None:
    config = vcs(["team.reviewer"], {"team.reviewer": "reviewer"})
    host = Host()
    checks = by_name(ready.token_checks(config, REGISTRY, tokens, host, date(2026, 1, 1)))
    assert checks["token:author:app"].ok and checks["token:reviewer:app"].ok
    assert "contents: write" in checks["token:author:app"].detail
    # Only the repository vcs.* acts on is checked, and only the needed probes are sent.
    assert not any("docs" in name for name in checks)
    posts = [url for method, url, _ in host.calls if method == "POST"]
    assert sorted(posts) == [
        f"{API}/repos/example-org/app/git/refs",
        f"{API}/repos/example-org/app/pulls",
    ]


def test_a_fine_grained_token_without_contents_write_fails(tokens: dict[str, Any]) -> None:
    checks = by_name(ready.token_checks(vcs(), REGISTRY, tokens, Host(granted={"pulls"})))
    assert checks["token:author:app"].ok is False
    assert checks["token:author:app"].detail == "lacks contents: write"


def test_a_reviewer_token_without_pull_requests_write_fails(tokens: dict[str, Any]) -> None:
    config = vcs(["team.reviewer"], {"team.reviewer": "reviewer"})
    checks = by_name(ready.token_checks(config, REGISTRY, tokens, Host(granted={"git/refs"})))
    assert checks["token:author:app"].ok is True
    assert checks["token:reviewer:app"].detail == "lacks pull_requests: write"


def test_an_account_without_push_access_fails(tokens: dict[str, Any]) -> None:
    checks = by_name(ready.token_checks(vcs(), REGISTRY, tokens, Host(push=False)))
    assert "push access for its account" in checks["token:author:app"].detail


def test_a_classic_token_needs_the_repo_scope(tokens: dict[str, Any]) -> None:
    bad = by_name(ready.token_checks(vcs(), REGISTRY, tokens, Host(scopes="read:org")))
    assert bad["token:author:app"].detail == "lacks the repo scope"
    host = Host(scopes="repo, read:org")
    good = by_name(ready.token_checks(vcs(), REGISTRY, tokens, host))
    assert good["token:author:app"].ok
    assert not any(method == "POST" for method, _, _ in host.calls)


def test_a_rejected_or_blind_token_fails(tokens: dict[str, Any]) -> None:
    rejected = by_name(ready.token_checks(vcs(), REGISTRY, tokens, Host(status=401)))
    assert "invalid or revoked" in rejected["token:author:app"].detail
    blind = by_name(ready.token_checks(vcs(), REGISTRY, tokens, Host(status=404)))
    assert "cannot read example-org/app (HTTP 404)" in blind["token:author:app"].detail


def test_an_expired_or_missing_token_is_not_sent(tokens: dict[str, Any], tmp_path: Path) -> None:
    config = vcs()
    config["credentials"]["tokens"]["author"]["expires_on"] = "2026-01-01"
    host = Host()
    checks = by_name(ready.token_checks(config, REGISTRY, tokens, host, date(2026, 6, 1)))
    assert checks["token:author"].detail == "expired on 2026-01-01 (vcs configuration)"
    # Only the reviewer's token, which needs no write permission, was sent: one read.
    assert [method for method, _, _ in host.calls] == ["GET"]
    tokens["profiles"]["vcs"]["credentials"]["author"] = str(tmp_path / "absent")
    checks = by_name(ready.token_checks(vcs(), REGISTRY, tokens, Host()))
    assert (
        checks["token:author"].ok is False and "FileNotFoundError" in checks["token:author"].detail
    )


def test_the_token_never_appears_in_any_output(tokens: dict[str, Any]) -> None:
    def leaky(method: str, url: str, token: str, body: bytes | None) -> Any:
        raise OSError(f"connection failed while sending {token}")

    checks = ready.token_checks(vcs(), REGISTRY, tokens, leaky)
    rendered = json.dumps([c.document() for c in checks])
    assert TOKEN not in rendered and "[redacted]" in rendered
    assert all(not c.ok for c in checks if c.name.startswith("token:author"))


# The kernel and the proxy


def test_kernel_answers_become_named_checks() -> None:
    body = {
        "ready": False,
        "as_of": "2026-01-01T00:00:00+00:00",
        "checks": [
            {"name": "database", "ok": True, "detail": "fine"},
            {"name": "supervision", "ok": False, "detail": "task-1 stuck"},
        ],
    }
    checks = by_name(ready.kernel_checks({"reached": True, "status": 503, "body": body}, False))
    assert checks["kernel"].ok and checks["kernel.database"].ok
    assert checks["kernel.supervision"].ok is False


def test_a_paused_team_fails_unless_allowed() -> None:
    outcome = {"reached": True, "status": 403, "error": "principal_disabled"}
    assert ready.kernel_checks(outcome, False)[0].ok is False
    allowed = ready.kernel_checks(outcome, True)[0]
    assert allowed.ok and "paused" in allowed.detail


def test_an_unreachable_or_older_kernel_fails() -> None:
    down = ready.kernel_checks({"reached": False, "error": "ConnectError"}, False)
    assert down[0].detail == "kernel did not answer: ConnectError"
    old = ready.kernel_checks({"reached": True, "status": 404, "error": "http_error"}, False)
    assert old[0].ok is False and "HTTP 404" in old[0].detail


def test_the_kernel_is_asked_as_the_kernel_user_without_the_token() -> None:
    seen: list[list[str]] = []

    def runner(argv: list[str], **kwargs: Any) -> Any:
        seen.append(argv)
        line = json.dumps({"reached": True, "status": 200, "body": {"ready": True, "checks": []}})
        return subprocess.CompletedProcess(argv, 0, stdout="noise\n" + line + "\n", stderr="")

    outcome = ready.call_kernel(Path("/etc/example/team.host.json"), True, runner)
    assert outcome["status"] == 200
    assert seen[0][:4] == ["runuser", "-u", "cardea-kernel", "--"]
    assert seen[0][-3:] == ["--host", "/etc/example/team.host.json", "--allow-paused"]
    garbled = ready.call_kernel(
        Path("/h"), False, lambda argv, **_: subprocess.CompletedProcess(argv, 2, "", "boom")
    )
    assert garbled == {"reached": False, "error": "teamkit ready failed (exit 2)"}


def test_squid_must_be_active() -> None:
    def state(value: str) -> Any:
        return lambda argv, **_: subprocess.CompletedProcess(argv, 0, value + "\n", "")

    assert ready.squid_check(state("active")).ok
    inactive = ready.squid_check(state("inactive"))
    assert inactive.ok is False and inactive.detail == "squid is inactive"


def test_the_whole_report_and_exit_code(tokens: dict[str, Any], tmp_path: Path) -> None:
    (tmp_path / "vcs.json").write_text(json.dumps(vcs()))
    (tmp_path / "repositories.json").write_text(json.dumps(REGISTRY))
    (tmp_path / "workers.json").write_text(json.dumps(tokens))

    def runner(argv: list[str], **kwargs: Any) -> Any:
        if argv[0] == "systemctl":
            return subprocess.CompletedProcess(argv, 0, "active\n", "")
        body = {
            "ready": True,
            "as_of": "x",
            "checks": [{"name": "database", "ok": True, "detail": "d"}],
        }
        return subprocess.CompletedProcess(argv, 0, json.dumps({"reached": True, "body": body}), "")

    result = ready.readiness(
        Path("/h"),
        vcs_path=tmp_path / "vcs.json",
        repositories_path=tmp_path / "repositories.json",
        workers_path=tmp_path / "workers.json",
        runner=runner,
        http=Host(),
    )
    assert result["ready"] is True, result
    text = ready.report(result)
    assert text.startswith("ready: yes") and "kernel.database" in text and TOKEN not in text
    failing = ready.readiness(
        Path("/h"),
        vcs_path=tmp_path / "vcs.json",
        repositories_path=tmp_path / "repositories.json",
        workers_path=tmp_path / "workers.json",
        runner=runner,
        http=Host(granted=set()),
    )
    assert failing["ready"] is False and "FAIL  token:author:app" in ready.report(failing)


def test_the_script_runs_isolated_with_the_standard_library_only() -> None:
    done = subprocess.run(
        [sys.executable, "-I", str(SCRIPTS / "cardea-ready.py"), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0 and "--allow-paused" in done.stdout


# How a deploy uses it


def test_only_checks_that_passed_before_count_as_regressions() -> None:
    def result(**states: bool) -> dict[str, Any]:
        return {"checks": [{"name": n, "ok": ok} for n, ok in states.items()]}

    before = result(kernel=True, squid=True, **{"token:author:app": False})
    after = result(
        kernel=False, squid=True, **{"token:author:app": False, "kernel.database": False}
    )
    # The token was already failing and the database check is new: neither is the deploy's.
    assert ready.regressions(before, after) == ["kernel"]
    assert ready.regressions(None, after) == []


def test_a_deploy_waits_for_regressions_to_clear_then_fails_on_what_remains() -> None:
    update = load("cardea-update")
    baseline = {"checks": [{"name": "kernel", "ok": True}, {"name": "squid", "ok": True}]}
    answers = iter(
        [
            {"checks": [{"name": "kernel", "ok": False}, {"name": "squid", "ok": True}]},
            {"checks": [{"name": "kernel", "ok": True}, {"name": "squid", "ok": True}]},
        ]
    )

    class Ready:
        regressions = staticmethod(ready.regressions)

        @staticmethod
        def readiness(host: Path) -> dict[str, Any]:
            return next(answers)

    slept: list[float] = []
    found = update.ready_regressions(Path("/h"), baseline, Ready, sleep=slept.append)
    assert found == [] and slept == [10]

    class Broken(Ready):
        @staticmethod
        def readiness(host: Path) -> dict[str, Any]:
            return {"checks": [{"name": "kernel", "ok": True}, {"name": "squid", "ok": False}]}

    clock = iter([0.0, 50.0, 130.0])
    found = update.ready_regressions(
        Path("/h"), baseline, Broken, sleep=lambda _: None, clock=lambda: next(clock)
    )
    assert found == ["squid"]

    class Crashing(Ready):
        @staticmethod
        def readiness(host: Path) -> dict[str, Any]:
            raise OSError("systemctl missing")

    assert update.readiness(Path("/h"), Crashing) is None
    clock = iter([0.0, 130.0])
    found = update.ready_regressions(
        Path("/h"), baseline, Crashing, sleep=lambda _: None, clock=lambda: next(clock)
    )
    assert found == ["cardea-ready"]
    # Without a baseline (the previous checkout had no cardea-ready) nothing is compared.
    assert update.ready_regressions(Path("/h"), None, Broken) == []
