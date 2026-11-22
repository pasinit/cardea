"""`vcs.merge` and per-caller repository credentials (ADR-037 amendment 2026-11-17).

A group with two developers and a principal reviewer. The developers act on the host as
one bot account, so they cannot approve each other's pull requests (the host refuses an
author's approval of its own pull request); the principal acts as a second account,
through its own token. A developer merges its own pull request once the kernel holds its
`vcs.open_change` receipt and an allowed `vcs.review` APPROVE receipt at the current head,
the host shows the reviewer's approval of that head, every check passed, and the host can
merge. In-process handlers against the fake host; the confined path is in
test_vcs_confined.py.
"""

import subprocess
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from cardea_vcs_ops import Vcs, in_process_descriptors
from cardea_vcs_ops.config import MergeConfig, NamedToken, PersonalAccessTokensConfig, VcsConfig
from cardea_vcs_ops.credentials import CredentialUnavailable, PersonalAccessTokens
from cardea_vcs_ops.fake import FakeGitHub
from test_agent_runs import FakeSession
from test_vcs_ops import (
    STEPS,
    Auth,
    FakeClient,
    gate_config,
    make_change,
    member_run,
    text,
)

from cardea.kernel.models import OperationState
from cardea.runtime.agent_session import SessionRequest, SessionResult
from cardea.service import agent_run_runtime
from cardea.service import kernel as handlers
from cardea.service.agent_run_runtime import AgentRunRuntime
from cardea.service.auth import Authentication
from cardea.service.database import Database, borrow
from cardea.service.groups import apply
from cardea.service.harnesses import IMPLICIT_ID, Catalog, Harnesses, ModelEntry, implicit_entry
from cardea.service.kernel import Kernel, provision
from cardea.service.models import RIGHTS, AgentGroup, Provisioning
from cardea.service.operation_runtime import OperationRuntime
from cardea.service.operations import OperationContext, Registry

# Planted for these tests; shaped so no secret scanner mistakes them for real tokens.
AUTHOR = "planted-author-token-not-real-0123456789abcdef"
REVIEWER = "planted-reviewer-token-not-real-0123456789abcd"
BOT, OWNER = "team-bot", "owner-reviewer"
DEV_OPS = ("vcs.push_branch", "vcs.open_change", "vcs.read_change", "vcs.merge")
PRINCIPAL_OPS = ("vcs.read_change", "vcs.review", "vcs.comment", "vcs.check_repository")
BASE = (*RIGHTS, "models.invoke", "repo:cardea")


def tokens_config() -> PersonalAccessTokensConfig:
    later = NamedToken(expires_on=date(2999, 1, 1))
    return PersonalAccessTokensConfig(
        kind="personal_access_tokens",
        tokens={"github-author": later, "github-reviewer": later},
        default="github-author",
        pools={"eng.principal": "github-reviewer"},
    )


def write_tokens(directory: Path) -> Path:
    directory.mkdir()
    for name, token in (("github-author", AUTHOR), ("github-reviewer", REVIEWER)):
        (directory / name).write_text(token + "\n")
        (directory / name).chmod(0o600)
    return directory


@pytest.fixture
def host(tmp_path: Path) -> FakeGitHub:
    fake = FakeGitHub(tmp_path / "host", logins={AUTHOR: BOT, REVIEWER: OWNER})
    fake.root.mkdir()
    fake.add("acme", "cardea")
    return fake


@pytest.fixture
def vcs(host: FakeGitHub, tmp_path: Path) -> Vcs:
    config = VcsConfig(
        credentials=tokens_config(),
        allow_approve=("eng.principal",),
        merge=MergeConfig(reviewer_login=OWNER),
    )
    tokens = PersonalAccessTokens(tokens_config(), directory=write_tokens(tmp_path / "creds"))
    return Vcs(config, tokens, transport=host.transport(), registry=host.registry())


