"""Kernel side of confined operations and sessions (ADR-028); portable fakes, no host."""

import asyncio
import base64
import hashlib
import json
import time
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import rfc8785
from mcp.client.session import ClientSession
from pydantic import ValidationError
from test_agent_runs import FakeSession, call
from test_operations import Auth, FakeClient

from cardea.kernel.models import OperationState
from cardea.runtime.agent_session import SessionRequest, SessionResult
from cardea.service import agent_run_runtime, confined, operation_runtime
from cardea.service import kernel as handlers
from cardea.service.agent_run_runtime import AgentRunRuntime
from cardea.service.auth import Authentication
from cardea.service.confined import (
    ConfinedAgentSession,
    Workers,
    WorkerStopped,
    job_id,
)
from cardea.service.database import Database, Fault, borrow
from cardea.service.kernel import Kernel, provision
from cardea.service.models import (
    RIGHTS,
    AgentRunRequest,
    AgentSessionConfig,
    OperationTaskRequest,
    Permission,
    Principal,
    Provisioning,
    now,
)
from cardea.service.operation_runtime import OperationRuntime
from cardea.service.operations import OperationDescriptor, OperationRejected, Registry

INSTALLED = {
    "profiles": {
        "browser": {"memory_mb": 512, "network": {"policy": "allow_list"}, "sha256": "p-browser"},
        "session": {"memory_mb": 768, "network": {"policy": "model_api_only"}, "sha256": "p-s"},
    },
    "workers": {"fetch": ["browser"], "post": ["browser"], "agent": ["session"]},
    "sha256": "config",
}


def finished(document: object, result: str = "success", exit_status: str = "0") -> dict[str, Any]:
    return {
        "state": "finished",
        "result": result,
        "exit_status": exit_status,
        "output": base64.b64encode(json.dumps(document).encode()).decode(),
    }


class WorkerHost:
    """Plays the root helper: records starts, stops and cleanups."""

    def __init__(self) -> None:
        self.installed: dict[str, Any] = json.loads(json.dumps(INSTALLED))
        self.starts: list[dict[str, Any]] = []
        self.stops: list[str] = []
        self.cleaned: list[str] = []
        self.outcome = finished({"output": {"text": "page text"}})
        self.reject = False
        self.running = 0  # polls reporting "running" before the outcome; -1 forever
        self.on_status: Callable[[], None] | None = None

    def profiles(self) -> dict[str, Any]:
        return self.installed

    def start_confined(
        self, job: str, content: bytes, deadline: float, profile: str, worker: str
    ) -> None:
        if self.reject:
            raise Fault("host_rejected", "Host refused the job: spool_capacity", 409)
        self.starts.append(
            {
                "job": job,
                "payload": json.loads(content),
                "deadline": deadline,
                "profile": profile,
                "worker": worker,
            }
        )

    def status(self, job: str) -> dict[str, Any]:
        if self.on_status is not None:
            self.on_status()
        if self.running:
            self.running -= 1
            return {"state": "running"}
        return self.outcome

    def stop(self, job: str) -> dict[str, Any]:
        self.stops.append(job)
        return {"stopped": True}

    def cleanup(self, job: str) -> dict[str, Any]:
        self.cleaned.append(job)
        return {"cleaned": True}


def descriptor(name: str, effect: str, worker: str, **binding: Any) -> OperationDescriptor:
    return OperationDescriptor.model_validate(
        {
            "tool": {
                "name": name,
                "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}}},
                "outputSchema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            },
            "cardea": {
                "version": 1,
                "effect": effect,
                "executor": {"kind": "confined", "profile": "browser", "worker": worker},
                "timeout_seconds": 1,
                **binding,
            },
        }
    )


GRANTS = (*RIGHTS, "models.invoke", "pages.fetch", "pages.post")


@pytest.fixture
def host() -> WorkerHost:
    return WorkerHost()


@pytest.fixture
def kernel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: WorkerHost) -> Iterator[Kernel]:
    monkeypatch.setattr(handlers, "DBOSClient", FakeClient)
    monkeypatch.setattr(operation_runtime, "LEASE_MARGIN", 0.3)
    monkeypatch.setattr(operation_runtime, "SETTLE_MARGIN", 0.1)
    monkeypatch.setattr(agent_run_runtime, "POLL_SECONDS", 0.05)
    monkeypatch.setattr(confined, "POLL_SECONDS", 0.01)
    registry = Registry(
        (descriptor("pages.fetch", "read", "fetch"), descriptor("pages.post", "effect", "post")),
        {},
    )
    database = Database(tmp_path / "data")
    database.migrate()
    principals = tuple(
        Principal(
            principal_id=p,
            kind="service" if p == "runtime" else "human",
            auth_kind="spiffe",
            issuer="test",
            subject=p,
        )
        for p in ("runtime", "supervisor", "outsider")
    ) + (Principal(principal_id="agent", kind="agent", runtime_id="runtime"),)
    granted = tuple(
        Permission(
            principal_id="supervisor",
            namespace="test",
            operation=right,
            delegate=True,
            expires_at=now() + timedelta(hours=1),
        )
        for right in GRANTS
    )
    provision(
        database,
        Provisioning(
            principals=principals,
            permissions=granted,
            platform=tuple(p.model_copy(update={"principal_id": "platform"}) for p in granted),
        ),
        registry,
    )
    instance = Kernel(
        database, cast(Authentication, Auth()), operations=registry, workers=Workers(host)
    )
    try:
        yield instance
    finally:
        database.close()


