"""`code.session` against a mirror that lags the repository host (work item rec-b17bb608).

The host's public read endpoint is a fake opener standing in for `urllib` (no sockets), and
the host's git endpoint is a local bare repository; the mirror is another one, behind both.
"""

import io
import json
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from cardea.service.operations import OperationRejected, WithArtifacts

API = "https://api.host.invalid"

HARNESS = """
import pathlib
pathlib.Path("change.txt").write_text("follow-up\\n")
"""


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


class Host:
    """The repository host: a bare repository and its public ref endpoint."""

    def __init__(self, tmp_path: Path) -> None:
        self.work = tmp_path / "work"
        self.work.mkdir()
        git(self.work, "init", "--quiet", "--initial-branch=main")
        self.commit("README", "one\n")
        self.git_url = tmp_path / "host.git"
        git(tmp_path, "clone", "--quiet", "--bare", str(self.work), str(self.git_url))
        # The mirror is a copy taken now; the host moves on afterwards.
        self.mirror = tmp_path / "mirror.git"
        git(tmp_path, "clone", "--quiet", "--bare", str(self.work), str(self.mirror))
        self.requests: list[dict[str, str]] = []
        self.public = True

    def commit(self, name: str, text: str) -> str:
        (self.work / name).write_text(text)
        git(self.work, "add", name)
        git(self.work, "commit", "--quiet", "-m", name)
        return git(self.work, "rev-parse", "HEAD").strip()

    def push(self, *refs: str) -> None:
        git(self.work, "push", "--quiet", str(self.git_url), *refs)

    def head(self, branch: str) -> str | None:
        done = subprocess.run(
            ["git", "rev-parse", f"refs/heads/{branch}"],
            cwd=self.git_url,
            capture_output=True,
            text=True,
            check=False,
        )
        return done.stdout.strip() if done.returncode == 0 else None

    @contextmanager
    def serving(self) -> Iterator[str]:
        """Answer `GET /repos/acme/app/git/ref/heads/<branch>` as the host's public endpoint
        would: the branch's head, or 404 for a branch or a repository that is not public."""
        host = self

        class Opener:
            def open(self, request: urllib.request.Request, timeout: float) -> io.BytesIO:
                host.requests.append({"url": request.full_url, **dict(request.header_items())})
                prefix = f"{API}/repos/acme/app/git/ref/heads/"
                assert request.full_url.startswith(prefix)
                sha = host.head(request.full_url.removeprefix(prefix)) if host.public else None
                if sha is None:
                    raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)  # type: ignore[arg-type]
                return io.BytesIO(json.dumps({"object": {"sha": sha}}).encode())

        original = urllib.request.build_opener
        urllib.request.build_opener = lambda *handlers: Opener()  # type: ignore[assignment]
        try:
            yield API
        finally:
            urllib.request.build_opener = original


@pytest.fixture(autouse=True)
def no_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CARDEA_REPOSITORIES", raising=False)


@pytest.fixture
def host(tmp_path: Path) -> Host:
    return Host(tmp_path)


@contextmanager
def unreachable() -> Iterator[None]:
    class Opener:
        def open(self, request: urllib.request.Request, timeout: float) -> io.BytesIO:
            raise urllib.error.URLError("no route to host")

    original = urllib.request.build_opener
    urllib.request.build_opener = lambda *handlers: Opener()  # type: ignore[assignment]
    try:
        yield
    finally:
        urllib.request.build_opener = original


def run(
    tmp_path: Path,
    host: Host,
    api_url: str | None,
    base_ref: str = "main",
    git_url: object = "default",
    ran: Path | None = None,
) -> tuple[dict[str, Any], bytes]:
    from cardea_coding_ops.session import session

    scratch = tmp_path / "scratch"
    scratch.mkdir(parents=True)
    script = tmp_path / "harness.py"
    script.write_text(
        HARNESS + (f"open({str(ran)!r}, 'w').write('ran')\n" if ran is not None else "")
    )
    entry: dict[str, Any] = {"source": str(host.mirror), "gates": []}
    if api_url is not None:
        entry |= {
            "owner": "acme",
            "name": "app",
            "api_url": api_url,
            "git_url": str(host.git_url) if git_url == "default" else git_url,
        }
    config = {"harness": {"argv": [sys.executable, str(script)]}, "repositories": {"app": entry}}
    arguments: dict[str, Any] = {"repository": "app", "base_ref": base_ref, "task": "go"}
    result = session(arguments, config, scratch)
    if isinstance(result, WithArtifacts):
        return result.output, result.artifacts["change.bundle"]
    assert isinstance(result, dict)
    return result, b""


