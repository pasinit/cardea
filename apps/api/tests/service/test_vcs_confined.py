"""The `vcs.*` operations in their confined worker (ADR-037), against the fake GitHub.

The installed descriptors run in the `vcs` profile. Here the helper is a local stand-in
that runs the real worker command (`operation_worker <handler> [<reconciler>]`) as a
separate process with only the worker's environment: the host-owner configuration file,
which holds no token, and the profile credential file. The fake GitHub serves the worker
over loopback HTTP. The kernel process never holds the repository token.
"""

import base64
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from cardea_vcs_ops import descriptors
from cardea_vcs_ops.config import load
from cardea_vcs_ops.fake import FakeGitHub, serve
from test_agent_runs import FakeSession
from test_vcs_ops import (
    DENIED,
    TOKEN,
    build_kernel,
    calls,
    make_change,
    run_calls,
    text,
    untouched,
)

import cardea
from cardea.hosts import launcher, operation_worker
from cardea.kernel.models import OperationState
from cardea.service.confined import Workers
from cardea.service.database import Fault
from cardea.service.kernel import Kernel
from cardea.service.operation_runtime import OperationRuntime
from cardea.service.operations import OperationRejected, Registry

# The worker commands a host owner installs: the handler, then its reconciler.
WORKERS = {
    "vcs-push-branch": ["vcs.push_branch", "vcs.find_branch"],
    "vcs-open-change": ["vcs.open_change", "vcs.find_change"],
    "vcs-comment": ["vcs.comment", "vcs.find_comment"],
    "vcs-review": ["vcs.review", "vcs.find_review"],
    "vcs-read-change": ["vcs.read_change"],
    "vcs-check-repository": ["vcs.check_repository"],
}
INPUT_KB = 16384


class LocalVcsHost:
    """The helper's interface: stages inputs as the helper does, runs the worker locally."""

    def __init__(self, root: Path, env: dict[str, str]) -> None:
        self.root, self.env = root, env
        self.outputs: dict[str, bytes] = {}
        # Everything the kernel sent the helper: job documents and staged inputs.
        self.requests: list[tuple[str, bytes, list[dict[str, str]] | None]] = []

    def profiles(self) -> dict[str, Any]:
        network = {
            "policy": "allow_list",
            "proxy": "127.0.10.6:3128",
            "hosts": ["api.github.com", "github.com"],
        }
        return {
            "profiles": {
                "vcs": {
                    "wall_seconds": 600,
                    "network": network,
                    "input_kb": INPUT_KB,
                    "sha256": "p-vcs",
                }
            },
            "workers": {name: ["vcs"] for name in WORKERS},
            "sha256": "config",
        }

    def start_confined(
        self,
        job: str,
        content: bytes,
        deadline: float,
        profile: str,
        worker: str,
        inputs: list[dict[str, str]] | None = None,
    ) -> None:
        self.requests.append((worker, content, inputs))
        staged = launcher.admit_inputs(inputs, {"input_kb": INPUT_KB})
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.root),
            # The kernel source is on the test's path, not installed in the environment.
            "PYTHONPATH": str(Path(cardea.__file__).parents[1]),
            **self.env,
        }
        if staged:
            folder = self.root / job / "inputs"
            folder.mkdir(parents=True)
            for index, data in enumerate(staged):
                (folder / str(index)).write_bytes(data)
                (folder / str(index)).chmod(0o444)
            env["CARDEA_INPUTS"] = str(folder)
        done = subprocess.run(
            [sys.executable, "-m", "cardea.hosts.operation_worker", *WORKERS[worker]],
            input=content,
            capture_output=True,
            env=env,
            timeout=120,
            check=False,
        )
        self.outputs[job] = done.stdout

    def status(self, job: str) -> dict[str, Any]:
        return {
            "state": "finished",
            "result": "success",
            "exit_status": "0",
            "output": base64.b64encode(self.outputs[job]).decode(),
        }

    def stop(self, job: str) -> dict[str, Any]:
        return {}

    def cleanup(self, job: str) -> dict[str, Any]:
        return {}


@pytest.fixture
def github(tmp_path: Path) -> Iterator[tuple[FakeGitHub, str]]:
    fake = FakeGitHub(tmp_path / "host")
    fake.root.mkdir()
    with serve(fake) as url:
        yield fake, url