def submit(kernel: Kernel, operation: str = "pages.fetch", **changes: Any) -> str:
    request = OperationTaskRequest.model_validate(
        {
            "request_id": f"{operation}-one",
            "agent_id": "agent",
            "namespace": "test",
            "objective": "Read one page",
            "operation": operation,
            "arguments": {"url": "https://example.org"},
            "grants": [*RIGHTS, operation],
            **changes,
        }
    )
    return kernel.submit(kernel.auth.validate("supervisor"), request).task.task_id


def receipt(kernel: Kernel, task_id: str, operation_id: str = "execute") -> Any:
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        return tx.get_operation(task_id, operation_id)


def failure(kernel: Kernel, task_id: str) -> str:
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        task = tx.get_task(task_id)
        return task.failure.code if task.failure else ""


def cancel_on_status(kernel: Kernel, task_id: str) -> Callable[[], None]:
    def cancel() -> None:
        with kernel.database.engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT OR IGNORE INTO control_requests VALUES (?,?,?,?)",
                (task_id, "cancel-1", "cancel", 0),
            )

    return cancel


# Descriptors


def test_descriptor_declares_exactly_one_executor() -> None:
    document = descriptor("pages.fetch", "read", "fetch").model_dump(mode="json", by_alias=True)
    with pytest.raises(ValidationError):
        OperationDescriptor.model_validate(
            document | {"cardea": document["cardea"] | {"handler": "test.fetch"}}
        )
    with pytest.raises(ValidationError):
        OperationDescriptor.model_validate(
            document | {"cardea": document["cardea"] | {"executor": None}}
        )
    with pytest.raises(ValidationError):
        OperationDescriptor.model_validate(
            document
            | {"cardea": document["cardea"] | {"executor": {"kind": "confined", "profile": "x"}}}
        )


def test_in_process_digest_is_unchanged_by_executors() -> None:
    document = {
        "tool": {
            "name": "mail.send",
            "inputSchema": {"type": "object"},
            "outputSchema": {"type": "object"},
        },
        "cardea": {"version": 1, "effect": "effect", "handler": "test.handler"},
    }
    described = OperationDescriptor.model_validate(document)
    dumped = described.model_dump(mode="json")
    assert "executor" in dumped["cardea"]
    del dumped["cardea"]["executor"]
    assert described.digest == hashlib.sha256(rfc8785.dumps(dumped)).hexdigest()
    assert Registry((described,), None).entry_points() == {"test.handler"}
    assert Registry((descriptor("pages.fetch", "read", "fetch"),), {}).entry_points() == set()


# Admission


def test_profile_not_installed_is_refused_at_admission(kernel: Kernel, host: WorkerHost) -> None:
    host.installed["workers"]["fetch"] = ["session"]  # the worker is not allowed under browser
    with pytest.raises(Fault) as refused:
        submit(kernel)
    assert refused.value.code == "worker_profile_not_installed"
    del host.installed["profiles"]["browser"]
    with pytest.raises(Fault):
        submit(kernel)
    kernel.workers = None
    with pytest.raises(Fault) as disabled:
        submit(kernel)
    assert disabled.value.code == "worker_profile_not_installed"
    assert host.starts == []


