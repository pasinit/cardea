"""Repository-host operations (ADR-037 slices 4 and 5) against an in-process fake GitHub.

Group members' agent runs call the `vcs.*` tools over the real per-attempt MCP endpoint;
the fake host is `httpx.MockTransport` over real bare git repositories.
"""

import hashlib
import json
import subprocess
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path
from typing import Any, cast

import pytest
from cardea_vcs_ops import Vcs, descriptors, git, in_process_descriptors
from cardea_vcs_ops.config import FixedTokenConfig, VcsConfig
from cardea_vcs_ops.credentials import Credential
from cardea_vcs_ops.fake import FakeGitHub
from cardea_vcs_ops.git import Workspace, push
from cardea_vcs_ops.ops import check_state
from mcp.client.session import ClientSession
from pydantic import SecretStr
from test_agent_runs import FakeSession, call

from cardea.kernel.models import ResourceRef
from cardea.runtime.agent_session import SessionRequest, SessionResult
from cardea.service import agent_run_runtime
from cardea.service import kernel as handlers
from cardea.service.agent_run_runtime import AgentRunRuntime
from cardea.service.auth import Authentication, Proof
from cardea.service.confined import Workers
from cardea.service.database import Database, Fault, borrow
from cardea.service.groups import apply
from cardea.service.harnesses import IMPLICIT_ID, Catalog, Harnesses, ModelEntry, implicit_entry
from cardea.service.kernel import Kernel, provision
from cardea.service.models import (
    RIGHTS,
    AgentGroup,
    AgentRunRequest,
    ContentGateConfig,
    Provisioning,
    now,
)
from cardea.service.operations import OperationDescriptor, Registry

TOKEN = "ghs_TEST" + "x7Q2" * 8  # recognisable, never expected outside the host
OPERATIONS = (
    "vcs.push_branch",
    "vcs.open_change",
    "vcs.comment",
    "vcs.review",
    "vcs.read_change",
    "vcs.check_repository",
    "vcs.merge",
    "vcs.read_branch",
    "vcs.list_changes",
)
RUN_GRANTS = (*RIGHTS, "models.invoke", *OPERATIONS, "repo:cardea")
STEPS = count()


class FakeClient:
    def __init__(self, **kwargs: Any) -> None:
        pass

    def enqueue_in_transaction(self, *args: Any) -> None:
        pass

    def send_in_transaction(self, *args: Any, **kwargs: Any) -> None:
        pass

    def destroy(self) -> None:
        pass


class Auth:
    def runtime_proof(self) -> Proof:
        return Proof("spiffe", "test", "runtime", time.time() + 60)

    def validate(self, token: str, kind: str = "spiffe") -> Proof:
        if token != "supervisor":
            raise Fault("invalid_credential", "Unknown test credential", 401)
        return Proof("spiffe", "test", token, time.time() + 60)


def policy() -> Provisioning:
    expires = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    rights = (*RUN_GRANTS, "groups.manage", "repo:other")
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
            ]
            + [{"principal_id": "loner", "kind": "agent", "runtime_id": "runtime"}],
            "permissions": permissions,
            "platform": [{**p, "principal_id": "platform", "delegate": False} for p in permissions],
        }
    )


def in_process() -> tuple[OperationDescriptor, ...]:
    """The package's descriptors run in the service process: the handler logic alone. The
    installed descriptors run in the `vcs` confined worker (test_vcs_confined.py)."""
    return in_process_descriptors()


@pytest.fixture
def host(tmp_path: Path) -> FakeGitHub:
    return FakeGitHub(tmp_path / "host")


@pytest.fixture
def session() -> FakeSession:
    return FakeSession()


@pytest.fixture
def vcs(host: FakeGitHub) -> Vcs:
    (host.root).mkdir()
    host.add("acme", "cardea")
    host.add("acme", "other")
    # Repositories come from the host's one registry (ADR-037); the vcs file keeps the rest.
    config = VcsConfig(credentials=FixedTokenConfig(kind="test", token=SecretStr(TOKEN)))
    return Vcs(config, transport=host.transport(), registry=host.registry())


@pytest.fixture
def kernel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, session: FakeSession, vcs: Vcs
) -> Iterator[Kernel]:
    instance = build_kernel(tmp_path, monkeypatch, session, Registry(in_process(), vcs.handlers()))
    try:
        yield instance
    finally:
        instance.database.close()


