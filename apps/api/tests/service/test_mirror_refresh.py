"""The repository mirror after a push (ADR-037): the pushed head becomes checkable by
`env.try`, which stays offline and still refuses any SHA the mirror lacks. Real git."""

import json
import logging
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cardea_env_ops.try_ import stage

from cardea.service import mirror
from cardea.service.operations import OperationContext, OperationRejected


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


class Fixture:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.work = tmp_path / "work"
        self.work.mkdir()
        git(self.work, "init", "--quiet", "--initial-branch=main")
        (self.work / "f").write_text("base\n")
        git(self.work, "add", "f")
        git(self.work, "commit", "--quiet", "-m", "base")
        self.base = git(self.work, "rev-parse", "HEAD")
        self.source = tmp_path / "source.git"
        git(tmp_path, "clone", "--quiet", "--bare", str(self.work), str(self.source))

    def change(self, text: str = "change\n") -> tuple[bytes, str]:
        """A new commit on `main` of the work repository, bundled thin over the base."""
        (self.work / "f").write_text(text)
        git(self.work, "commit", "--quiet", "-am", text.strip())
        head = git(self.work, "rev-parse", "HEAD")
        bundle = self.tmp / f"{head}.bundle"
        git(self.work, "bundle", "create", "--quiet", str(bundle), f"{self.base}..main")
        return bundle.read_bytes(), head

    def registry(self, **entry: Any) -> dict[str, str]:
        hosted = {
            "owner": "acme",
            "name": "app",
            "api_url": "https://api.example.invalid",
            "git_url": "https://git.example.invalid/acme/app.git",
        }
        body = {"source": str(self.source), **hosted, **entry}
        path = self.tmp / "repositories.json"
        path.write_text(json.dumps({"repositories": {"app": body}}))
        path.chmod(0o644)
        return {"CARDEA_REPOSITORIES": str(path)}

    def refs(self) -> str:
        return git(self.source, "for-each-ref", "--format=%(refname) %(objectname)")


@pytest.fixture
def fx(tmp_path: Path) -> Fixture:
    return Fixture(tmp_path)


def context(bundle: bytes | None) -> OperationContext:
    inputs = {} if bundle is None else {"/change": bundle}
    return OperationContext(
        "t", "eng", "r", datetime.now(UTC) + timedelta(minutes=1), "k", inputs=inputs
    )


def arguments(branch: str, head: str, repository: str = "app") -> dict[str, Any]:
    return {"repository": repository, "branch": branch, "head": head}


def test_the_pushed_head_becomes_checkable(fx: Fixture, tmp_path: Path) -> None:
    bundle, head = fx.change()
    scratch = tmp_path / "before"
    scratch.mkdir()
    with pytest.raises(OperationRejected) as refused:
        stage(str(fx.source), head, scratch)
    assert refused.value.code == "env_commit_unknown"

    mirror.after_push(arguments("agent/eng/fix", head), context(bundle), fx.registry())

    assert f"refs/heads/agent/eng/fix {head}" in fx.refs()
    assert "refs/cardea" not in fx.refs()
    after = tmp_path / "after"
    after.mkdir()
    assert git(stage(str(fx.source), head, after), "rev-parse", "HEAD") == head
    # An unknown SHA is still refused.
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(OperationRejected) as unknown:
        stage(str(fx.source), "1" * 40, other)
    assert unknown.value.code == "env_commit_unknown"


def test_a_mismatched_head_branch_or_repository_changes_nothing(fx: Fixture) -> None:
    bundle, head = fx.change()
    before = fx.refs()
    env = fx.registry()
    for args in (
        arguments("agent/eng/fix", "2" * 40),
        arguments("agent/eng/fix", head.upper()),
        arguments("agent/eng/fix", "abc"),
        arguments("agent/../main", head),
        arguments("-x", head),
        arguments("agent/eng/fix.lock", head),
        arguments("agent/eng/fix", head, "unknown"),
    ):
        mirror.after_push(args, context(bundle), env)
    mirror.after_push(arguments("agent/eng/fix", head), context(None), env)
    assert fx.refs() == before


def test_a_garbage_bundle_is_logged_and_changes_nothing(
    fx: Fixture, caplog: pytest.LogCaptureFixture
) -> None:
    _, head = fx.change()
    before = fx.refs()
    with caplog.at_level(logging.WARNING):
        mirror.after_push(arguments("agent/eng/fix", head), context(b"junk"), fx.registry())
    assert fx.refs() == before
    assert any(r.__dict__.get("code") == "mirror_refused" for r in caplog.records)


def test_an_unsafe_registry_is_logged_and_never_raised(
    fx: Fixture, caplog: pytest.LogCaptureFixture
) -> None:
    bundle, head = fx.change()
    env = fx.registry()
    Path(env["CARDEA_REPOSITORIES"]).chmod(0o666)
    with caplog.at_level(logging.WARNING):
        mirror.after_push(arguments("agent/eng/fix", head), context(bundle), env)
    assert any(r.__dict__.get("code") == "mirror_failed" for r in caplog.records)
    assert "agent/eng/fix" not in fx.refs()


def test_a_repository_without_a_host_is_left_alone(fx: Fixture) -> None:
    bundle, head = fx.change()
    path = fx.tmp / "readonly.json"
    path.write_text(json.dumps({"repositories": {"app": {"source": str(fx.source)}}}))
    path.chmod(0o644)
    mirror.after_push(
        arguments("agent/eng/fix", head), context(bundle), {"CARDEA_REPOSITORIES": str(path)}
    )
    assert "agent/eng/fix" not in fx.refs()


def test_hooks_in_the_mirror_do_not_run(fx: Fixture) -> None:
    bundle, head = fx.change()
    marker = fx.tmp / "ran"
    hook = fx.source / "hooks" / "reference-transaction"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    mirror.after_push(arguments("agent/eng/fix", head), context(bundle), fx.registry())
    assert f"refs/heads/agent/eng/fix {head}" in fx.refs()
    assert not marker.exists()