def pool(name: str, replicas: int, operations: tuple[str, ...]) -> dict[str, Any]:
    return {
        "name": name,
        "replicas": replicas,
        "grants": [*BASE, *operations],
        "run": {
            "instructions": "Work.",
            "operations": list(operations),
            "model": "model-a",
            "bounds": {"timeout_seconds": 60, "attempts": 1},
        },
        "triggers": [{"schedule": {"cron": "0 9 29 2 *", "timezone": "UTC", "misfire": "skip"}}],
    }


def provisioning() -> Provisioning:
    expires = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    rights = sorted({*BASE, *DEV_OPS, *PRINCIPAL_OPS, "groups.manage"})
    permissions = [
        {
            "principal_id": "supervisor",
            "namespace": "eng",
            "operation": r,
            "delegate": True,
            "expires_at": expires,
        }
        for r in rights
    ]
    return Provisioning.model_validate(
        {
            "principals": [
                {
                    "principal_id": p,
                    "kind": "service" if p == "runtime" else "human",
                    "auth_kind": "spiffe",
                    "issuer": "test",
                    "subject": p,
                }
                for p in ("runtime", "supervisor")
            ],
            "permissions": permissions,
            "platform": [{**p, "principal_id": "platform", "delegate": False} for p in permissions],
        }
    )


@pytest.fixture
def kernel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, session: FakeSession, vcs: Vcs
) -> Iterator[Kernel]:
    monkeypatch.setattr(handlers, "DBOSClient", FakeClient)
    monkeypatch.setattr(agent_run_runtime, "POLL_SECONDS", 0.05)
    registry = Registry(in_process_descriptors(), vcs.handlers())
    db = Database(tmp_path / "data")
    db.migrate()
    provision(db, provisioning(), registry)
    entry = implicit_entry("model-a").model_copy(update={"models": {"model-a": ModelEntry()}})
    harnesses = Harnesses(
        {IMPLICIT_ID: session}, Catalog({IMPLICIT_ID: entry}, IMPLICIT_ID, implicit=IMPLICIT_ID)
    )
    instance = Kernel(db, cast(Authentication, Auth()), operations=registry, harnesses=harnesses)
    instance.content_gate = gate_config(tmp_path)
    group = {
        "apiVersion": "cardea/v1alpha1",
        "kind": "AgentGroup",
        "metadata": {"name": "eng", "namespace": "eng"},
        "spec": {
            "supervisor": "supervisor",
            "pools": [pool("dev", 2, DEV_OPS), pool("principal", 1, PRINCIPAL_OPS)],
        },
    }
    apply(
        instance,
        instance.auth.validate("supervisor"),
        "eng",
        AgentGroup.model_validate(group),
        False,
    )
    try:
        yield instance
    finally:
        db.close()


@pytest.fixture
def session() -> FakeSession:
    return FakeSession()


def act(
    kernel: Kernel, session: FakeSession, member: str, steps: list[tuple[str, dict[str, Any]]]
) -> tuple[str, list[Any]]:
    """One run of `member` making these tool calls."""
    from test_agent_runs import call

    results: list[Any] = []

    async def script(client: Any, request: SessionRequest) -> SessionResult:
        for tool, arguments in steps:
            results.append(await call(client, tool, **arguments))
        return SessionResult(text="Done.", is_error=False)

    session.scripts = [script]
    task_id = member_run(kernel, member)
    AgentRunRuntime(kernel).execute(task_id)
    return task_id, results


def open_pull(kernel: Kernel, session: FakeSession, tmp_path: Path, member: str) -> int:
    change = make_change(kernel, tmp_path, message=f"Change {next(STEPS)}")
    branch = f"agent/eng/{member.split('.')[-1]}-{next(STEPS)}"
    _, results = act(
        kernel,
        session,
        member,
        [
            ("vcs-push_branch", {"repository": "cardea", "branch": branch, **change}),
            (
                "vcs-open_change",
                {
                    "repository": "cardea",
                    "head": branch,
                    "base": "main",
                    "title": "Change",
                    "body": "What changed and why.",
                },
            ),
        ],
    )
    assert not any(r.is_error for r in results), [text(r) for r in results]
    return int(results[1].structured_content["number"])