def build_kernel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session: FakeSession,
    registry: Registry,
    workers: Workers | None = None,
) -> Kernel:
    """A kernel with the `eng` group (one `dev` member) and the content gate."""
    monkeypatch.setattr(handlers, "DBOSClient", FakeClient)
    monkeypatch.setattr(agent_run_runtime, "POLL_SECONDS", 0.05)
    db = Database(tmp_path / "data")
    db.migrate()
    provision(db, policy(), registry)
    entry = implicit_entry("model-a").model_copy(update={"models": {"model-a": ModelEntry()}})
    harnesses = Harnesses(
        {IMPLICIT_ID: session}, Catalog({IMPLICIT_ID: entry}, IMPLICIT_ID, implicit=IMPLICIT_ID)
    )
    instance = Kernel(
        db,
        cast(Authentication, Auth()),
        operations=registry,
        harnesses=harnesses,
        workers=workers,
    )
    instance.content_gate = gate_config(tmp_path)
    group = {
        "apiVersion": "cardea/v1alpha1",
        "kind": "AgentGroup",
        "metadata": {"name": "eng", "namespace": "eng"},
        "spec": {
            "supervisor": "supervisor",
            "pools": [
                {
                    "name": "dev",
                    "replicas": 1,
                    "grants": list(RUN_GRANTS),
                    "run": {
                        "instructions": "Ship the change.",
                        "operations": list(OPERATIONS),
                        "model": "model-a",
                        "bounds": {"timeout_seconds": 60, "attempts": 1},
                    },
                    "triggers": [
                        {"schedule": {"cron": "0 9 29 2 *", "timezone": "UTC", "misfire": "skip"}}
                    ],
                }
            ],
        },
    }
    apply(
        instance,
        instance.auth.validate("supervisor"),
        "eng",
        AgentGroup.model_validate(group),
        False,
    )
    return instance


DENIED = "project-nightjar"


def gate_config(directory: Path) -> ContentGateConfig:
    """The kernel's content gate with a stand-in scanner and a one-term denylist."""
    scanner = directory / "no-findings"
    scanner.write_text("#!/bin/sh\ncat >/dev/null\necho '[]'\n")
    scanner.chmod(0o700)
    denylist = directory / "denylist"
    denylist.write_text(DENIED + "\n")
    denylist.chmod(0o600)
    return ContentGateConfig(
        gitleaks_binary=scanner,
        gitleaks_sha256=hashlib.sha256(scanner.read_bytes()).hexdigest(),
        denylist_file=denylist,
        notify_supervisor=False,
    )


def member_run(kernel: Kernel, member: str = "eng.dev.0", **changes: Any) -> str:
    with kernel.database.read() as conn:
        definition = conn.exec_driver_sql(
            "SELECT definition_json FROM triggers WHERE trigger_id=?", (f"{member}.t0",)
        ).scalar_one()
    template = json.loads(definition)["template"]
    request = AgentRunRequest.model_validate(
        {**template, "request_id": f"{member}.run-{next(STEPS)}", **changes}
    )
    return kernel.submit(kernel.auth.validate("supervisor"), request).task.task_id


def run_calls(
    kernel: Kernel, session: FakeSession, steps: list[tuple[str, dict[str, Any]]], **changes: Any
) -> tuple[str, str, list[Any]]:
    """One member run that makes these tool calls; returns its state and the results."""
    results: list[Any] = []

    async def script(client: ClientSession, request: SessionRequest) -> SessionResult:
        for tool, arguments in steps:
            result = await call(client, tool, **arguments)
            results.append(result)
        return SessionResult(text="Done.", is_error=False)

    session.scripts = [script]
    task_id = member_run(kernel, **changes)
    return task_id, AgentRunRuntime(kernel).execute(task_id), results


def text(result: Any) -> str:
    return " ".join(getattr(c, "text", "") for c in result.content)


