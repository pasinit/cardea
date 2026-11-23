"""The repository mirror takes a pushed branch from the staged change bundle, so `env.try`
can check out a pull request's head (ADR-037, ADR-038); real git repositories."""

import json
import logging
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cardea_env_ops.try_ import stage

from cardea.service import mirror
from cardea.service.operations import OperationContext
from cardea.service.repositories import REGISTRY

CLEAN = {
    "PATH": os.environ["PATH"],
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}
BRANCH = "agent/team/fix"


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(cwd), *args], env=CLEAN, capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


class World:
    """A mirror (bare) and a working clone that makes the change and its bundle."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.mirror = root / "mirror.git"
        self.work = root / "work"
        subprocess.run(
            ["git", "init", "-q", "--bare", "-b", "main", str(self.mirror)], env=CLEAN, check=True
        )
        subprocess.run(["git", "clone", "-q", str(self.mirror), str(self.work)], env=CLEAN)
        git(self.work, "checkout", "-q", "-b", "main")
        (self.work / "a.txt").write_text("base\n")
        git(self.work, "add", ".")
        git(self.work, "commit", "-q", "-m", "base")
        git(self.work, "push", "-q", "origin", "main")
        self.base = git(self.work, "rev-parse", "HEAD")
        git(self.work, "checkout", "-q", "-b", "change")
        (self.work / "b.txt").write_text("change\n")
        git(self.work, "add", ".")
        git(self.work, "commit", "-q", "-m", "change")
        self.head = git(self.work, "rev-parse", "HEAD")
        self.bundle = root / "change.bundle"
        git(self.work, "bundle", "create", "-q", str(self.bundle), f"{self.base}..HEAD")
        self.registry = root / "repositories.json"
        entry = {
            "source": str(self.mirror),
            "owner": "acme",
            "name": "app",
            "api_url": "https://api.github.com",
            "git_url": "https://github.com/acme/app.git",
        }
        document = {"repositories": {"app": entry, "ro": {"source": str(self.mirror)}}}
        self.registry.write_text(json.dumps(document))
        self.registry.chmod(0o600)

    def pushed(
        self, bundle: bytes | None = None, **arguments: str
    ) -> tuple[dict[str, str], OperationContext]:
        given = {"repository": "app", "branch": BRANCH, "head": self.head, **arguments}
        data = self.bundle.read_bytes() if bundle is None else bundle
        context = OperationContext(
            task_id="t",
            namespace="n",
            receipt_id="r",
            deadline=datetime.now(UTC),
            effect_key="e",
            inputs={"/change": data},
        )
        return given, context

    def after_push(self, bundle: bytes | None = None, **arguments: str) -> None:
        given, context = self.pushed(bundle, **arguments)
        mirror.after_push(given, context, {REGISTRY: str(self.registry)})

    def heads(self) -> str:
        return git(self.mirror, "for-each-ref", "--format=%(refname) %(objectname)")

    def checkout(self, commit: str) -> Path:
        scratch = self.root / f"scratch-{commit[:8]}"
        scratch.mkdir()
        return stage(str(self.mirror), commit, scratch)


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def refused(world: World, commit: str) -> str:
    with pytest.raises(Exception) as error:
        world.checkout(commit)
    return getattr(error.value, "code", str(error.value))


def test_the_pushed_head_is_unknown_until_the_mirror_is_refreshed(world: World) -> None:
    assert refused(world, world.head) == "env_commit_unknown"
    world.after_push()
    assert (world.checkout(world.head) / "b.txt").read_text() == "change\n"
    assert world.heads() == (
        f"refs/heads/{BRANCH} {world.head}\nrefs/heads/main {world.base}"
    )  # the scratch ref is gone


def test_an_unknown_sha_is_still_refused(world: World) -> None:
    world.after_push()
    assert refused(world, "1" * 40) == "env_commit_unknown"


@pytest.mark.parametrize(
    "arguments",
    [
        {"head": "0" * 40},  # the bundle holds no such commit
        {"head": "abc"},  # not 40 hex
        {"head": "A" * 40},
        {"branch": "../escape"},
        {"branch": "agent/x y"},
        {"branch": "-bad"},
        {"repository": "other"},  # not in the registry
        {"repository": "ro"},  # nothing hosted: vcs.* does not act on it
    ],
)
def test_a_mismatch_changes_nothing(world: World, arguments: dict[str, str]) -> None:
    before = world.heads()
    world.after_push(**arguments)
    assert world.heads() == before
    assert refused(world, world.head) == "env_commit_unknown"


def test_a_head_the_bundle_does_not_hold_does_not_move_a_branch(world: World) -> None:
    other = git(world.work, "rev-parse", f"{world.base}")
    before = world.heads()
    world.after_push(head=other)  # real commit, but the bundle's ref is another one
    assert world.heads() == before


def test_a_garbage_bundle_is_logged_not_raised(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    before = world.heads()
    with caplog.at_level(logging.WARNING, logger=mirror.__name__):
        world.after_push(bundle=b"not a bundle")
    assert world.heads() == before
    assert any("mirror refresh" in record.getMessage() for record in caplog.records)


def test_an_unsafe_registry_is_logged_not_raised(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    world.registry.chmod(0o666)  # unsafe: the registry is refused
    with caplog.at_level(logging.WARNING, logger=mirror.__name__):
        world.after_push()
    assert world.heads() == f"refs/heads/main {world.base}"
    assert any("mirror refresh failed" in record.getMessage() for record in caplog.records)


def test_hooks_in_the_mirror_do_not_run(world: World) -> None:
    hook = world.mirror / "hooks" / "reference-transaction"
    hook.write_text(f"#!/bin/sh\ntouch {world.root / 'ran'}\n")
    hook.chmod(0o755)
    world.after_push()
    assert not (world.root / "ran").exists()
    assert f"refs/heads/{BRANCH} {world.head}" in world.heads()