def approve(kernel: Kernel, session: FakeSession, number: int, host: FakeGitHub) -> Any:
    """The PE's approval of the head; the kernel refuses one unless its checks passed."""
    host.repositories["acme/cardea"].pass_checks(head_of(host, number), "lint", "test")
    _, (review,) = act(
        kernel,
        session,
        "eng.principal.0",
        [
            (
                "vcs-review",
                {
                    "repository": "cardea",
                    "number": number,
                    "event": "APPROVE",
                    "body": "Approved.",
                    "client_key": f"approve-{next(STEPS)}",
                },
            )
        ],
    )
    return review


def merge(kernel: Kernel, session: FakeSession, member: str, number: int) -> Any:
    _, results = act(
        kernel, session, member, [("vcs-merge", {"repository": "cardea", "number": number})]
    )
    return results[0]


def head_of(host: FakeGitHub, number: int) -> str:
    pull = host.repositories["acme/cardea"].pull(number)
    assert pull is not None
    return str(pull["head"]["sha"])


def push_directly(host: FakeGitHub, number: int, tmp_path: Path) -> str:
    """A new commit on the pull request's branch, as a later push would add."""
    repo = host.repositories["acme/cardea"]
    pull = repo.pull(number)
    assert pull is not None
    branch = pull["head"]["ref"]
    work = tmp_path / f"later-{next(STEPS)}"
    git = ["git", "-c", "user.name=agent", "-c", "user.email=agent@example.invalid"]
    subprocess.run(["git", "clone", "-q", "-b", branch, str(repo.path), str(work)], check=True)
    (work / "later.txt").write_text("later\n")
    subprocess.run([*git, "-C", str(work), "add", "later.txt"], check=True)
    subprocess.run([*git, "-C", str(work), "commit", "-q", "-m", "Later"], check=True)
    subprocess.run(["git", "-C", str(work), "push", "-q", "origin", branch], check=True)
    return head_of(host, number)


def approved_and_green(
    kernel: Kernel, session: FakeSession, host: FakeGitHub, tmp_path: Path
) -> int:
    number = open_pull(kernel, session, tmp_path, "eng.dev.0")
    review = approve(kernel, session, number, host)
    assert not review.is_error, text(review)
    host.repositories["acme/cardea"].pass_checks(head_of(host, number), "lint", "test")
    return number


def refused(result: Any, code: str) -> None:
    assert result.is_error and code in text(result), text(result)


# The merge itself


def test_the_opener_merges_an_approved_green_pull_request(
    kernel: Kernel, session: FakeSession, host: FakeGitHub, tmp_path: Path
) -> None:
    number = approved_and_green(kernel, session, host, tmp_path)
    head = head_of(host, number)
    merged = merge(kernel, session, "eng.dev.0", number)
    assert not merged.is_error, text(merged)
    output = merged.structured_content
    assert output["merged"] is True and output["head_sha"] == head
    assert output["opened_receipt"] and output["approval_receipt"]
    repo = host.repositories["acme/cardea"]
    assert repo.head("main") == head and host.calls.count("merge") == 1
    pull = repo.pull(number)
    assert pull is not None and pull["merged_by"] == {"login": BOT}
    # The reviewer's approval came from its own identity; the author's from the bot.
    (review,) = repo.reviews[number]
    assert review["user"]["login"] == OWNER and review["commit_id"] == head
    assert pull["user"]["login"] == BOT
    # The squash commit is the pull request's title and number, and its description.
    assert pull["commit_title"] == f"Change (#{number})"
    assert pull["commit_message"] == "What changed and why."
    # The merged agent branch is deleted.
    assert output["head_deleted"] is True and repo.head(pull["head"]["ref"]) is None