def make_change(
    kernel: Kernel, tmp_path: Path, message: str = "Fix the login retry", content: str = ""
) -> dict[str, Any]:
    """A commit on a fixture repository, bundled and stored as an object in `eng`."""
    work = tmp_path / f"work-{next(STEPS)}"
    git = ["git", "-c", "user.name=agent", "-c", "user.email=agent@example.invalid"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    (work / "README.md").write_text(f"{message}\n{content}")
    subprocess.run([*git, "-C", str(work), "add", "README.md"], check=True)
    subprocess.run([*git, "-C", str(work), "commit", "-q", "-m", message], check=True)
    head = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    bundle = work.parent / f"{work.name}.bundle"
    subprocess.run(
        ["git", "-C", str(work), "bundle", "create", "-q", str(bundle), "main"], check=True
    )
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        stored = tx.put_object(
            ResourceRef(namespace="eng", resource_id=f"change-{work.name}"),
            bundle.read_bytes(),
            expected_version=0,
            producer_id="supervisor",
            at=now(),
        )
    return {"change": stored.model_dump(mode="json"), "head": head}


def calls(kernel: Kernel, task_id: str) -> list[Any]:
    with kernel.database.read() as conn, borrow(conn) as tx:
        return list(tx.list_operations(task_id))


# The whole flow: push, open, comment, review, read


def test_push_open_comment_review_and_read(
    kernel: Kernel, session: FakeSession, host: FakeGitHub, tmp_path: Path
) -> None:
    change = make_change(kernel, tmp_path)
    branch = "agent/eng/login-retry"
    steps = [
        ("vcs-push_branch", {"repository": "cardea", "branch": branch, **change}),
        (
            "vcs-open_change",
            {"repository": "cardea", "head": branch, "base": "main", "title": "Retry login once"},
        ),
        (
            "vcs-comment",
            {"repository": "cardea", "number": 1, "body": "Ready.", "client_key": "c1"},
        ),
        (
            "vcs-review",
            {
                "repository": "cardea",
                "number": 1,
                "event": "REQUEST_CHANGES",
                "body": "Add a test.",
                "client_key": "r1",
            },
        ),
        ("vcs-read_change", {"repository": "cardea", "number": 1}),
    ]
    task_id, state, results = run_calls(kernel, session, steps)
    assert state == "succeeded", [text(r) for r in results]
    assert [r.is_error for r in results] == [False] * 5, [text(r) for r in results]
    repo = host.repositories["acme/cardea"]
    assert repo.head(branch) == change["head"]
    (pull,) = repo.pulls
    assert results[1].structured_content["number"] == pull["number"] == 1
    (comment,) = repo.comments[1]
    assert comment["body"].startswith("Ready.") and "<!-- cardea:" in comment["body"]
    (review,) = repo.reviews[1]
    assert review["state"] == "CHANGES_REQUESTED"
    read = results[4].structured_content
    assert read["head"] == branch and read["head_sha"] == change["head"]
    assert read["reviews"][0]["state"] == "CHANGES_REQUESTED" and read["comments"] == 1
    # The kernel recorded the caller's group and staged the change without recording it.
    push = next(c for c in calls(kernel, task_id) if c.request.name == "vcs.push_branch")
    assert cast(Any, push.request.arguments)["context"]["group"] == "eng"
    assert "change.bundle" not in json.dumps(push.request.arguments)
    # A retried push of the same commit does nothing: the effect scope returns it.
    pushes = len(host.calls)
    _, state, again = run_calls(kernel, session, steps[:1])
    assert state == "succeeded" and not again[0].is_error
    assert len(host.calls) == pushes


# Refusals before any network call


def untouched(host: FakeGitHub, before: int) -> bool:
    return len(host.authorizations) == before


@pytest.mark.parametrize("branch", ["main", "agent/other/x", "agent/eng/", "agent/eng/../main"])
def test_branches_outside_the_group_prefix_are_refused(
    kernel: Kernel, session: FakeSession, host: FakeGitHub, tmp_path: Path, branch: str
) -> None:
    change = make_change(kernel, tmp_path)
    before = len(host.authorizations)
    _, _, (pushed,) = run_calls(
        kernel,
        session,
        [("vcs-push_branch", {"repository": "cardea", "branch": branch, **change})],
    )
    assert pushed.is_error and "branch_not_allowed" in text(pushed)
    assert untouched(host, before) and host.repositories["acme/cardea"].head(branch) is None


def test_a_caller_outside_any_group_cannot_push(
    kernel: Kernel, host: FakeGitHub, tmp_path: Path
) -> None:
    """An operation task by an agent outside a group has no branch prefix to push under."""
    from cardea.service.models import OperationTaskRequest
    from cardea.service.operation_runtime import OperationRuntime

    change = make_change(kernel, tmp_path)
    request = OperationTaskRequest.model_validate(
        {
            "request_id": "loner-push",
            "agent_id": "loner",
            "namespace": "eng",
            "objective": "Push",
            "operation": "vcs.push_branch",
            "arguments": {"repository": "cardea", "branch": "agent/eng/x", **change},
            "grants": ["objects.read", "objects.write", "vcs.push_branch", "repo:cardea"],
        }
    )
    task_id = kernel.submit(kernel.auth.validate("supervisor"), request).task.task_id
    before = len(host.authorizations)
    assert OperationRuntime(kernel).execute(task_id) == "failed"
    (receipt,) = calls(kernel, task_id)
    assert cast(Any, receipt.failure).code == "branch_not_allowed"
    assert untouched(host, before)


def test_approve_reviews_are_refused_unless_allowed(
    kernel: Kernel, session: FakeSession, host: FakeGitHub, vcs: Vcs, tmp_path: Path
) -> None:
    opened = open_pull(kernel, session, tmp_path)
    # Opened by another account: the host refuses an author's approval of its own change.
    host.repositories["acme/cardea"].pulls[0]["user"] = {"login": "author[bot]"}
    approve = {
        "repository": "cardea",
        "number": opened,
        "event": "APPROVE",
        "body": "LGTM",
        "client_key": "a1",
    }
    before = len(host.authorizations)
    _, _, (refused,) = run_calls(kernel, session, [("vcs-review", approve)])
    assert refused.is_error and "approve_refused" in text(refused)
    assert untouched(host, before)
    # Allowed for another pool only: the caller (`eng.dev.0`) is still refused.
    vcs.config = vcs.config.model_copy(update={"allow_approve": ("eng.principal",)})
    _, _, (other,) = run_calls(kernel, session, [("vcs-review", {**approve, "client_key": "a2"})])
    assert other.is_error and "approve_refused" in text(other)
    assert untouched(host, before)
    # Allowed, but never over a head whose checks did not all pass.
    vcs.config = vcs.config.model_copy(update={"allow_approve": True})
    _, _, (no_ci,) = run_calls(kernel, session, [("vcs-review", {**approve, "client_key": "a5"})])
    assert no_ci.is_error and "approve_checks_missing" in text(no_ci)
    assert "create_review" not in host.calls  # the head was read; no review was sent
    head = host.repositories["acme/cardea"].pull(opened)["head"]["sha"]  # type: ignore[index]
    host.repositories["acme/cardea"].pass_checks(head, "lint", "test")
    # Allowed for the caller's pool (ADR-037 question 2: the dev team's principal role).
    vcs.config = vcs.config.model_copy(update={"allow_approve": ("eng.dev",)})
    _, _, (pooled,) = run_calls(kernel, session, [("vcs-review", {**approve, "client_key": "a3"})])
    assert not pooled.is_error and pooled.structured_content["state"] == "APPROVED"
    vcs.config = vcs.config.model_copy(update={"allow_approve": True})
    _, _, (allowed,) = run_calls(kernel, session, [("vcs-review", {**approve, "client_key": "a4"})])
    assert not allowed.is_error and allowed.structured_content["state"] == "APPROVED"


def test_approve_pools_match_the_kernel_member_id(vcs: Vcs) -> None:
    from dataclasses import replace as with_fields

    from pydantic import ValidationError

    vcs.config = vcs.config.model_copy(update={"allow_approve": ("dev-team.principal",)})
    principal = with_fields(caller("dev-team"), agent_id="dev-team.principal.0")
    assert vcs.may_approve(principal)
    refused = [
        with_fields(caller("dev-team"), agent_id="dev-team.security.0"),  # another pool
        with_fields(caller("dev-team"), agent_id="dev-team.principal-x.0"),  # a longer name
        with_fields(caller("other"), agent_id="dev-team.principal.0"),  # not its member
        with_fields(caller("dev-team"), agent_id=None),
        with_fields(caller("dev-team"), agent_id="dev-team.principal.0", group=None),
    ]
    assert not any(vcs.may_approve(c) for c in refused)
    default = VcsConfig(credentials=FixedTokenConfig(kind="test", token=SecretStr(TOKEN)))
    assert default.allow_approve is False
    assert not Vcs(default, registry=vcs.registry).may_approve(principal)
    for bad in (["dev-team"], ["dev-team.principal.0"], ["Dev.x"]):
        with pytest.raises(ValidationError):
            VcsConfig.model_validate(
                {"allow_approve": bad, "credentials": {"kind": "profile_credential"}}
            )


def test_repository_authority_is_the_repo_grant(
    kernel: Kernel, session: FakeSession, host: FakeGitHub, tmp_path: Path
) -> None:
    """The pool holds `repo:cardea` only: `other` is refused by the kernel, before the
    handler runs; an unknown alias is refused by the operation."""
    change = make_change(kernel, tmp_path)
    before = len(host.authorizations)
    _, _, results = run_calls(
        kernel,
        session,
        [
            ("vcs-push_branch", {"repository": "other", "branch": "agent/eng/x", **change}),
            ("vcs-read_change", {"repository": "other", "number": 1}),
        ],
    )
    assert all(r.is_error and "task_grant_denied" in text(r) for r in results)
    assert untouched(host, before)


def test_a_change_from_another_namespace_is_not_staged(
    kernel: Kernel, session: FakeSession, host: FakeGitHub, tmp_path: Path
) -> None:
    change = make_change(kernel, tmp_path)
    change["change"]["namespace"] = "elsewhere"
    _, _, (pushed,) = run_calls(
        kernel,
        session,
        [("vcs-push_branch", {"repository": "cardea", "branch": "agent/eng/x", **change})],
    )
    assert pushed.is_error and "input_namespace" in text(pushed)


# Crash and reconcile: the host acted, the kernel did not hear back


@pytest.fixture
def short_leases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_run_runtime, "attempt_lease_seconds", lambda request: 4.0)
    monkeypatch.setattr(agent_run_runtime, "LEASE_MARGIN", 1.0)