def test_profile_removed_after_admission_refuses_dispatch(kernel: Kernel, host: WorkerHost) -> None:
    task_id = submit(kernel)
    kernel.workers = Workers(WorkerHost())
    cast(WorkerHost, kernel.workers.host).installed = {"profiles": {}, "workers": {}}
    assert OperationRuntime(kernel).execute(task_id) == "failed"
    assert failure(kernel, task_id) == "worker_profile_not_installed"
    assert host.starts == []


def test_unavailable_profile_listing_installs_nothing(kernel: Kernel, host: WorkerHost) -> None:
    def broken() -> dict[str, Any]:
        raise Fault("host_unavailable", "down", 503)

    kernel.workers = Workers(host)
    host.profiles = broken  # type: ignore[method-assign]
    with pytest.raises(Fault) as refused:
        submit(kernel)
    assert refused.value.code == "worker_profile_not_installed"


# Execution


def test_confined_read_passes_json_and_limits_through(kernel: Kernel, host: WorkerHost) -> None:
    task_id = submit(kernel)
    before = time.time()
    assert OperationRuntime(kernel).execute(task_id) == "succeeded"
    (started,) = host.starts
    done = receipt(kernel, task_id)
    assert started["job"] == job_id("operation", done.receipt_id)
    assert (started["profile"], started["worker"]) == ("browser", "fetch")
    assert started["payload"]["arguments"] == {"url": "https://example.org"}
    context = started["payload"]["context"]
    assert context["receipt_id"] == done.receipt_id and context["task_id"] == task_id
    # The worker deadline is the descriptor timeout; the profile caps it again in the helper.
    assert before < started["deadline"] <= time.time() + 1
    assert done.request.arguments["executor"] == {
        "profile": "browser",
        "worker": "fetch",
        "profile_sha256": "p-browser",
    }
    assert done.result == {"output": {"text": "page text"}}
    assert host.cleaned == [started["job"]]


def test_memory_cap_kill_of_an_effect_is_outcome_unknown(kernel: Kernel, host: WorkerHost) -> None:
    host.outcome = finished(None, result="oom-kill", exit_status="9")
    task_id = submit(kernel, "pages.post")
    assert OperationRuntime(kernel).execute(task_id) == "failed"
    assert receipt(kernel, task_id).state is OperationState.OUTCOME_UNKNOWN
    assert failure(kernel, task_id) == "reconciliation_required"
    assert host.cleaned == [host.starts[0]["job"]]
    # The service is unaffected: the next confined call runs normally.
    host.outcome = finished({"output": {"text": "ok"}})
    assert OperationRuntime(kernel).execute(submit(kernel)) == "succeeded"


def test_memory_cap_kill_of_a_read_fails_cleanly(kernel: Kernel, host: WorkerHost) -> None:
    host.outcome = finished(None, result="oom-kill", exit_status="9")
    task_id = submit(kernel)
    assert OperationRuntime(kernel).execute(task_id) == "failed"
    assert receipt(kernel, task_id).state is OperationState.FAILED
    assert failure(kernel, task_id) == "worker_memory_limit"


@pytest.mark.parametrize(
    ("outcome", "code"),
    [
        (finished(None, result="timeout", exit_status="0"), "worker_timeout"),
        (finished(None, result="exit-code", exit_status="1"), "worker_failed"),
        (finished({"text": "not wrapped"}), "worker_output_invalid"),
    ],
)
def test_other_worker_failures_of_reads(
    kernel: Kernel, host: WorkerHost, outcome: dict[str, Any], code: str
) -> None:
    host.outcome = outcome
    task_id = submit(kernel)
    assert OperationRuntime(kernel).execute(task_id) == "failed"
    assert failure(kernel, task_id) == code


def test_worker_that_never_started_is_a_definite_failure(kernel: Kernel, host: WorkerHost) -> None:
    host.reject = True
    task_id = submit(kernel, "pages.post")
    assert OperationRuntime(kernel).execute(task_id) == "failed"
    assert receipt(kernel, task_id).state is OperationState.FAILED
    assert failure(kernel, task_id) == "operation_rejected"
    assert host.stops == []


def test_worker_reported_rejection_is_a_definite_failure(kernel: Kernel, host: WorkerHost) -> None:
    host.outcome = finished({"rejected": "provider refused"})
    task_id = submit(kernel, "pages.post")
    assert OperationRuntime(kernel).execute(task_id) == "failed"
    assert receipt(kernel, task_id).state is OperationState.FAILED