def test_a_lagging_mirror_is_caught_up_from_the_hosts_public_endpoint(
    tmp_path: Path, host: Host
) -> None:
    stale = host.head("main")
    newest = host.commit("later", "two\n")
    host.push("main")
    with host.serving() as api:
        output, bundle = run(tmp_path, host, api)
    assert output["base_sha"] == newest != stale
    assert output["changed"] is True and bundle
    # The change sits on the host's head, so it fast-forwards the branch there.
    (tmp_path / "change.bundle").write_bytes(bundle)
    git(host.git_url, "fetch", "--quiet", str(tmp_path / "change.bundle"), "main:refs/heads/next")
    assert git(host.git_url, "merge-base", "--is-ancestor", newest, "next") == ""
    # The request carried no credential of any kind.
    assert host.requests
    assert not {"Authorization", "Cookie"} & {k.title() for r in host.requests for k in r}


def test_a_branch_pushed_after_the_mirror_refresh_is_the_base(tmp_path: Path, host: Host) -> None:
    git(host.work, "checkout", "--quiet", "-b", "agent/dev/feature")
    tip = host.commit("feature", "first change\n")
    host.push("agent/dev/feature")
    with host.serving() as api:
        output, _ = run(tmp_path, host, api, base_ref="agent/dev/feature")
    assert output["base_sha"] == tip
    assert output["base_ref"] == "agent/dev/feature"


def test_a_stale_mirror_is_refused_when_the_hosts_commits_cannot_be_fetched(
    tmp_path: Path, host: Host
) -> None:
    host.commit("later", "two\n")
    host.push("main")
    ran = tmp_path / "ran"
    with host.serving() as api, pytest.raises(OperationRejected) as refused:
        run(tmp_path, host, api, git_url=None, ran=ran)
    assert refused.value.code == "mirror_stale"
    assert "refresh the mirror" in str(refused.value)
    assert not ran.exists()


def test_a_stale_mirror_is_refused_when_the_hosts_git_endpoint_is_unreachable(
    tmp_path: Path, host: Host
) -> None:
    host.commit("later", "two\n")
    host.push("main")
    ran = tmp_path / "ran"
    with host.serving() as api, pytest.raises(OperationRejected) as refused:
        run(tmp_path, host, api, git_url=str(tmp_path / "gone.git"), ran=ran)
    assert refused.value.code == "mirror_stale"
    assert not ran.exists()


def test_a_git_url_that_could_carry_a_credential_is_not_used(tmp_path: Path, host: Host) -> None:
    host.commit("later", "two\n")
    host.push("main")
    with host.serving() as api, pytest.raises(OperationRejected) as refused:
        run(tmp_path, host, api, git_url="ssh://git@example.invalid/acme/app.git")
    assert refused.value.code == "mirror_stale"


def test_a_current_mirror_needs_no_fetch(tmp_path: Path, host: Host) -> None:
    with host.serving() as api:
        # The git endpoint is unusable, so any fetch would fail the call.
        output, _ = run(tmp_path, host, api, git_url=str(tmp_path / "gone.git"))
    assert output["base_sha"] == host.head("main")
    assert len(host.requests) == 1


def test_a_repository_that_is_not_public_keeps_the_mirror_as_before(
    tmp_path: Path, host: Host
) -> None:
    host.public = False
    mirrored = git(host.mirror, "rev-parse", "main").strip()
    host.commit("later", "two\n")
    host.push("main")
    with host.serving() as api:
        output, _ = run(tmp_path, host, api)
    assert output["base_sha"] == mirrored


def test_an_unreachable_read_endpoint_keeps_the_mirror_as_before(
    tmp_path: Path, host: Host
) -> None:
    with unreachable():
        output, _ = run(tmp_path, host, API)
    assert output["base_sha"] == git(host.mirror, "rev-parse", "main").strip()


def test_a_repository_without_a_host_is_unchanged(tmp_path: Path, host: Host) -> None:
    output, _ = run(tmp_path, host, None)
    assert output["base_sha"] == git(host.mirror, "rev-parse", "main").strip()
    assert host.requests == []


def test_an_unknown_branch_is_still_refused(tmp_path: Path, host: Host) -> None:
    with host.serving() as api, pytest.raises(OperationRejected, match="could not be checked out"):
        run(tmp_path, host, api, base_ref="agent/dev/nowhere")


def test_the_receipt_says_whether_the_host_confirmed_the_base(tmp_path: Path, host: Host) -> None:
    with host.serving() as api:
        confirmed, _ = run(tmp_path, host, api)
    assert confirmed["base_checked"] is True
    with unreachable():
        unchecked, _ = run(tmp_path / "again", host, API)
    assert unchecked["base_checked"] is False