def open_pull(kernel: Kernel, session: FakeSession, tmp_path: Path, name: str = "work") -> int:
    change = make_change(kernel, tmp_path, message=f"Change {name}")
    branch = f"agent/eng/{name}"
    _, state, results = run_calls(
        kernel,
        session,
        [
            ("vcs-push_branch", {"repository": "cardea", "branch": branch, **change}),
            (
                "vcs-open_change",
                {"repository": "cardea", "head": branch, "base": "main", "title": name},
            ),
        ],
    )
    assert state == "succeeded", [text(r) for r in results]
    return int(results[1].structured_content["number"])


def crashed_then_reconciled(kernel: Kernel, task_id: str, state: str) -> Any:
    from cardea.kernel.models import OperationState
    from cardea.service.operation_runtime import OperationRuntime

    assert state == "failed"
    (receipt,) = calls(kernel, task_id)
    assert receipt.state is OperationState.OUTCOME_UNKNOWN
    assert OperationRuntime(kernel).reconcile(task_id, receipt.request.operation_id) == "succeeded"
    (settled,) = calls(kernel, task_id)
    assert settled.state is OperationState.SUCCEEDED
    return cast(Any, settled.result)["output"]


def test_push_crash_is_reconciled_from_the_branch_head(
    kernel: Kernel,
    session: FakeSession,
    host: FakeGitHub,
    vcs: Vcs,
    tmp_path: Path,
    short_leases: None,
) -> None:
    change = make_change(kernel, tmp_path)
    branch = "agent/eng/crash"

    async def pushes_then_drops(*args: Any) -> None:
        await push(*args)
        raise ConnectionError("connection dropped after the push")

    vcs.pusher = pushes_then_drops
    task_id, state, _ = run_calls(
        kernel, session, [("vcs-push_branch", {"repository": "cardea", "branch": branch, **change})]
    )
    output = crashed_then_reconciled(kernel, task_id, state)
    assert output["head"] == change["head"] == host.repositories["acme/cardea"].head(branch)