def test_cancel_stops_the_worker_and_cleans_up(kernel: Kernel, host: WorkerHost) -> None:
    task_id = submit(kernel)
    host.running = -1
    host.on_status = cancel_on_status(kernel, task_id)
    assert OperationRuntime(kernel).execute(task_id) == "cancelled"
    job = host.starts[0]["job"]
    assert host.stops == [job] and host.cleaned == [job]
    assert receipt(kernel, task_id).state is OperationState.CANCELLED


def test_cancel_of_a_running_effect_stops_it_and_stays_uncertain(
    kernel: Kernel, host: WorkerHost
) -> None:
    task_id = submit(kernel, "pages.post")
    host.running = -1
    host.on_status = cancel_on_status(kernel, task_id)
    assert OperationRuntime(kernel).execute(task_id) == "failed"
    assert host.stops == [host.starts[0]["job"]]
    assert receipt(kernel, task_id).state is OperationState.OUTCOME_UNKNOWN


def test_timeout_stops_the_worker(kernel: Kernel, host: WorkerHost) -> None:
    host.running = -1
    task_id = submit(kernel, "pages.post")
    assert OperationRuntime(kernel).execute(task_id) == "failed"
    job = host.starts[0]["job"]
    assert host.stops == [job] and host.cleaned == [job]
    assert receipt(kernel, task_id).state is OperationState.OUTCOME_UNKNOWN