def test_a_merged_branch_outside_the_agent_prefix_is_kept(
    kernel: Kernel, session: FakeSession, host: FakeGitHub, tmp_path: Path
) -> None:
    number = approved_and_green(kernel, session, host, tmp_path)
    repo = host.repositories["acme/cardea"]
    pull = repo.pull(number)
    assert pull is not None
    # As if a person had opened it from their own branch.
    subprocess.run(
        ["git", "-C", str(repo.path), "branch", "people/fix", pull["head"]["sha"]], check=True
    )
    pull["head"]["ref"] = "people/fix"
    merged = merge(kernel, session, "eng.dev.0", number)
    assert not merged.is_error, text(merged)
    assert merged.structured_content["head_deleted"] is False
    assert repo.head("people/fix") is not None and "delete_branch" not in host.calls


@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("unapproved", "merge_not_approved"),
        ("stale", "merge_approval_stale"),
        ("red", "merge_checks_failed"),
        ("pending", "merge_checks_pending"),
        ("no_checks", "merge_checks_missing"),
        ("other_developer", "merge_not_opener"),
        ("not_mergeable", "merge_not_mergeable"),
    ],
)
def test_merging_is_refused_with_nothing_sent(
    kernel: Kernel,
    session: FakeSession,
    host: FakeGitHub,
    tmp_path: Path,
    case: str,
    code: str,
) -> None:
    repo = host.repositories["acme/cardea"]
    number = open_pull(kernel, session, tmp_path, "eng.dev.0")
    member = "eng.dev.1" if case == "other_developer" else "eng.dev.0"
    if case != "unapproved":
        assert not approve(kernel, session, number, host).is_error
    head = head_of(host, number)
    repo.pass_checks(head, "lint", "test")
    if case == "stale":
        head = push_directly(host, number, tmp_path)
        repo.pass_checks(head, "lint", "test")
    elif case == "red":
        repo.checks[head][1]["conclusion"] = "failure"
    elif case == "pending":
        repo.checks[head][0].update(status="in_progress", conclusion=None)
    elif case == "no_checks":
        del repo.checks[head]
    elif case == "not_mergeable":
        pull = repo.pull(number)
        assert pull is not None
        pull["mergeable"] = False
    result = merge(kernel, session, member, number)
    refused(result, code)
    assert "merge" not in host.calls and repo.head("main") is None


def test_the_principal_cannot_merge(
    kernel: Kernel, session: FakeSession, host: FakeGitHub, tmp_path: Path
) -> None:
    number = approved_and_green(kernel, session, host, tmp_path)
    result = merge(kernel, session, "eng.principal.0", number)
    assert result.is_error and "merge" not in host.calls


def test_the_host_approval_must_be_the_configured_reviewers(
    kernel: Kernel, session: FakeSession, host: FakeGitHub, tmp_path: Path, vcs: Vcs
) -> None:
    number = approved_and_green(kernel, session, host, tmp_path)
    vcs.config = vcs.config.model_copy(update={"merge": MergeConfig(reviewer_login="someone")})
    refused(merge(kernel, session, "eng.dev.0", number), "merge_host_not_approved")
    vcs.config = vcs.config.model_copy(update={"merge": None})
    refused(merge(kernel, session, "eng.dev.0", number), "merge_not_configured")
    assert "merge" not in host.calls


def test_an_approval_from_a_pool_not_allowed_does_not_count(
    kernel: Kernel, session: FakeSession, host: FakeGitHub, tmp_path: Path, vcs: Vcs
) -> None:
    number = approved_and_green(kernel, session, host, tmp_path)
    # The host owner withdraws the principal pool's approval right after the fact.
    vcs.config = vcs.config.model_copy(update={"allow_approve": ("eng.other",)})
    refused(merge(kernel, session, "eng.dev.0", number), "merge_not_approved")


def test_an_interrupted_merge_is_reconciled_against_the_host(
    kernel: Kernel, session: FakeSession, host: FakeGitHub, tmp_path: Path
) -> None:
    number = approved_and_green(kernel, session, host, tmp_path)
    host.fail_after.add("merge")
    task_id, (result,) = act(
        kernel, session, "eng.dev.0", [("vcs-merge", {"repository": "cardea", "number": number})]
    )
    assert result.is_error
    with kernel.database.read() as conn, borrow(conn) as tx:
        (receipt,) = [r for r in tx.list_operations(task_id) if r.request.name == "vcs.merge"]
    assert receipt.state is OperationState.OUTCOME_UNKNOWN
    settled = OperationRuntime(kernel).reconcile(task_id, receipt.request.operation_id)
    assert settled == "succeeded" and host.calls.count("merge") == 1
    with kernel.database.read() as conn, borrow(conn) as tx:
        (receipt,) = [r for r in tx.list_operations(task_id) if r.request.name == "vcs.merge"]
    output = cast(Any, receipt.result)["output"]
    # The interrupted call never reached the deletion; the reconciler reports the branch.
    assert output["merged"] is True and output["head_deleted"] is False