def test_open_crash_is_reconciled_from_the_pull_for_the_head(
    kernel: Kernel, session: FakeSession, host: FakeGitHub, tmp_path: Path, short_leases: None
) -> None:
    change = make_change(kernel, tmp_path)
    branch = "agent/eng/pr-crash"
    run_calls(
        kernel, session, [("vcs-push_branch", {"repository": "cardea", "branch": branch, **change})]
    )
    host.fail_after.add("create_pull")
    task_id, state, _ = run_calls(
        kernel,
        session,
        [
            (
                "vcs-open_change",
                {"repository": "cardea", "head": branch, "base": "main", "title": "t"},
            )
        ],
    )
    output = crashed_then_reconciled(kernel, task_id, state)
    (pull,) = host.repositories["acme/cardea"].pulls
    assert output["number"] == pull["number"]


@pytest.mark.parametrize("tool", ["vcs-comment", "vcs-review"])
def test_comment_and_review_crashes_are_reconciled_from_the_marker(
    kernel: Kernel,
    session: FakeSession,
    host: FakeGitHub,
    tmp_path: Path,
    short_leases: None,
    tool: str,
) -> None:
    number = open_pull(kernel, session, tmp_path)
    arguments: dict[str, Any] = {
        "repository": "cardea",
        "number": number,
        "body": "Looks close.",
        "client_key": "k1",
    }
    if tool == "vcs-review":
        arguments["event"] = "COMMENT"
    host.fail_after.add("create_comment" if tool == "vcs-comment" else "create_review")
    task_id, state, _ = run_calls(kernel, session, [(tool, arguments)])
    output = crashed_then_reconciled(kernel, task_id, state)
    repo = host.repositories["acme/cardea"]
    store = repo.comments if tool == "vcs-comment" else repo.reviews
    (made,) = store[number]
    assert output["url"] == made["html_url"]


def test_a_duplicate_pull_request_returns_the_existing_one(
    kernel: Kernel, session: FakeSession, host: FakeGitHub, tmp_path: Path
) -> None:
    """Another task (another effect scope) opens the same head: the host answers 422 and the
    operation returns the pull request that already exists."""
    number = open_pull(kernel, session, tmp_path, name="dup")
    _, state, (again,) = run_calls(
        kernel,
        session,
        [
            (
                "vcs-open_change",
                {"repository": "cardea", "head": "agent/eng/dup", "base": "main", "title": "x"},
            )
        ],
    )
    assert state == "succeeded" and not again.is_error
    assert again.structured_content["number"] == number
    assert len(host.repositories["acme/cardea"].pulls) == 1


# The credential stays with the host call


def test_the_token_never_leaves_the_host_call(
    kernel: Kernel,
    session: FakeSession,
    host: FakeGitHub,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    short_leases: None,
) -> None:
    caplog.set_level("DEBUG")
    number = open_pull(kernel, session, tmp_path)
    host.fail_after.add("create_comment")
    run_calls(
        kernel,
        session,
        [
            (
                "vcs-comment",
                {"repository": "cardea", "number": number, "body": "b", "client_key": "z"},
            )
        ],
    )
    # It reached the host, as a bearer token, and nowhere else.
    assert f"Bearer {TOKEN}" in host.authorizations
    with kernel.database.read() as conn:
        tables = [
            r[0] for r in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")
        ]
        for table in tables:
            for row in conn.exec_driver_sql(f'SELECT * FROM "{table}"'):
                assert TOKEN not in repr(tuple(row)), table
    assert TOKEN not in caplog.text
    for request in session.requests:
        assert TOKEN not in repr(request)
    assert TOKEN not in repr(kernel.operations.handlers) and TOKEN not in str(host.root)


