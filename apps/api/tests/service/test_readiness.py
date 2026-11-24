"""`/livez` and `/readyz`: each readiness check passes and fails on its own, and the endpoint
answers 503 when any fails. Portable fakes only: no helper, proxy, host or DBOS executor."""

import json
import os
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from cardea.kernel.models import BudgetLimits, DispatchLease, Task, TaskState
from cardea.service import kernel as handlers
from cardea.service import readiness
from cardea.service.app import create_app
from cardea.service.auth import Authentication
from cardea.service.database import Database, Fault
from cardea.service.kernel import Kernel
from cardea.service.operations import OperationRejected
from cardea.service.readiness import Readiness

BEGAN = datetime(2026, 11, 24, 9, 0, tzinfo=UTC)


class Host:
    def __init__(self, proxies: tuple[str, ...] = ("127.0.10.1:3128",)) -> None:
        self.refuse = False
        self.proxies = proxies

    def profiles(self) -> dict[str, Any]:
        if self.refuse:
            raise Fault("host_unavailable", "Host result could not be established", 503)
        return {
            "profiles": {
                f"p{i}": {"network": {"policy": "public_web", "proxy": proxy}}
                for i, proxy in enumerate(self.proxies)
            }
            | {"quiet": {"network": {"policy": "none"}}},
            "workers": {},
        }


class Workers:
    """Answers each token probe in turn, recording who was asked."""

    def __init__(self, *answers: dict[str, Any] | OperationRejected) -> None:
        self.answers = list(answers)
        self.asked: list[dict[str, Any]] = []

    @property
    def host(self) -> Host:
        return Host()

    async def run(self, job: str, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        self.asked.append({"payload": payload, **kwargs})
        answer = self.answers.pop(0)
        if isinstance(answer, OperationRejected):
            raise answer
        return answer


class FakeClient:
    def __init__(self, **kwargs: Any) -> None:
        pass

    def destroy(self) -> None:
        pass


@pytest.fixture
def kernel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Kernel]:
    monkeypatch.setattr(handlers, "DBOSClient", FakeClient)
    db = Database(tmp_path / "data")
    db.migrate()
    instance = Kernel(db, cast(Authentication, None))
    try:
        yield instance
    finally:
        db.close()


def probe(kernel: Kernel, **options: Any) -> Readiness:
    return Readiness(kernel, executor=lambda: None, **options)


def statuses(report: dict[str, Any]) -> dict[str, str]:
    return {c["name"]: c["status"] for c in report["checks"]}


def sql(kernel: Kernel, statement: str, *values: Any) -> None:
    with kernel.database.engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        conn.exec_driver_sql(statement, values)
        conn.commit()


def test_a_bare_kernel_is_ready(kernel: Kernel) -> None:
    report = probe(kernel).report()
    assert report["status"] == "ok"
    assert statuses(report) == {
        name: "ok"
        for name in (
            "database",
            "dbos_executor",
            "host_helper",
            "egress_proxy",
            "repository_mirrors",
            "vcs_tokens",
            "supervisor_and_triggers",
            "member_leases",
        )
    }
    assert all(c["reason"] for c in report["checks"])