# Per-caller credentials


def caller(agent_id: str | None, group: str | None) -> OperationContext:
    return OperationContext(
        task_id="t",
        namespace="eng",
        receipt_id="r",
        deadline=datetime.now(UTC) + timedelta(minutes=1),
        effect_key="e",
        agent_id=agent_id,
        group=group,
    )


def test_only_the_reviewer_pool_gets_the_reviewer_token(tmp_path: Path) -> None:
    import asyncio

    from cardea_vcs_ops.config import Repository

    tokens = PersonalAccessTokens(tokens_config(), directory=write_tokens(tmp_path / "creds"))
    repository = Repository(owner="acme", name="cardea", api_url="https://x.test", git_url="g")

    def token(context: OperationContext | None) -> str:
        return asyncio.run(tokens.credential(repository, ("contents:write",), context)).token

    assert token(caller("eng.principal.0", "eng")) == REVIEWER
    for other in (
        caller("eng.dev.0", "eng"),
        caller("eng.principal.0", None),  # not resolved as a member: the default
        caller("eng.principal.0", "other"),
        caller("eng.principals.0", "eng"),
        caller("eng.principal", "eng"),
        caller(None, "eng"),
        None,
    ):
        assert token(other) == AUTHOR, other
    # An expired token refuses its own callers only.
    lapsed = replace(tokens, today=lambda: date(2999, 1, 1))
    with pytest.raises(CredentialUnavailable, match="github-reviewer"):
        token_of = lapsed.credential(repository, (), caller("eng.principal.0", "eng"))
        asyncio.run(token_of)
    with pytest.raises(CredentialUnavailable, match="github-author"):
        asyncio.run(lapsed.credential(repository, (), caller("eng.dev.0", "eng")))


def test_each_caller_reaches_the_host_with_its_own_token(
    kernel: Kernel, session: FakeSession, host: FakeGitHub, tmp_path: Path
) -> None:
    open_pull(kernel, session, tmp_path, "eng.dev.0")
    developer = list(host.authorizations)
    assert developer and set(developer) == {f"Bearer {AUTHOR}"}
    approve(kernel, session, 1, host)
    assert set(host.authorizations[len(developer) :]) == {f"Bearer {REVIEWER}"}


def test_the_host_refuses_an_authors_approval_of_its_own_pull_request(
    kernel: Kernel, session: FakeSession, host: FakeGitHub, tmp_path: Path, vcs: Vcs
) -> None:
    """Why two identities: one bot account authors every pull request."""
    number = open_pull(kernel, session, tmp_path, "eng.dev.0")
    vcs.config = vcs.config.model_copy(
        update={"credentials": tokens_config().model_copy(update={"pools": {}})}
    )
    vcs.credentials = PersonalAccessTokens(
        tokens_config().model_copy(update={"pools": {}}), directory=tmp_path / "creds"
    )
    refused(approve(kernel, session, number, host), "host_refused")
    assert number not in host.repositories["acme/cardea"].reviews


# The kernel's evidence binding


