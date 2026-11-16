"""Repository-host operations (ADR-037 slices 4 and 5) against an in-process fake GitHub.

Group members' agent runs call the `vcs.*` tools over the real per-attempt MCP endpoint;
the fake host is `httpx.MockTransport` over real bare git repositories.
"""

import json
import subprocess
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path
from typing import Any, cast

import pytest
from cardea_vcs_ops import Vcs, descriptors
from cardea_vcs_ops.config import FixedTokenConfig, VcsConfig
from cardea_vcs_ops.fake import FakeGitHub
from cardea_vcs_ops.git import push_bundle
from mcp.client.session import ClientSession
from pydantic import SecretStr
from test_agent_runs import FakeSession, call

from cardea.kernel.models import ResourceRef
from cardea.runtime.agent_session import SessionRequest, SessionResult
from cardea.service import agent_run_runtime
from cardea.service import kernel as handlers
from cardea.service.agent_run_runtime import AgentRunRuntime
from cardea.service.auth import Authentication, Proof
from cardea.service.database import Database, Fault, borrow
from cardea.service.groups import apply
from cardea.service.harnesses import IMPLICIT_ID, Catalog, Harnesses, ModelEntry, implicit_entry
from cardea.service.kernel import Kernel, provision
from cardea.service.models import RIGHTS, AgentGroup, AgentRunRequest, Provisioning, now
from cardea.service.operations import Registry

TOKEN = "ghs_TEST" + "x7Q2" * 8  # recognisable, never expected outside the host
OPERATIONS = ("vcs.push_branch", "vcs.open_change", "vcs.comment", "vcs.review", "vcs.read_change")
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


@pytest.fixture
def host(tmp_path: Path) -> FakeGitHub:
    return FakeGitHub(tmp_path / "host")


@pytest.fixture
def session() -> FakeSession:
    return FakeSession()


@pytest.fixture
def vcs(host: FakeGitHub) -> Vcs:
    (host.root).mkdir()
    config = VcsConfig(
        repositories={"cardea": host.add("acme", "cardea"), "other": host.add("acme", "other")},
        credentials=FixedTokenConfig(kind="test", token=SecretStr(TOKEN)),
    )
    return Vcs(config, transport=host.transport())


@pytest.fixture
def kernel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, session: FakeSession, vcs: Vcs
) -> Iterator[Kernel]:
    monkeypatch.setattr(handlers, "DBOSClient", FakeClient)
    monkeypatch.setattr(agent_run_runtime, "POLL_SECONDS", 0.05)
    registry = Registry(descriptors(), vcs.handlers())
    db = Database(tmp_path / "data")
    db.migrate()
    provision(db, policy(), registry)
    entry = implicit_entry("model-a").model_copy(update={"models": {"model-a": ModelEntry()}})
    harnesses = Harnesses(
        {IMPLICIT_ID: session}, Catalog({IMPLICIT_ID: entry}, IMPLICIT_ID, implicit=IMPLICIT_ID)
    )
    instance = Kernel(db, cast(Authentication, Auth()), operations=registry, harnesses=harnesses)
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
    try:
        yield instance
    finally:
        db.close()


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
    kernel: Kernel, tmp_path: Path, message: str = "Fix the login retry"
) -> dict[str, Any]:
    """A commit on a fixture repository, bundled and stored as an object in `eng`."""
    work = tmp_path / f"work-{next(STEPS)}"
    git = ["git", "-c", "user.name=agent", "-c", "user.email=agent@example.invalid"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    (work / "README.md").write_text(f"{message}\n")
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