def test_lease_loss_stops_the_worker(host: WorkerHost, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(confined, "POLL_SECONDS", 0.01)
    host.running = -1
    workers = Workers(host)
    with pytest.raises(WorkerStopped) as stopped:
        asyncio.run(
            workers.run(
                "a" * 32,
                {"arguments": {}},
                profile="browser",
                worker="fetch",
                deadline=time.time() + 5,
                watch=lambda: "lease_lost",
            )
        )
    assert stopped.value.code == "lease_lost"
    assert host.stops == ["a" * 32] and host.cleaned == ["a" * 32]


def test_watch_reports_lease_loss_and_cancellation(kernel: Kernel) -> None:
    task_id = submit(kernel)
    assert kernel.worker_watch(task_id, "lease-other") == "lease_lost"
    cancel_on_status(kernel, task_id)()
    assert kernel.worker_watch(task_id, "lease-other") == "cancel_requested"


def test_recovery_stops_a_worker_left_by_a_crash(kernel: Kernel, host: WorkerHost) -> None:
    task_id = submit(kernel, "pages.post")

    class Crash(Exception):
        pass

    def crash(stage: str) -> None:
        if stage == "operation.intent_committed":
            raise Crash

    kernel.fault = crash
    with pytest.raises(Crash):
        OperationRuntime(kernel).execute(task_id)
    kernel.fault = lambda stage: None
    assert OperationRuntime(kernel).execute(task_id) == "failed"
    job = job_id("operation", receipt(kernel, task_id).receipt_id)
    assert host.starts == [] and job in host.stops and job in host.cleaned
    assert receipt(kernel, task_id).state is OperationState.OUTCOME_UNKNOWN


# Agent runs


def run_request(**changes: Any) -> AgentRunRequest:
    return AgentRunRequest.model_validate(
        {
            "request_id": "routine-0830",
            "agent_id": "agent",
            "namespace": "test",
            "objective": "Read listings",
            "instructions": "Fetch the page and summarize it.",
            "operations": ["pages.fetch", "pages.post"],
            "grants": list(GRANTS),
            "attempt_timeout_seconds": 30,
            **changes,
        }
    )


def test_agent_tool_call_killed_at_its_memory_cap(kernel: Kernel, host: WorkerHost) -> None:
    session = FakeSession()
    kernel.session = session

    async def script(client: ClientSession, request: SessionRequest) -> SessionResult:
        read = await call(client, "pages-fetch", url="https://example.org")
        assert read.is_error and "worker_memory_limit" in str(read.content)
        host.outcome = finished({"output": {"text": "second try"}})
        again = await call(client, "pages-fetch", url="https://example.org")
        assert not again.is_error and again.structured_content == {"text": "second try"}
        return SessionResult(text="Summarized.", is_error=False)

    session.scripts = [script]
    host.outcome = finished(None, result="oom-kill", exit_status="9")
    task_id = kernel.submit(kernel.auth.validate("supervisor"), run_request()).task.task_id
    assert AgentRunRuntime(kernel).execute(task_id) == "succeeded"
    assert receipt(kernel, task_id, "call-0001").state is OperationState.FAILED
    assert len(host.cleaned) == 2


def test_confined_session_is_refused_when_its_profile_is_missing(
    kernel: Kernel, host: WorkerHost
) -> None:
    kernel.session = ConfinedAgentSession(Workers(host), "missing", "agent", None)
    with pytest.raises(Fault) as refused:
        kernel.submit(kernel.auth.validate("supervisor"), run_request())
    assert refused.value.code == "worker_profile_not_installed"


# Confined agent sessions


def session_request(**changes: Any) -> SessionRequest:
    fields: dict[str, Any] = {
        "system_prompt": "system",
        "prompt": "do it",
        "mcp_url": "http://127.0.0.1:4100/mcp",
        "mcp_token": "token",
        "tool_names": ("pages-fetch",),
        "max_turns": 4,
        "max_cost_usd": 0.5,
        "timeout_seconds": 30.0,
        "job_key": "task-1:1",
        **changes,
    }
    return SessionRequest(**fields)


def test_confined_session_runs_in_a_worker(host: WorkerHost) -> None:
    host.outcome = finished(
        {"output": {"text": "done", "is_error": False, "cost_usd": 0.02, "turns": 3}}
    )
    session = ConfinedAgentSession(Workers(host), "session", "agent", "claude-sonnet-4-5")
    result = asyncio.run(session.run(session_request()))
    assert result == SessionResult(text="done", is_error=False, cost_usd=0.02, turns=3)
    (started,) = host.starts
    assert started["job"] == job_id("session", "task-1:1") == ConfinedAgentSession.job("task-1:1")
    assert (started["profile"], started["worker"]) == ("session", "agent")
    assert started["payload"]["model"] == "claude-sonnet-4-5"
    assert started["payload"]["request"]["mcp_url"] == "http://127.0.0.1:4100/mcp"
    # No credential crosses the helper boundary: the profile holds it.
    assert "credential" not in json.dumps(started["payload"]).lower()


def test_confined_session_memory_cap_is_a_failed_attempt(host: WorkerHost) -> None:
    host.outcome = finished(None, result="oom-kill", exit_status="9")
    session = ConfinedAgentSession(Workers(host), "session", "agent", None)
    result = asyncio.run(session.run(session_request()))
    assert result.is_error and result.stop_reason == "worker_memory_limit"


def test_cancelled_session_stops_its_worker(
    host: WorkerHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(confined, "POLL_SECONDS", 0.01)
    host.running = -1
    session = ConfinedAgentSession(Workers(host), "session", "agent", None)

    async def cancel_soon() -> None:
        running = asyncio.ensure_future(session.run(session_request()))
        await asyncio.sleep(0.1)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running

    asyncio.run(cancel_soon())
    job = job_id("session", "task-1:1")
    assert host.stops == [job] and host.cleaned == [job]


def test_session_configuration_keeps_credentials_out_of_the_service() -> None:
    AgentSessionConfig(confined_profile="session", confined_worker="agent")
    with pytest.raises(ValidationError):
        AgentSessionConfig(
            confined_profile="session",
            confined_worker="agent",
            oauth_token_file=Path("/etc/cardea/token"),
        )
    with pytest.raises(ValidationError):
        AgentSessionConfig(confined_profile="session")


# Per-profile concurrency


class SlowHost(WorkerHost):
    """Each job reports "running" for a few polls; records how many run at once."""

    def __init__(self, concurrency: int | None) -> None:
        super().__init__()
        self.installed["profiles"]["browser"]["concurrency"] = concurrency
        self.polls: dict[str, int] = {}
        self.active = 0
        self.peak = 0

    def start_confined(
        self, job: str, content: bytes, deadline: float, profile: str, worker: str
    ) -> None:
        super().start_confined(job, content, deadline, profile, worker)
        self.active += 1
        self.peak = max(self.peak, self.active)

    def status(self, job: str) -> dict[str, Any]:
        self.polls[job] = self.polls.get(job, 0) + 1
        return self.outcome if self.polls[job] > 3 else {"state": "running"}

    def stop(self, job: str) -> dict[str, Any]:
        self.active -= 1
        return super().stop(job)


async def fetch_pages(workers: Workers, count: int, deadline: float) -> list[Any]:
    results: list[Any] = await asyncio.gather(
        *(
            workers.run(f"{i:032x}", {}, profile="browser", worker="fetch", deadline=deadline)
            for i in range(count)
        ),
        return_exceptions=True,
    )
    return results


@pytest.mark.parametrize(("concurrency", "peak"), [(1, 1), (2, 2), (None, 3)])
def test_profile_concurrency_bounds_parallel_workers(
    monkeypatch: pytest.MonkeyPatch, concurrency: int | None, peak: int
) -> None:
    monkeypatch.setattr(confined, "POLL_SECONDS", 0.01)
    host = SlowHost(concurrency)
    results = asyncio.run(fetch_pages(Workers(host), 3, time.time() + 10))
    assert results == [{"text": "page text"}] * 3
    assert host.peak == peak and len(host.starts) == 3


def test_profile_slots_are_shared_across_event_loops(monkeypatch: pytest.MonkeyPatch) -> None:
    """Agent attempts run in separate threads, each with its own loop."""
    import threading

    monkeypatch.setattr(confined, "POLL_SECONDS", 0.01)
    host = SlowHost(1)
    workers = Workers(host)
    threads = [
        threading.Thread(target=lambda: asyncio.run(fetch_pages(workers, 2, time.time() + 10)))
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert host.peak == 1 and len(host.starts) == 4


def test_busy_profile_refuses_before_starting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(confined, "POLL_SECONDS", 0.01)
    host = SlowHost(1)
    workers = Workers(host)
    slot = workers._slot("browser")
    assert slot is not None and slot.acquire(blocking=False)
    (busy,) = asyncio.run(fetch_pages(workers, 1, time.time() + 0.05))
    assert isinstance(busy, confined.ExecutorBusy) and "nothing ran" in str(busy)
    assert host.starts == [] and host.stops == []
    slot.release()
    assert asyncio.run(fetch_pages(workers, 1, time.time() + 10)) == [{"text": "page text"}]


def test_waiting_call_stops_when_its_task_is_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(confined, "POLL_SECONDS", 0.01)
    host = SlowHost(1)
    workers = Workers(host)
    slot = workers._slot("browser")
    assert slot is not None and slot.acquire(blocking=False)
    waiting = workers.run(
        "a" * 32,
        {},
        profile="browser",
        worker="fetch",
        deadline=time.time() + 10,
        watch=lambda: "cancel_requested",
    )
    with pytest.raises(OperationRejected, match="cancel_requested"):
        asyncio.run(waiting)
    assert host.starts == []
    slot.release()


# Queue wait versus execution time, and failure reasons


class Timed:
    """Makes each job of a WorkerHost run for `seconds` of real time; tracks the peak."""

    def __init__(self, host: WorkerHost, seconds: float, queue_timeout: int = 600) -> None:
        self.host, self.seconds = host, seconds
        self.started: dict[str, float] = {}
        self.active = self.peak = 0
        host.installed["profiles"]["browser"] |= {
            "concurrency": 1,
            "queue_timeout_seconds": queue_timeout,
        }
        start, stop = host.start_confined, host.stop

        def start_confined(
            job: str, content: bytes, deadline: float, profile: str, worker: str
        ) -> None:
            start(job, content, deadline, profile, worker)
            self.started[job] = time.monotonic()
            self.active += 1
            self.peak = max(self.peak, self.active)

        def status(job: str) -> dict[str, Any]:
            if time.monotonic() - self.started[job] < self.seconds:
                return {"state": "running"}
            return host.outcome

        def stopped(job: str) -> dict[str, Any]:
            self.active -= 1
            return stop(job)

        host.start_confined = start_confined  # type: ignore[method-assign]
        host.status = status  # type: ignore[method-assign]
        host.stop = stopped  # type: ignore[method-assign]


def test_queue_wait_does_not_count_against_the_operation_timeout(
    kernel: Kernel, host: WorkerHost
) -> None:
    # Four parallel calls on a one-slot profile: each runs 0.4 s within its 1 s timeout,
    # but the last one waits about 1.2 s for its slot first.
    timed = Timed(host, 0.4)
    session = FakeSession()
    kernel.session = session

    async def burst(client: ClientSession, request: SessionRequest) -> SessionResult:
        results = await asyncio.gather(
            *(call(client, "pages-fetch", url=f"https://example.org/{i}") for i in range(4))
        )
        assert [r.is_error for r in results] == [False] * 4, [r.content for r in results]
        return SessionResult(text="Read four pages.", is_error=False)

    session.scripts = [burst]
    task_id = kernel.submit(kernel.auth.validate("supervisor"), run_request()).task.task_id
    begun = time.monotonic()
    assert AgentRunRuntime(kernel).execute(task_id) == "succeeded"
    assert time.monotonic() - begun > 1.5  # the burst really outlasted one call's timeout
    assert timed.peak == 1 and len(host.starts) == 4


def test_queue_expiry_is_a_distinct_retryable_failure(kernel: Kernel, host: WorkerHost) -> None:
    Timed(host, 0.1, queue_timeout=1)
    session = FakeSession()
    kernel.session = session
    assert kernel.workers is not None
    slot = kernel.workers._slot("browser")
    assert slot is not None and slot.acquire(blocking=False)

    async def busy(client: ClientSession, request: SessionRequest) -> SessionResult:
        result = await call(client, "pages-fetch", url="https://example.org")
        text = str(result.content)
        assert result.is_error and "executor_busy" in text and "queue_timeout" in text
        return SessionResult(text="The browser was busy.", is_error=False)

    session.scripts = [busy]
    task_id = kernel.submit(kernel.auth.validate("supervisor"), run_request()).task.task_id
    try:
        assert AgentRunRuntime(kernel).execute(task_id) == "succeeded"
    finally:
        slot.release()
    failure = receipt(kernel, task_id, "call-0001").failure
    assert failure.code == "executor_busy" and failure.retryable is True
    assert failure.details["reason"] == "queue_timeout"
    assert host.starts == []


@pytest.mark.parametrize(
    ("status", "code", "reason", "summary"),
    [
        (finished(None, "exit-code", "1"), "worker_failed", "worker_exit", "exit status 1"),
        (finished(None, "oom-kill", "9"), "worker_memory_limit", "worker_exit", "oom-kill"),
        (finished(None, "timeout", "0"), "worker_timeout", "timeout", "profile limit"),
        (
            {**finished(None), "output": "bm90IGpzb24="},
            "worker_output_invalid",
            "invalid_output",
            "",
        ),
        (
            finished({"error": {"type": "HTTPStatusError", "status": 502}}),
            "worker_error",
            "upstream_http_error",
            "HTTPStatusError (HTTP 502)",
        ),
        (
            finished({"error": {"type": "KeyError", "status": None}}),
            "worker_error",
            "error",
            "KeyError",
        ),
        (
            finished({"error": {"type": "Bad\nType token=abc", "status": 99}}),
            "worker_error",
            "error",
            "handler error",
        ),
    ],
)
def test_worker_results_carry_a_bounded_reason(
    status: dict[str, Any], code: str, reason: str, summary: str
) -> None:
    with pytest.raises(confined.WorkerFailed) as failed:
        confined._result(status)
    assert (failed.value.code, failed.value.reason) == (code, reason)
    assert summary in failed.value.summary and len(failed.value.summary) <= 200


def test_failed_read_receipt_records_the_reason(kernel: Kernel, host: WorkerHost) -> None:
    host.outcome = finished({"error": {"type": "HTTPStatusError", "status": 503}})
    task_id = submit(kernel)
    assert OperationRuntime(kernel).execute(task_id) == "failed"
    failure = receipt(kernel, task_id).failure
    assert failure.code == "worker_error" and failure.retryable is False
    assert failure.details == {
        "reason": "upstream_http_error",
        "summary": "HTTPStatusError (HTTP 503)",
    }


# Abandoned calls: the agent's MCP client gave up on a call


def test_an_abandoned_call_is_withdrawn_while_it_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(confined, "POLL_SECONDS", 0.01)
    host = SlowHost(1)
    workers = Workers(host)
    slot = workers._slot("browser")
    assert slot is not None and slot.acquire(blocking=False)

    async def abandoned() -> Any:
        confined.abandoned.set(lambda: True)
        return await workers.run(
            "b" * 32, {}, profile="browser", worker="fetch", deadline=time.time() + 10
        )

    with pytest.raises(confined.CallAbandoned, match="nothing ran"):
        asyncio.run(abandoned())
    assert host.starts == []
    slot.release()
    # With a free slot the worker starts at once, and abandonment no longer stops it.
    assert asyncio.run(abandoned()) == {"text": "page text"}
    assert len(host.starts) == 1 and host.stops == ["b" * 32]


def tools_call(request_id: int, **arguments: Any) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": "pages-fetch", "arguments": arguments},
    }


def mcp_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }


async def settled(kernel: Kernel, task_id: str, operation_id: str) -> Any:
    """The call's receipt once it is no longer running (the endpoint settles it)."""
    for _ in range(200):
        found = receipt(kernel, task_id, operation_id)
        if found.state is not OperationState.RUNNING:
            return found
        await asyncio.sleep(0.02)
    raise AssertionError("the call never settled")


def test_a_client_that_disconnects_withdraws_its_waiting_call(
    kernel: Kernel, host: WorkerHost
) -> None:
    Timed(host, 0.1)
    assert kernel.workers is not None
    slot = kernel.workers._slot("browser")
    assert slot is not None and slot.acquire(blocking=False)
    session = FakeSession()
    kernel.session = session
    task_id = ""

    async def gives_up(client: ClientSession, request: SessionRequest) -> SessionResult:
        # Like a harness whose MCP client stops waiting: it closes the connection.
        async with httpx.AsyncClient(timeout=0.3, trust_env=False) as http:
            with pytest.raises(httpx.TimeoutException):
                await http.post(
                    request.mcp_url,
                    json=tools_call(1, url="https://example.org"),
                    headers=mcp_headers(request.mcp_token),
                )
        withdrawn = await settled(kernel, task_id, "call-0001")
        assert withdrawn.state is OperationState.FAILED
        return SessionResult(text="Gave up.", is_error=False)

    session.scripts = [gives_up]
    task_id = kernel.submit(kernel.auth.validate("supervisor"), run_request()).task.task_id
    try:
        assert AgentRunRuntime(kernel).execute(task_id) == "succeeded"
    finally:
        slot.release()
    failure = receipt(kernel, task_id, "call-0001").failure
    assert failure.code == "call_abandoned" and failure.retryable is True
    assert failure.details["reason"] == "abandoned"
    assert host.starts == []


def test_a_cancel_notification_withdraws_the_waiting_call_it_names(
    kernel: Kernel, host: WorkerHost
) -> None:
    Timed(host, 0.1)
    assert kernel.workers is not None
    slot = kernel.workers._slot("browser")
    assert slot is not None and slot.acquire(blocking=False)
    session = FakeSession()
    kernel.session = session

    async def cancels(client: ClientSession, request: SessionRequest) -> SessionResult:
        headers = mcp_headers(request.mcp_token)
        async with httpx.AsyncClient(timeout=10, trust_env=False) as http:
            waiting = asyncio.ensure_future(
                http.post(
                    request.mcp_url, json=tools_call(7, url="https://a.example"), headers=headers
                )
            )
            await asyncio.sleep(0.3)
            # A cancellation naming another request changes nothing.
            for target in (8, 7):
                notice = {
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": {"requestId": target, "reason": "user stopped"},
                }
                accepted = await http.post(request.mcp_url, json=notice, headers=headers)
                assert accepted.status_code == 202
            answer = (await waiting).json()
        assert answer["id"] == 7 and answer["result"]["isError"] is True
        assert "call_abandoned" in answer["result"]["content"][0]["text"]
        return SessionResult(text="Cancelled.", is_error=False)

    session.scripts = [cancels]
    task_id = kernel.submit(kernel.auth.validate("supervisor"), run_request()).task.task_id
    try:
        assert AgentRunRuntime(kernel).execute(task_id) == "succeeded"
    finally:
        slot.release()
    assert receipt(kernel, task_id, "call-0001").failure.code == "call_abandoned"
    assert host.starts == []


def test_a_running_call_finishes_and_settles_after_its_client_leaves(
    kernel: Kernel, host: WorkerHost
) -> None:
    timed = Timed(host, 0.5)
    session = FakeSession()
    kernel.session = session
    task_id = ""

    async def leaves(client: ClientSession, request: SessionRequest) -> SessionResult:
        async with httpx.AsyncClient(timeout=0.2, trust_env=False) as http:
            with pytest.raises(httpx.TimeoutException):
                await http.post(
                    request.mcp_url,
                    json=tools_call(1, url="https://example.org"),
                    headers=mcp_headers(request.mcp_token),
                )
        done = await settled(kernel, task_id, "call-0001")
        assert done.state is OperationState.SUCCEEDED
        return SessionResult(text="Read it.", is_error=False)

    session.scripts = [leaves]
    task_id = kernel.submit(kernel.auth.validate("supervisor"), run_request()).task.task_id
    assert AgentRunRuntime(kernel).execute(task_id) == "succeeded"
    assert len(host.starts) == 1 and timed.active == 0