def test_evidence_queries_keep_old_digests_and_reject_bad_paths() -> None:
    from cardea_vcs_ops import descriptors
    from pydantic import ValidationError

    from cardea.service.operations import EvidenceQuery, OperationDescriptor

    (merging,) = [d for d in descriptors() if d.name == "vcs.merge"]
    assert [q.name for q in merging.cardea.evidence or ()] == ["opened", "approvals"]
    plain = merging.model_dump(mode="json")
    del plain["cardea"]["evidence"]
    without = OperationDescriptor.model_validate(plain)
    explicit = OperationDescriptor.model_validate(
        {**plain, "cardea": {**plain["cardea"], "evidence": None}}
    )
    assert without.digest == explicit.digest != merging.digest
    for bad in ({"arguments/a.b": "/a"}, {"request/x": "/x"}, {"arguments/x": "x"}, {}):
        with pytest.raises(ValidationError):
            EvidenceQuery.model_validate({"name": "q", "operation": "vcs.review", "match": bad})


# A repository whose host rules the host owner waived (ADR-037 amendment 2026-11-21)

REASON = "The host plan has no rulesets for private repositories."


def waive(vcs: Vcs) -> None:
    assert vcs.registry is not None
    entries = dict(vcs.registry.repositories)
    entries["cardea"] = entries["cardea"].model_copy(
        update={"host_rules": "waived", "waiver_reason": REASON}
    )
    vcs.registry = vcs.registry.model_copy(update={"repositories": entries})


def check(kernel: Kernel, session: FakeSession) -> dict[str, Any]:
    _, (result,) = act(
        kernel, session, "eng.principal.0", [("vcs-check_repository", {"repository": "cardea"})]
    )
    assert not result.is_error, text(result)
    return cast(dict[str, Any], result.structured_content)


def test_without_host_rules_a_repository_is_not_ready_unless_waived(
    kernel: Kernel, session: FakeSession, host: FakeGitHub, vcs: Vcs
) -> None:
    host.repositories["acme/cardea"].rules_unavailable = True
    # The rules API's 403 "upgrade" answer is "unavailable", not an error.
    required = check(kernel, session)
    assert required["ready"] is False and required["missing"]
    assert required["host_rules"] == "required" and required["rules_available"] is False
    waive(vcs)
    waived = check(kernel, session)
    assert waived["ready"] is True and waived["missing"] == []
    assert waived["host_rules"] == "waived" and waived["waiver_reason"] == REASON
    assert waived["waived_checks"] == required["missing"]


def test_a_waived_repository_keeps_every_cardea_guard(
    kernel: Kernel, session: FakeSession, host: FakeGitHub, vcs: Vcs, tmp_path: Path
) -> None:
    waive(vcs)
    repo = host.repositories["acme/cardea"]
    repo.rules_unavailable = True
    # The agent prefix still holds for pushes.
    change = make_change(kernel, tmp_path, message="Outside the prefix")
    _, (pushed,) = act(
        kernel,
        session,
        "eng.dev.0",
        [("vcs-push_branch", {"repository": "cardea", "branch": "main", **change})],
    )
    refused(pushed, "branch_not_allowed")
    # A merge still needs the PE's approval at the head, then green checks.
    number = open_pull(kernel, session, tmp_path, "eng.dev.0")
    refused(merge(kernel, session, "eng.dev.0", number), "merge_not_approved")
    review = approve(kernel, session, number, host)
    assert not review.is_error, text(review)
    del repo.checks[head_of(host, number)]  # approved while green; the checks are gone again
    refused(merge(kernel, session, "eng.dev.0", number), "merge_checks_missing")
    refused(merge(kernel, session, "eng.dev.1", number), "merge_not_opener")
    assert host.calls.count("merge") == 0
    repo.pass_checks(head_of(host, number), "lint", "test")
    merged = merge(kernel, session, "eng.dev.0", number)
    assert not merged.is_error, text(merged)
    # The waiver is on the receipt of every effect it concerns.
    assert merged.structured_content["host_rules"] == "waived"
    assert merged.structured_content["waiver_reason"] == REASON


def test_a_waiver_needs_a_reason_and_a_hosted_repository() -> None:
    from cardea.service.repositories import Repository as Entry

    hosted = {
        "source": "/srv/mirror",
        "owner": "acme",
        "name": "widgets",
        "api_url": "https://api.github.test",
        "git_url": "/srv/widgets.git",
    }
    with pytest.raises(ValueError, match="waiver_reason"):
        Entry.model_validate({**hosted, "host_rules": "waived"})
    with pytest.raises(ValueError, match="waiver_reason"):
        Entry.model_validate({**hosted, "waiver_reason": REASON})
    with pytest.raises(ValueError, match="only to a repository"):
        Entry.model_validate(
            {"source": "/srv/mirror", "host_rules": "waived", "waiver_reason": REASON}
        )
    assert Entry.model_validate(hosted).host_rules == "required"