def test_remote_git_gets_the_profiles_proxy_and_nothing_else_inherited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Inside a confined worker the egress proxy is the only route to the host (ADR-028):
    # remote git built its environment from nothing and dropped it, so fetch went direct.
    proxy = "http://127.0.10.1:3128"
    for name in ("HTTP_PROXY", "http_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", proxy)
    monkeypatch.setenv("https_proxy", proxy)
    monkeypatch.setenv("NO_PROXY", "127.0.10.0,localhost")
    monkeypatch.setenv("CARDEA_UNRELATED", "kept out")
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.setattr(git, "CA_BUNDLES", ())
    credential = Credential(TOKEN, datetime.now(UTC) + timedelta(minutes=5))
    with Workspace(b"", "0" * 40, "https://git.example.test/a/b.git", credential) as workspace:
        env = workspace._environment()
    assert env["HTTPS_PROXY"] == env["https_proxy"] == proxy
    assert env["NO_PROXY"] == "127.0.10.0,localhost"
    assert set(env) == {
        "PATH",
        "HOME",
        "GIT_ASKPASS",
        "GIT_TERMINAL_PROMPT",
        "GIT_CONFIG_NOSYSTEM",
        "CARDEA_GIT_TOKEN",
        "HTTPS_PROXY",
        "https_proxy",
        "NO_PROXY",
    }


def test_remote_git_names_the_ca_bundle_it_finds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "ca-certificates.crt"
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.setattr(git, "CA_BUNDLES", (str(tmp_path / "absent.crt"), str(bundle)))
    assert git.ca_bundle() == {}  # nothing bound: git keeps its default
    bundle.write_text("certificates")
    assert git.ca_bundle() == {"GIT_SSL_CAINFO": str(bundle)}
    named = tmp_path / "named.pem"
    named.write_text("certificates")
    monkeypatch.setenv("SSL_CERT_FILE", str(named))
    assert git.ca_bundle() == {"GIT_SSL_CAINFO": str(named)}


def test_descriptors_without_resource_or_inputs_keep_their_digest() -> None:
    from cardea.service.operations import OperationDescriptor

    (push,) = [d for d in descriptors() if d.name == "vcs.push_branch"]
    document = push.model_dump(mode="json")
    plain = {
        **document,
        "cardea": {k: v for k, v in document["cardea"].items() if k not in {"resource", "inputs"}},
    }
    without = OperationDescriptor.model_validate(plain)
    explicit = OperationDescriptor.model_validate(
        {**plain, "cardea": {**plain["cardea"], "resource": None, "inputs": None}}
    )
    assert without.digest == explicit.digest != push.digest


# The kernel's content gate runs before anything is sent


def test_the_content_gate_refuses_before_sending(
    kernel: Kernel, session: FakeSession, host: FakeGitHub, tmp_path: Path
) -> None:
    number = open_pull(kernel, session, tmp_path)
    in_file = make_change(kernel, tmp_path, message="Docs", content=f"Mentions {DENIED}.\n")
    in_message = make_change(kernel, tmp_path, message=f"Ship {DENIED}")
    before = len(host.authorizations)
    _, _, results = run_calls(
        kernel,
        session,
        [
            ("vcs-push_branch", {"repository": "cardea", "branch": "agent/eng/a", **in_file}),
            ("vcs-push_branch", {"repository": "cardea", "branch": "agent/eng/b", **in_message}),
            (
                "vcs-comment",
                {
                    "repository": "cardea",
                    "number": number,
                    "body": f"see {DENIED}",
                    "client_key": "g",
                },
            ),
        ],
    )
    assert all(r.is_error and "content_refused" in text(r) for r in results), [
        text(r) for r in results
    ]
    # The refusal names the rule and place, never the matched text.
    assert all(DENIED not in text(r) for r in results)
    assert "README.md:2" in text(results[0]) and "commit_message" in text(results[1])
    assert untouched(host, before)


def test_without_a_configured_gate_nothing_is_sent(
    kernel: Kernel, session: FakeSession, host: FakeGitHub, tmp_path: Path
) -> None:
    number = open_pull(kernel, session, tmp_path)
    kernel.content_gate = None
    before = len(host.authorizations)
    _, _, (refused,) = run_calls(
        kernel,
        session,
        [
            (
                "vcs-comment",
                {"repository": "cardea", "number": number, "body": "ok", "client_key": "n"},
            )
        ],
    )
    assert refused.is_error and "content_gate_unavailable" in text(refused)
    assert untouched(host, before)