def test_endpoints_answer_200_and_503(kernel: Kernel, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(readiness, "executor_running", lambda: None)
    client = TestClient(create_app(kernel))
    live = client.get("/livez")  # no credential
    assert (live.status_code, live.json()) == (200, {"status": "ok"})
    ready = client.get("/readyz")
    assert (ready.status_code, ready.json()["status"]) == (200, "ok")

    def stopped() -> None:
        raise RuntimeError("DBOS is not launched")

    monkeypatch.setattr(readiness, "executor_running", stopped)
    client = TestClient(create_app(kernel))
    ready = client.get("/readyz")
    assert ready.status_code == 503
    body = ready.json()
    assert body["status"] == "fail"
    assert statuses(body)["dbos_executor"] == "fail"
    assert statuses(body)["database"] == "ok"
    assert client.get("/livez").status_code == 200  # the process still answers


def test_a_failing_check_does_not_hide_the_others(kernel: Kernel) -> None:
    def stopped() -> None:
        raise RuntimeError("secret detail")

    report = Readiness(kernel, executor=stopped).report()
    failed = [c for c in report["checks"] if c["status"] == "fail"]
    assert [c["name"] for c in failed] == ["dbos_executor"]
    assert "RuntimeError" in failed[0]["reason"]
    assert "secret detail" not in json.dumps(report)


def test_the_helper_must_answer_and_accept_the_workers_file(kernel: Kernel) -> None:
    host = Host()
    kernel.workers = cast(Any, SimpleNamespace(host=host))
    assert statuses(probe(kernel).report())["host_helper"] == "ok"
    host.refuse = True
    report = probe(kernel).report()
    assert statuses(report)["host_helper"] == "fail"
    assert statuses(report)["egress_proxy"] == "fail"


def test_every_egress_proxy_must_accept_a_connection(kernel: Kernel) -> None:
    kernel.workers = cast(Any, SimpleNamespace(host=Host(("127.0.10.1:3128", "127.0.10.2:3128"))))
    reached: list[tuple[str, int]] = []

    def connect(address: tuple[str, int], timeout: float) -> Any:
        reached.append(address)
        if address[0] == "127.0.10.2":
            raise ConnectionRefusedError
        return SimpleNamespace(close=lambda: None)

    report = probe(kernel, connect=connect).report()
    assert reached == [("127.0.10.1", 3128), ("127.0.10.2", 3128)]
    check = next(c for c in report["checks"] if c["name"] == "egress_proxy")
    assert check["status"] == "fail"
    assert "127.0.10.2:3128" in check["reason"]
    assert "127.0.10.1" not in check["reason"]


def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **sources: Path) -> None:
    path = tmp_path / "repositories.json"
    path.write_text(
        json.dumps({"repositories": {alias: {"source": str(s)} for alias, s in sources.items()}})
    )
    path.chmod(0o600)
    monkeypatch.setenv("CARDEA_REPOSITORIES", str(path))


def mirror(path: Path, age: float) -> Path:
    path.mkdir()
    for name in ("HEAD", "FETCH_HEAD"):
        (path / name).write_text("x")
        stamp = time.time() - age
        os.utime(path / name, (stamp, stamp))
    return path