# A repository that requires up-to-date branches (ADR-037 amendment 2026-11-22)

GIT_AS = ["git", "-c", "user.name=agent", "-c", "user.email=agent@example.invalid"]


def require_up_to_date(vcs: Vcs, on: bool = True) -> None:
    assert vcs.registry is not None
    entries = dict(vcs.registry.repositories)
    entries["cardea"] = entries["cardea"].model_copy(update={"require_up_to_date": on})
    vcs.registry = vcs.registry.model_copy(update={"repositories": entries})


def bare(host: FakeGitHub, *args: str) -> str:
    repo = host.repositories["acme/cardea"]
    found = subprocess.run(
        [*GIT_AS, "-C", str(repo.path), *args], capture_output=True, text=True, check=True
    )
    return found.stdout.strip()


def advance_main(host: FakeGitHub) -> str:
    """Another change lands on main, as a merge of someone else's pull request would."""
    commit = bare(host, "commit-tree", "main^{tree}", "-p", "main", "-m", "Another change")
    bare(host, "update-ref", "refs/heads/main", commit)
    return commit


def bring_base_in(host: FakeGitHub, number: int, tmp_path: Path) -> str:
    """The developer merges the latest main into its branch and pushes it."""
    repo = host.repositories["acme/cardea"]
    pull = repo.pull(number)
    assert pull is not None
    branch = pull["head"]["ref"]
    work = tmp_path / f"update-{next(STEPS)}"
    subprocess.run(["git", "clone", "-q", "-b", branch, str(repo.path), str(work)], check=True)
    subprocess.run(
        [*GIT_AS, "-C", str(work), "merge", "-q", "--no-edit", "origin/main"], check=True
    )
    subprocess.run(["git", "-C", str(work), "push", "-q", "origin", branch], check=True)
    return head_of(host, number)


def behind_main(kernel: Kernel, session: FakeSession, host: FakeGitHub, tmp_path: Path) -> int:
    """An approved, green pull request whose branch was cut from main before main moved."""
    number = open_pull(kernel, session, tmp_path, "eng.dev.0")
    bare(host, "update-ref", "refs/heads/main", head_of(host, number))  # cut from main
    push_directly(host, number, tmp_path)  # the change itself
    advance_main(host)  # main moves on
    assert not approve(kernel, session, number, host).is_error
    host.repositories["acme/cardea"].pass_checks(head_of(host, number), "lint", "test")
    return number


def test_a_pull_request_behind_its_base_is_refused_until_brought_up_to_date(
    kernel: Kernel, session: FakeSession, host: FakeGitHub, vcs: Vcs, tmp_path: Path
) -> None:
    require_up_to_date(vcs)
    repo = host.repositories["acme/cardea"]
    number = behind_main(kernel, session, host, tmp_path)
    main = repo.head("main")
    result = merge(kernel, session, "eng.dev.0", number)
    refused(result, "merge_behind_base")
    message = text(result)
    # The refusal says what to do: bring the base in, push, green checks, re-approval.
    for step in (
        "does not contain the latest main",
        "Merge the latest main into your branch (no rebase, no force-push)",
        "push it",
        "every check to pass at the new head",
        "new approval of that head",
        "call vcs.merge again",
    ):
        assert step in message, message
    assert "merge" not in host.calls and repo.head("main") == main
    # Brought up to date: the old approval and checks are for an earlier head.
    head = bring_base_in(host, number, tmp_path)
    refused(merge(kernel, session, "eng.dev.0", number), "merge_approval_stale")
    assert not approve(kernel, session, number, host).is_error
    del repo.checks[head_of(host, number)]  # approved while green; the checks are gone again
    refused(merge(kernel, session, "eng.dev.0", number), "merge_checks_missing")
    repo.pass_checks(head, "lint", "test")
    merged = merge(kernel, session, "eng.dev.0", number)
    assert not merged.is_error, text(merged)
    output = merged.structured_content
    assert output["up_to_date"] is True and output["base_sha"] == main
    assert output["head_sha"] == head and repo.head("main") == head