@pytest.fixture
def helper(tmp_path: Path, github: tuple[FakeGitHub, str]) -> LocalVcsHost:
    fake, url = github
    repositories = {}
    for alias, name in (("cardea", "cardea"), ("other", "other")):
        repository = fake.add("acme", name).model_copy(update={"api_url": url})
        repositories[alias] = repository.model_dump()
    # The host owner's file holds no secret: it may be shared and read-only.
    config = tmp_path / "vcs.json"
    config.write_text(
        json.dumps({"repositories": repositories, "credentials": {"kind": "profile_credential"}})
    )
    config.chmod(0o644)
    # The profile credential, which systemd would load into the unit.
    credential = tmp_path / "credential"
    credential.write_text(TOKEN + "\n")
    credential.chmod(0o600)
    return LocalVcsHost(
        tmp_path / "spool",
        {"CARDEA_VCS_CONFIG": str(config), "CARDEA_CREDENTIAL": str(credential)},
    )


@pytest.fixture
def session() -> FakeSession:
    return FakeSession()


@pytest.fixture
def kernel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, session: FakeSession, helper: LocalVcsHost
) -> Iterator[Kernel]:
    # Installed as packaged: confined, with no handler or reconciler in the service process.
    instance = build_kernel(
        tmp_path, monkeypatch, session, Registry(descriptors(), {}), Workers(helper)
    )
    try:
        yield instance
    finally:
        instance.database.close()


@pytest.fixture
def host(github: tuple[FakeGitHub, str]) -> FakeGitHub:
    return github[0]


def test_the_installed_descriptors_run_confined() -> None:
    for descriptor in descriptors():
        executor = descriptor.cardea.executor
        assert executor is not None and executor.profile == "vcs", descriptor.name
        assert descriptor.cardea.handler is None
        assert WORKERS[executor.worker][0] == descriptor.name
        if descriptor.cardea.reconciler:
            assert WORKERS[executor.worker][1:] == [descriptor.cardea.reconciler]


