"""`vcs.review` refuses an APPROVE unless every check run on the pull request's head
succeeded (the principal engineer's rule, enforced here and not only by instructions).

The handler runs directly against the fake host: the refusals are the kernel-side rule, so
nothing here needs an agent run. A refused approval sends no review."""

import asyncio
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cardea_vcs_ops.config import FixedTokenConfig, VcsConfig
from cardea_vcs_ops.fake import FakeGitHub, FakeRepository
from cardea_vcs_ops.ops import Vcs
from pydantic import SecretStr

from cardea.service.operations import OperationContext, OperationRejected

NUMBER = 1


@pytest.fixture
def host(tmp_path: Path) -> FakeGitHub:
    fake = FakeGitHub(tmp_path / "host")
    fake.root.mkdir()
    fake.add("acme", "cardea")
    return fake


@pytest.fixture
def vcs(host: FakeGitHub) -> Vcs:
    config = VcsConfig(
        credentials=FixedTokenConfig(kind="test", token=SecretStr("test-token-0123456789")),
        allow_approve=True,
    )
    return Vcs(config, transport=host.transport(), registry=host.registry())


def open_pull(host: FakeGitHub, tmp_path: Path) -> tuple[FakeRepository, str]:
    """A pull request by another account, its branch pushed to the bare repository."""
    repo = host.repositories["acme/cardea"]
    work = tmp_path / "work"
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.test"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    subprocess.run(
        [*git, "-C", str(work), "commit", "-q", "--allow-empty", "-m", "Base"], check=True
    )
    subprocess.run(["git", "-C", str(work), "checkout", "-q", "-b", "agent/eng/x"], check=True)
    subprocess.run(
        [*git, "-C", str(work), "commit", "-q", "--allow-empty", "-m", "Change"], check=True
    )
    subprocess.run(
        ["git", "-C", str(work), "push", "-q", str(repo.path), "main", "agent/eng/x"], check=True
    )
    repo.pulls.append(
        {
            "number": NUMBER,
            "state": "open",
            "merged": False,
            "draft": False,
            "user": {"login": "author[bot]"},
            "head": {"ref": "agent/eng/x", "sha": ""},
            "base": {"ref": "main"},
        }
    )
    pull = repo.pull(NUMBER)
    assert pull is not None
    return repo, str(pull["head"]["sha"])


def caller() -> OperationContext:
    """An agent of group `eng`, with the kernel's content gate bound but finding nothing."""
    return OperationContext(
        task_id="t",
        namespace="eng",
        receipt_id="r",
        deadline=datetime.now(UTC) + timedelta(minutes=1),
        effect_key="e",
        group="eng",
        check_content=lambda **_: None,
    )


def approve(vcs: Vcs, key: str = "k") -> dict[str, Any]:
    context = caller()
    arguments = {
        "repository": "cardea",
        "number": NUMBER,
        "event": "APPROVE",
        "body": "Looks right.",
        "client_key": key,
    }
    return asyncio.run(vcs.review(arguments, context))  # type: ignore[arg-type]


def test_approval_with_every_check_passed_is_pinned_to_the_checked_head(
    host: FakeGitHub, vcs: Vcs, tmp_path: Path
) -> None:
    repo, head = open_pull(host, tmp_path)
    repo.pass_checks(head, "lint", "test")
    made = approve(vcs)
    assert made["state"] == "APPROVED" and made["commit_id"] == head


@pytest.mark.parametrize(
    ("case", "code", "named"),
    [
        ("failed", "approve_checks_failed", "test"),
        ("running", "approve_checks_pending", "lint"),
        ("action_required", "approve_checks_action_required", "test"),
        ("missing", "approve_checks_missing", ""),
        ("other_head", "approve_checks_missing", ""),
    ],
)
def test_approval_is_refused_and_nothing_is_reviewed_unless_every_check_passed(
    host: FakeGitHub, vcs: Vcs, tmp_path: Path, case: str, code: str, named: str
) -> None:
    repo, head = open_pull(host, tmp_path)
    if case == "other_head":
        repo.pass_checks("0" * 40, "lint", "test")  # green, but not on this head
    elif case != "missing":
        repo.pass_checks(head, "lint", "test")
    if case == "failed":
        repo.checks[head][1]["conclusion"] = "failure"
    elif case == "action_required":
        repo.checks[head][1]["conclusion"] = "action_required"
    elif case == "running":
        repo.checks[head][0].update(status="in_progress", conclusion=None)
    with pytest.raises(OperationRejected) as refused:
        approve(vcs)
    assert refused.value.code == code and named in str(refused.value)
    assert NUMBER not in repo.reviews


def test_skipped_and_neutral_checks_count_as_passed(
    host: FakeGitHub, vcs: Vcs, tmp_path: Path
) -> None:
    repo, head = open_pull(host, tmp_path)
    repo.pass_checks(head, "lint", "test")
    repo.checks[head][0]["conclusion"] = "skipped"
    repo.checks[head][1]["conclusion"] = "neutral"
    assert approve(vcs)["state"] == "APPROVED"


def test_request_changes_and_comments_need_no_passing_checks(
    host: FakeGitHub, vcs: Vcs, tmp_path: Path
) -> None:
    repo, head = open_pull(host, tmp_path)
    repo.pass_checks(head, "test")
    repo.checks[head][0]["conclusion"] = "failure"
    context = caller()
    for event in ("REQUEST_CHANGES", "COMMENT"):
        arguments = {
            "repository": "cardea",
            "number": NUMBER,
            "event": event,
            "body": "The test check failed.",
            "client_key": event,
        }
        asyncio.run(vcs.review(arguments, context))  # type: ignore[arg-type]
    assert len(repo.reviews[NUMBER]) == 2