def test_a_mirror_older_than_ten_minutes_is_not_ready(
    kernel: Kernel, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry(
        tmp_path,
        monkeypatch,
        fresh=mirror(tmp_path / "fresh.git", 60),
        old=mirror(tmp_path / "old.git", 601),
        gone=tmp_path / "gone.git",
    )
    check = next(c for c in probe(kernel).report()["checks"] if c["name"] == "repository_mirrors")
    assert check["status"] == "fail"
    assert "old (older than 10 min)" in check["reason"]
    assert "gone (no mirror)" in check["reason"]
    assert "fresh (" not in check["reason"]
    assert str(tmp_path) not in check["reason"]


def test_fresh_mirrors_are_ready(
    kernel: Kernel, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry(tmp_path, monkeypatch, one=mirror(tmp_path / "one.git", 9))
    assert statuses(probe(kernel).report())["repository_mirrors"] == "ok"


def host_with_vcs(kernel: Kernel, workers: Workers) -> None:
    kernel.workers = cast(Any, workers)
    executor = SimpleNamespace(profile="vcs", worker="vcs-push-branch")
    descriptor = SimpleNamespace(cardea=SimpleNamespace(executor=executor))
    kernel.operations = cast(Any, SimpleNamespace(descriptors={"vcs.push_branch": descriptor}))


def test_each_pusher_and_reviewer_token_is_checked_by_the_worker(
    kernel: Kernel, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = {"owner": "o", "name": "n", "api_url": "https://api.example.invalid", "git_url": "u"}
    path = tmp_path / "repositories.json"
    path.write_text(json.dumps({"repositories": {"app": {"source": "/m", **api}}}))
    path.chmod(0o600)
    monkeypatch.setenv("CARDEA_REPOSITORIES", str(path))
    workers = Workers(
        {"applied": True, "output": {"reason": "ok"}},
        {"applied": False, "output": {"reason": "refused (403): no write access"}},
    )
    host_with_vcs(kernel, workers)
    monkeypatch.setattr(
        Readiness,
        "_holders",
        lambda self: [
            ("vcs.push_branch", "pusher", "dev", "developers", "dev.developers.0"),
            ("vcs.push_branch", "reviewer", "dev", "reviewers", "dev.reviewers.0"),
        ],
    )
    now = [1000.0]
    ready = probe(kernel, clock=lambda: now[0])
    report = ready.report()
    check = next(c for c in report["checks"] if c["name"] == "vcs_tokens")
    assert check["status"] == "fail"
    assert "dev.reviewers reviewer on app: refused (403): no write access" in check["reason"]
    assert "pusher" not in check["reason"]
    assert [a["payload"]["arguments"] for a in workers.asked] == [
        {"repository": "app", "role": "pusher"},
        {"repository": "app", "role": "reviewer"},
    ]
    assert all(a["payload"]["reconcile"] is True for a in workers.asked)
    assert {a["payload"]["entry"] for a in workers.asked} == {"vcs.check_token"}
    assert {a["profile"] for a in workers.asked} == {"vcs"}
    # Answers are kept for a minute, so a probe loop does not call the repository host.
    now[0] += readiness.TOKEN_CACHE_SECONDS - 1
    assert statuses(ready.report())["vcs_tokens"] == "fail"
    assert len(workers.asked) == 2


def test_a_worker_that_does_not_serve_the_check_fails_it(kernel: Kernel) -> None:
    workers = Workers(OperationRejected("this worker does not serve that entry point"))
    host_with_vcs(kernel, workers)
    ready = probe(kernel)
    ok, reason = ready._probe("vcs.push_branch", "pusher", "dev", "dev.developers.0", "app")
    assert not ok
    assert "does not serve" in reason


def test_a_trigger_needs_an_enabled_supervisor_and_a_slot_that_admits(kernel: Kernel) -> None:
    at = BEGAN.isoformat()
    for who, disabled in (("good", 0), ("off", 1)):
        sql(
            kernel,
            "INSERT INTO principals(principal_id,kind,disabled,issuer,subject) VALUES (?,?,?,?,?)",
            who,
            "human",
            disabled,
            "test",
            who,
        )
    for trigger, supervisor in (("ok", "good"), ("disabled", "off"), ("refused", "good")):
        sql(
            kernel,
            "INSERT INTO triggers(trigger_id,supervisor_id,namespace,definition_json,"
            "definition_sha256,enabled,removed,anchor,updated_at) VALUES (?,?,?,?,?,1,0,?,?)",
            trigger,
            supervisor,
            "test",
            "{}",
            "0" * 64,
            at,
            at,
        )
    ready = probe(kernel)
    ok, reason = ready.triggers()
    assert not ok
    assert "disabled: its supervisor is missing or disabled" in reason
    assert "ok:" not in reason
    sql(kernel, "UPDATE principals SET disabled=0 WHERE principal_id='off'")
    assert ready.triggers()[0]
    sql(
        kernel,
        "INSERT INTO trigger_events(trigger_id,kind,slot,code,detail_json,at) "
        "VALUES ('refused','failed',?,'budget_exhausted','{}',?)",
        at,
        at,
    )
    ok, reason = ready.triggers()
    assert not ok
    assert "refused: its last slot was refused (budget_exhausted)" in reason
    sql(
        kernel,
        "INSERT INTO trigger_events(trigger_id,kind,slot,task_id,detail_json,at) "
        "VALUES ('refused','admitted',?,'task-1','{}',?)",
        at,
        at,
    )
    assert ready.triggers()[0]


def test_a_member_running_past_its_lease_is_not_ready(
    kernel: Kernel, monkeypatch: pytest.MonkeyPatch
) -> None:
    limits = BudgetLimits(
        max_steps=1,
        max_model_calls=1,
        max_model_tokens=1,
        max_tool_milliseconds=1,
        max_output_bytes=1,
        max_concurrent_operations=1,
        deadline=BEGAN + timedelta(hours=2),
    )
    task = Task(
        task_id="task-a",
        agent_id="dev.developers.0",
        objective="work",
        runtime_id="runtime",
        verifier_id="verifier",
        budget_account_id="budget",
        limits=limits,
        state=TaskState.RUNNING,
        created_at=BEGAN,
        updated_at=BEGAN + timedelta(minutes=1),
        lease=DispatchLease(
            lease_id="lease-a",
            worker_id="worker",
            acquired_at=BEGAN,
            expires_at=BEGAN + timedelta(minutes=10),
        ),
    )
    snapshot = task.model_dump_json()
    sql(kernel, "INSERT INTO tasks VALUES (?,?,?,?)", "task-a", 0, "running", snapshot)
    sql(
        kernel,
        "INSERT INTO task_bindings VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        "task-a",
        "sup",
        "dev.developers.0",
        "runtime",
        "dev",
        "r",
        "d",
        "{}",
        "[]",
        0,
        None,
    )
    sql(
        kernel,
        "INSERT INTO group_members(principal_id,namespace,group_name,pool,ordinal,state,"
        "created_at) VALUES (?,?,?,?,?,?,?)",
        "dev.developers.0",
        "dev",
        "dev",
        "developers",
        0,
        "active",
        BEGAN.isoformat(),
    )
    clock = [BEGAN + timedelta(minutes=5)]
    monkeypatch.setattr(readiness, "now", lambda: clock[0])
    assert probe(kernel).leases()[0]
    clock[0] = BEGAN + timedelta(minutes=11)
    ok, reason = probe(kernel).leases()
    assert not ok
    assert "task-a" in reason


def test_a_store_that_refuses_writes_is_not_ready(kernel: Kernel) -> None:
    sql(
        kernel,
        "CREATE TRIGGER no_writes BEFORE UPDATE ON service_meta "
        "BEGIN SELECT RAISE(ABORT, 'read-only'); END",
    )
    report = probe(kernel).report()
    assert statuses(report)["database"] == "fail"


def test_the_token_check_covers_each_pool_granted_a_role(kernel: Kernel) -> None:
    def pool(name: str, *grants: str) -> dict[str, Any]:
        return {
            "name": name,
            "replicas": 2,
            "grants": list(grants),
            "run": {
                "instructions": "Work on the repository.",
                "operations": [g for g in grants if g.startswith("vcs.")],
                "model": "model-a",
                "bounds": {"timeout_seconds": 60, "attempts": 1},
            },
        }

    spec = {
        "supervisor": "sup",
        "pools": [
            pool("developers", "vcs.push_branch", "tasks.inspect"),
            pool("reviewers", "vcs.review"),
            pool("writers", "tasks.inspect"),
        ],
    }
    sql(
        kernel,
        "INSERT INTO agent_groups VALUES (?,?,?,?,?,?,?,?,?)",
        "test",
        "dev",
        "sup",
        1,
        1,
        json.dumps(spec),
        "0" * 64,
        BEGAN.isoformat(),
        BEGAN.isoformat(),
    )
    for pool_name, ordinal in (("developers", 0), ("developers", 1), ("reviewers", 0)):
        sql(
            kernel,
            "INSERT INTO group_members(principal_id,namespace,group_name,pool,ordinal,state,"
            "created_at) VALUES (?,?,?,?,?,?,?)",
            f"dev.{pool_name}.{ordinal}",
            "test",
            "dev",
            pool_name,
            ordinal,
            "active",
            BEGAN.isoformat(),
        )
    assert probe(kernel)._holders() == [
        ("vcs.push_branch", "pusher", "dev", "developers", "dev.developers.0"),
        ("vcs.review", "reviewer", "dev", "reviewers", "dev.reviewers.0"),
    ]