def test_push_open_comment_review_read_and_check_in_the_worker(
    kernel: Kernel,
    session: FakeSession,
    host: FakeGitHub,
    helper: LocalVcsHost,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG")
    change = make_change(kernel, tmp_path)
    branch = "agent/eng/login-retry"
    steps = [
        ("vcs-push_branch", {"repository": "cardea", "branch": branch, **change}),
        (
            "vcs-open_change",
            {"repository": "cardea", "head": branch, "base": "main", "title": "Retry login"},
        ),
        ("vcs-comment", {"repository": "cardea", "number": 1, "body": "Ready.", "client_key": "c"}),
        (
            "vcs-review",
            {
                "repository": "cardea",
                "number": 1,
                "event": "COMMENT",
                "body": "Fine.",
                "client_key": "r",
            },
        ),
        ("vcs-read_change", {"repository": "cardea", "number": 1}),
        ("vcs-check_repository", {"repository": "cardea"}),
    ]
    task_id, state, results = run_calls(kernel, session, steps)
    assert state == "succeeded", [text(r) for r in results]
    assert [r.is_error for r in results] == [False] * 6, [text(r) for r in results]
    repo = host.repositories["acme/cardea"]
    assert repo.head(branch) == change["head"]
    assert len(repo.pulls) == 1 and len(repo.comments[1]) == 1 and len(repo.reviews[1]) == 1
    assert results[4].structured_content["head_sha"] == change["head"]
    assert results[5].structured_content["ready"] is False  # the fake has no rules yet

    # Each operation ran in its own worker; the push got the change as a staged input.
    workers = [worker for worker, _, _ in helper.requests]
    assert workers == [
        "vcs-push-branch",
        "vcs-open-change",
        "vcs-comment",
        "vcs-review",
        "vcs-read-change",
        "vcs-check-repository",
    ]
    _, document, staged = helper.requests[0]
    sent = json.loads(document)
    bundle = make_bundle_bytes(kernel, change)
    (only,) = staged or []
    assert only["sha256"] == hashlib.sha256(bundle).hexdigest()
    assert sent["context"]["inputs"] == {
        "/change": {"file": "0", "sha256": only["sha256"], "bytes": len(bundle)}
    }
    assert sent["context"]["group"] == "eng" and "content_gate" in sent["context"]
    assert all(s is None for _, _, s in helper.requests[1:])

    # The token reached the host from the worker, and nothing on the kernel's side.
    assert f"Bearer {TOKEN}" in host.authorizations
    assert_token_absent(kernel, helper, session, caplog)
    # The staged bundle is not recorded with the receipt.
    push = next(c for c in calls(kernel, task_id) if c.request.name == "vcs.push_branch")
    recorded = json.dumps(cast(Any, push.request.arguments))
    assert base64.b64encode(bundle).decode()[:64] not in recorded


def make_bundle_bytes(kernel: Kernel, change: dict[str, Any]) -> bytes:
    from cardea.kernel.models import ObjectVersion
    from cardea.service.database import borrow

    with kernel.database.read() as conn, borrow(conn) as tx:
        return tx.read_object(ObjectVersion.model_validate(change["change"]))


def assert_token_absent(
    kernel: Kernel, helper: LocalVcsHost, session: FakeSession, caplog: pytest.LogCaptureFixture
) -> None:
    with kernel.database.read() as conn:
        tables = [
            r[0] for r in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")
        ]
        for table in tables:
            for row in conn.exec_driver_sql(f'SELECT * FROM "{table}"'):
                assert TOKEN not in repr(tuple(row)), table
    assert TOKEN not in caplog.text
    assert all(TOKEN not in repr(request) for request in session.requests)
    for _, document, staged in helper.requests:
        assert TOKEN.encode() not in document and TOKEN not in json.dumps(staged)
    assert all(TOKEN.encode() not in output for output in helper.outputs.values())
    assert TOKEN not in json.dumps(dict(os.environ))
    assert TOKEN not in repr(kernel.operations.handlers)


def test_refusals_keep_their_codes_and_send_nothing(
    kernel: Kernel, session: FakeSession, host: FakeGitHub, tmp_path: Path
) -> None:
    change = make_change(kernel, tmp_path)
    before = len(host.authorizations)
    _, _, results = run_calls(
        kernel,
        session,
        [
            ("vcs-push_branch", {"repository": "cardea", "branch": "main", **change}),
            (
                "vcs-push_branch",
                {
                    "repository": "cardea",
                    "branch": "agent/eng/x",
                    **make_change(kernel, tmp_path, message=f"Ship {DENIED}"),
                },
            ),
        ],
    )
    assert "branch_not_allowed" in text(results[0])
    # The kernel's content gate, through the worker's loopback callback (#53).
    assert "content_refused" in text(results[1]) and DENIED not in text(results[1])
    assert all(r.is_error for r in results)
    # The refused push fetched the host's heads (reads) and sent nothing.
    repo = host.repositories["acme/cardea"]
    assert repo.head("main") is None and repo.head("agent/eng/x") is None
    assert len(host.authorizations) == before  # git reads the bare repository, not REST


def test_a_crash_after_the_host_acted_is_reconciled_in_the_worker(
    kernel: Kernel,
    session: FakeSession,
    host: FakeGitHub,
    helper: LocalVcsHost,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cardea.service import agent_run_runtime

    monkeypatch.setattr(agent_run_runtime, "attempt_lease_seconds", lambda request: 4.0)
    monkeypatch.setattr(agent_run_runtime, "LEASE_MARGIN", 1.0)
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
    assert state == "failed"
    (receipt,) = calls(kernel, task_id)
    assert receipt.state is OperationState.OUTCOME_UNKNOWN
    assert OperationRuntime(kernel).reconcile(task_id, receipt.request.operation_id) == "succeeded"
    (settled,) = calls(kernel, task_id)
    (pull,) = host.repositories["acme/cardea"].pulls
    assert cast(Any, settled.result)["output"]["number"] == pull["number"]
    # The reconciler ran in the operation's worker, as a reconcile job.
    worker, document, _ = helper.requests[-1]
    assert worker == "vcs-open-change"
    job = json.loads(document)
    assert (job["entry"], job["reconcile"]) == ("vcs.find_change", True)
    assert "content_gate" not in job["context"]  # a reconciler only reads
    assert_token_absent(kernel, helper, session, caplog)


def test_check_repository_reports_what_is_missing(
    kernel: Kernel, session: FakeSession, host: FakeGitHub
) -> None:
    repo = host.repositories["acme/cardea"]
    _, _, (bare,) = run_calls(kernel, session, [("vcs-check_repository", {"repository": "cardea"})])
    report = bare.structured_content
    assert report["ready"] is False and report["default_branch"] == "main"
    assert report["missing"] == [
        "default_branch_requires_review",
        "pushes_outside_agent_prefix_blocked",
    ]
    repo.rulesets = [
        {
            "include": ["~DEFAULT_BRANCH"],
            "rules": [
                {
                    "type": "pull_request",
                    "parameters": {
                        "required_approving_review_count": 1,
                        "require_last_push_approval": True,
                    },
                }
            ],
        },
        {
            "include": ["~ALL"],
            "exclude": ["agent/**"],
            "rules": [{"type": "creation"}, {"type": "update"}, {"type": "deletion"}],
        },
        {"include": ["agent/**"], "rules": [{"type": "non_fast_forward"}]},
    ]
    check = [("vcs-check_repository", {"repository": "cardea"})]
    _, _, (ready,) = run_calls(kernel, session, check)
    report = ready.structured_content
    assert report["ready"] is True and report["missing"] == []
    assert all(c["ok"] for c in report["checks"]), report["checks"]
    # Rules that also stop the agents' own branches make it not ready.
    repo.rulesets[1]["exclude"] = []
    _, _, (closed,) = run_calls(
        kernel, session, [("vcs-check_repository", {"repository": "cardea"})]
    )
    assert closed.structured_content["missing"] == ["agent_prefix_pushable"]


def test_a_tampered_staged_input_is_refused_in_the_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "inputs"
    folder.mkdir()
    (folder / "0").write_bytes(b"changed")
    monkeypatch.setenv("CARDEA_INPUTS", str(folder))
    manifest = {"/change": {"file": "0", "sha256": hashlib.sha256(b"x").hexdigest(), "bytes": 7}}
    with pytest.raises(OperationRejected) as refused:
        operation_worker.staged_inputs(manifest)
    assert refused.value.code == "input_digest_mismatch"
    good = {"/change": {"file": "0", "sha256": hashlib.sha256(b"changed").hexdigest(), "bytes": 7}}
    assert operation_worker.staged_inputs(good) == {"/change": b"changed"}


def test_a_reconcile_job_never_runs_the_handler() -> None:
    listed = ["vcs.open_change", "vcs.find_change"]
    assert operation_worker.choose(listed, {}) == "vcs.open_change"
    assert operation_worker.choose(listed, {"entry": "vcs.find_change", "reconcile": True}) == (
        "vcs.find_change"
    )
    assert operation_worker.choose(listed, {"entry": "vcs.find_review", "reconcile": True}) is None
    assert operation_worker.choose(["vcs.read_change"], {"reconcile": True}) is None


def test_a_shared_configuration_may_not_hold_a_token(tmp_path: Path) -> None:
    config = tmp_path / "vcs.json"
    config.write_text(
        json.dumps({"repositories": {}, "credentials": {"kind": "test", "token": TOKEN}})
    )
    config.chmod(0o644)
    with pytest.raises(Fault) as refused:
        load(config)
    assert refused.value.code == "unsafe_file"
    config.chmod(0o600)
    assert load(config).credentials.kind == "test"
    shared = {"repositories": {}, "credentials": {"kind": "profile_credential"}}
    config.write_text(json.dumps(shared))
    config.chmod(0o666)
    with pytest.raises(Fault):
        load(config)


def test_without_the_profile_credential_nothing_is_sent(
    kernel: Kernel, session: FakeSession, host: FakeGitHub, helper: LocalVcsHost
) -> None:
    del helper.env["CARDEA_CREDENTIAL"]
    before = len(host.authorizations)
    _, _, (refused,) = run_calls(
        kernel, session, [("vcs-read_change", {"repository": "cardea", "number": 1})]
    )
    assert refused.is_error and "credential_unavailable" in text(refused)
    assert untouched(host, before)