def test_an_up_to_date_pull_request_merges_with_the_guard_on(
    kernel: Kernel, session: FakeSession, host: FakeGitHub, vcs: Vcs, tmp_path: Path
) -> None:
    require_up_to_date(vcs)
    repo = host.repositories["acme/cardea"]
    number = open_pull(kernel, session, tmp_path, "eng.dev.0")
    base = head_of(host, number)
    bare(host, "update-ref", "refs/heads/main", base)
    head = push_directly(host, number, tmp_path)  # ahead of main, not behind it
    assert not approve(kernel, session, number, host).is_error
    repo.pass_checks(head, "lint", "test")
    merged = merge(kernel, session, "eng.dev.0", number)
    assert not merged.is_error, text(merged)
    output = merged.structured_content
    assert output["up_to_date"] is True and output["base_sha"] == base
    assert host.calls.count("compare") == 1 and host.calls.count("merge") == 1


def test_with_the_guard_off_a_branch_behind_its_base_merges_as_before(
    kernel: Kernel, session: FakeSession, host: FakeGitHub, vcs: Vcs, tmp_path: Path
) -> None:
    number = behind_main(kernel, session, host, tmp_path)
    merged = merge(kernel, session, "eng.dev.0", number)
    assert not merged.is_error, text(merged)
    output = merged.structured_content
    assert "up_to_date" not in output and "base_sha" not in output
    assert "compare" not in host.calls and host.calls.count("merge") == 1


def test_the_guard_keeps_every_other_rule(
    kernel: Kernel, session: FakeSession, host: FakeGitHub, vcs: Vcs, tmp_path: Path
) -> None:
    require_up_to_date(vcs)
    waive(vcs)
    repo = host.repositories["acme/cardea"]
    number = open_pull(kernel, session, tmp_path, "eng.dev.0")
    bare(host, "update-ref", "refs/heads/main", head_of(host, number))
    refused(merge(kernel, session, "eng.dev.0", number), "merge_not_approved")
    assert not approve(kernel, session, number, host).is_error
    del repo.checks[head_of(host, number)]  # approved while green; the checks are gone again
    refused(merge(kernel, session, "eng.dev.0", number), "merge_checks_missing")
    repo.pass_checks(head_of(host, number), "lint", "test")
    refused(merge(kernel, session, "eng.dev.1", number), "merge_not_opener")
    merged = merge(kernel, session, "eng.dev.0", number)
    assert not merged.is_error, text(merged)
    assert merged.structured_content["up_to_date"] is True
    assert merged.structured_content["host_rules"] == "waived"


def test_check_repository_reports_the_guard(
    kernel: Kernel, session: FakeSession, host: FakeGitHub, vcs: Vcs
) -> None:
    host.repositories["acme/cardea"].rules_unavailable = True
    waive(vcs)
    report = check(kernel, session)
    # A waiver with no guard: visible, so the host owner can see nothing requires it.
    assert report["host_rules"] == "waived" and report["require_up_to_date"] is False
    require_up_to_date(vcs)
    assert check(kernel, session)["require_up_to_date"] is True


def test_the_guard_applies_only_to_a_hosted_repository() -> None:
    from cardea.service.repositories import Repository as Entry

    with pytest.raises(ValueError, match="require_up_to_date"):
        Entry.model_validate({"source": "/srv/mirror", "require_up_to_date": True})
    with pytest.raises(ValueError):
        Entry.model_validate(
            {
                "source": "/srv/mirror",
                "owner": "acme",
                "name": "widgets",
                "api_url": "https://api.github.test",
                "git_url": "/srv/widgets.git",
                "require_up_to_date": "yes",
            }
        )