def fixture_git(work: Path, *args: str) -> str:
    identity = ["-c", "user.name=agent", "-c", "user.email=agent@example.invalid"]
    done = subprocess.run(
        ["git", *identity, "-C", str(work), *args], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def test_a_thin_bundle_artifact_from_code_session_pushes(
    kernel: Kernel, session: FakeSession, host: FakeGitHub, tmp_path: Path
) -> None:
    """`code.session` returns `base..HEAD` as an artifact of object parts. The push fetches the
    host's base, and the gate scans only the new commit: the base already on the host may
    hold text the gate would refuse, and it is not sent again."""
    repo = host.repositories["acme/cardea"]
    work = tmp_path / "thin"
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    (work / "base.txt").write_text(f"old notes about {DENIED}\n")
    fixture_git(work, "add", ".")
    fixture_git(work, "commit", "-q", "-m", "Base")
    fixture_git(work, "push", "-q", str(repo.path), "main")
    base = fixture_git(work, "rev-parse", "HEAD")
    (work / "fix.txt").write_text("the fix\n")
    fixture_git(work, "add", ".")
    fixture_git(work, "commit", "-q", "-m", "Fix")
    head = fixture_git(work, "rev-parse", "HEAD")
    target = tmp_path / "thin.bundle"
    fixture_git(work, "bundle", "create", "-q", str(target), f"{base}..HEAD")
    content = target.read_bytes()
    half = len(content) // 2
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        parts = [
            tx.put_object(
                ResourceRef(namespace="eng", resource_id=f"thin-part-{i}"),
                chunk,
                expected_version=0,
                producer_id="supervisor",
                at=now(),
            ).model_dump(mode="json")
            for i, chunk in enumerate((content[:half], content[half:]))
        ]
    artifact = {
        "name": "change.bundle",
        "bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "parts": parts,
    }
    branch = "agent/eng/thin"
    push_thin = {"repository": "cardea", "branch": branch, "change": artifact, "head": head}
    _, state, (pushed,) = run_calls(kernel, session, [("vcs-push_branch", push_thin)])
    assert state == "succeeded" and not pushed.is_error, text(pushed)
    assert repo.head(branch) == head
    # A listing whose parts do not match its digest is refused before anything runs.
    tampered = {**push_thin, "branch": "agent/eng/t2", "change": {**artifact, "sha256": "0" * 64}}
    _, _, (refused,) = run_calls(kernel, session, [("vcs-push_branch", tampered)])
    assert refused.is_error and "input_digest_mismatch" in text(refused)


# One repository registry (ADR-037 finding 3)


def caller(group: str) -> Any:
    from cardea.service.operations import OperationContext

    return OperationContext(
        task_id="t",
        namespace="eng",
        receipt_id="r",
        deadline=now() + timedelta(minutes=1),
        effect_key="e",
        group=group,
    )


def test_hosts_and_branch_prefixes_come_from_the_registry(host: FakeGitHub, vcs: Vcs) -> None:
    from cardea.service.operations import OperationRejected

    registry = host.registry(cardea="bots/{group}/")
    configured = Vcs(vcs.config, transport=host.transport(), registry=registry)
    assert configured.repository("cardea").git_url == str(host.repositories["acme/cardea"].path)
    configured.check_branch("bots/eng/fix", caller("eng"), "cardea")
    with pytest.raises(OperationRejected) as refused:
        configured.check_branch("agent/eng/fix", caller("eng"), "cardea")
    assert refused.value.code == "branch_not_allowed"
    configured.check_branch("agent/eng/fix", caller("eng"), "other")  # the default prefix


def test_a_registry_alias_without_a_host_is_not_a_vcs_repository(tmp_path: Path) -> None:
    from cardea.service.operations import OperationRejected
    from cardea.service.repositories import Registry, Repository

    registry = Registry(repositories={"mirror": Repository(source=str(tmp_path / "m.git"))})
    config = VcsConfig(credentials=FixedTokenConfig(kind="test", token=SecretStr(TOKEN)))
    with pytest.raises(OperationRejected) as refused:
        Vcs(config, registry=registry).repository("mirror")
    assert refused.value.code == "repository_unknown"


def test_the_vcs_file_repositories_are_deprecated_and_must_agree(
    tmp_path: Path, host: FakeGitHub, vcs: Vcs
) -> None:
    legacy = VcsConfig(
        repositories={"cardea": vcs.repository("cardea"), "other": vcs.repository("other")},
        credentials=FixedTokenConfig(kind="test", token=SecretStr(TOKEN)),
    )
    with pytest.warns(DeprecationWarning, match="CARDEA_VCS_CONFIG"):
        alone = Vcs(legacy, environ={})
    assert alone.registry is None and alone.repository("cardea").owner == "acme"
    registry = tmp_path / "repositories.json"
    registry.write_text(host.registry().model_dump_json())
    registry.chmod(0o600)
    with pytest.warns(DeprecationWarning):
        agreeing = Vcs(legacy, environ={"CARDEA_REPOSITORIES": str(registry)})
    assert agreeing.registry is not None
    moved = host.registry(cardea="bots/{group}/").model_dump_json()
    registry.write_text(moved)
    with pytest.warns(DeprecationWarning), pytest.raises(Fault) as refused:
        Vcs(legacy, environ={"CARDEA_REPOSITORIES": str(registry)})
    assert refused.value.code == "repositories_disagree"


def test_the_entry_points_refuse_a_disagreeing_host_definitely(
    tmp_path: Path, host: FakeGitHub, vcs: Vcs, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal, never an effect in doubt: nothing was sent."""
    import cardea_vcs_ops

    from cardea.service.operations import OperationRejected

    legacy = VcsConfig(
        repositories={"cardea": vcs.repository("cardea")},
        credentials=FixedTokenConfig(kind="test", token=SecretStr(TOKEN)),
    )
    registry = tmp_path / "repositories.json"
    registry.write_text(host.registry().model_dump_json())
    registry.chmod(0o600)
    monkeypatch.setenv("CARDEA_REPOSITORIES", str(registry))
    monkeypatch.setattr(cardea_vcs_ops, "load", lambda: legacy)
    monkeypatch.setattr(cardea_vcs_ops, "_DEFAULT", None)
    with pytest.warns(DeprecationWarning), pytest.raises(OperationRejected) as refused:
        cardea_vcs_ops._vcs()
    assert refused.value.code == "repositories_disagree"


def test_read_branch_returns_the_head_and_refuses_unknown_branches(
    kernel: Kernel, session: FakeSession, tmp_path: Path
) -> None:
    """The PM reads main's head before `env.try` (ADR-038), which needs a full commit."""
    change = make_change(kernel, tmp_path)
    branch = "agent/eng/head"
    _, state, results = run_calls(
        kernel,
        session,
        [
            ("vcs-push_branch", {"repository": "cardea", "branch": branch, **change}),
            ("vcs-read_branch", {"repository": "cardea", "branch": branch}),
            ("vcs-read_branch", {"repository": "cardea", "branch": "no-such-branch"}),
        ],
    )
    assert results[1].structured_content["head"] == change["head"], text(results[1])
    assert results[2].is_error and "branch_unknown" in text(results[2])


def test_list_changes_shows_open_changes_with_review_and_check_state(
    kernel: Kernel, session: FakeSession, host: FakeGitHub, tmp_path: Path
) -> None:
    """The PM's overview: open changes only, each with its head, review and check state."""
    first, second = make_change(kernel, tmp_path), make_change(kernel, tmp_path, "Second")
    steps: list[tuple[str, dict[str, Any]]] = []
    for number, (branch, change) in enumerate(
        [("agent/eng/one", first), ("agent/eng/two", second)], 1
    ):
        steps += [
            ("vcs-push_branch", {"repository": "cardea", "branch": branch, **change}),
            (
                "vcs-open_change",
                {
                    "repository": "cardea",
                    "head": branch,
                    "base": "main",
                    "title": f"Change {number}",
                },
            ),
        ]
    steps.append(
        (
            "vcs-review",
            {
                "repository": "cardea",
                "number": 1,
                "event": "REQUEST_CHANGES",
                "body": "No.",
                "client_key": "r1",
            },
        )
    )
    run_calls(kernel, session, steps)
    repo = host.repositories["acme/cardea"]
    repo.pass_checks(first["head"], "lint", "test")
    repo.checks[second["head"]] = [{"name": "test", "status": "completed", "conclusion": "failure"}]
    before = len(host.authorizations)
    _, state, (listed,) = run_calls(
        kernel, session, [("vcs-list_changes", {"repository": "cardea"})]
    )
    assert state == "succeeded" and not listed.is_error, text(listed)
    changes = {c["number"]: c for c in listed.structured_content["changes"]}
    assert [c["number"] for c in listed.structured_content["changes"]] == [2, 1]
    assert changes[1]["head"] == "agent/eng/one" and changes[1]["head_sha"] == first["head"]
    assert (changes[1]["review_state"], changes[1]["check_state"]) == (
        "changes_requested",
        "passing",
    )
    assert (changes[2]["review_state"], changes[2]["check_state"]) == ("none", "failing")
    # Closed changes are not listed, and the call only read.
    repo.pulls[0]["state"] = "closed"
    _, _, (open_only,) = run_calls(
        kernel, session, [("vcs-list_changes", {"repository": "cardea"})]
    )
    assert [c["number"] for c in open_only.structured_content["changes"]] == [2]
    assert len(host.authorizations) > before


def test_list_changes_without_the_grant_is_refused_naming_it(
    kernel: Kernel, session: FakeSession, host: FakeGitHub
) -> None:
    """A role lacking `repo:<alias>` is refused before the host is called, and the refusal
    names the grant. (A run cannot expose the operation without holding its own grant.)"""
    before = len(host.authorizations)
    _, _, (no_repo,) = run_calls(kernel, session, [("vcs-list_changes", {"repository": "other"})])
    assert no_repo.is_error and "repo:other" in text(no_repo), text(no_repo)
    assert untouched(host, before)


def test_check_state_summarises_completed_runs() -> None:
    """Skipped and neutral runs pass; any other conclusion fails; unfinished runs pend."""

    def run(conclusion: str | None, status: str = "completed") -> dict[str, Any]:
        return {"name": "test", "status": status, "conclusion": conclusion}

    assert check_state([]) == "none"
    assert check_state([run("success"), run("skipped"), run("neutral")]) == "passing"
    assert check_state([run("success"), run("failure")]) == "failing"
    assert check_state([run("cancelled")]) == "failing"
    assert check_state([run("failure"), run(None, "in_progress")]) == "pending"
