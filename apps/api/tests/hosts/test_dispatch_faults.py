"""Pre-fence host rejections, transient monitoring faults and operator-visible logging."""

import json
import logging
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from test_execution import Host, admit, state
from test_execution import kernel as kernel
from test_m2_completion import budget_row, start_message
from test_m2_completion import helper as helper

from cardea.hosts import debian, launcher
from cardea.hosts.debian import DebianHost
from cardea.service import host_runtime
from cardea.service.database import Fault, borrow
from cardea.service.host_runtime import HostRuntime
from cardea.service.kernel import Kernel
from cardea.service.server import JournalFormatter


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"deadline": 0}, "expired_authority"),
        ({"operation": "shell"}, "unknown_operation"),
        ({"input": "not base64!"}, "start_rejected"),
    ],
)
def test_launcher_marks_pre_fence_refusals_as_rejected(
    helper: Path, change: dict[str, Any], reason: str
) -> None:
    job = "a" * 32
    with pytest.raises(launcher.Rejected, match=f"^{reason}$"):
        launcher.handle(start_message(job) | change)
    assert not (helper / job).exists()


def test_launcher_rejects_failed_preflight_and_full_spool(
    helper: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unsupported() -> dict[str, Any]:
        raise ValueError("unsupported_kernel")

    monkeypatch.setattr(launcher, "preflight", unsupported)
    with pytest.raises(launcher.Rejected, match="^unsupported_kernel$"):
        launcher.handle(start_message("a" * 32))
    monkeypatch.setattr(launcher, "preflight", lambda: {})
    for index in range(256):
        (helper / f"{index:032x}").mkdir()
    with pytest.raises(launcher.Rejected, match="^spool_capacity$"):
        launcher.handle(start_message("f" * 32))


def test_launcher_never_rejects_after_the_fence(helper: Path) -> None:
    job = "a" * 32
    launcher.handle(start_message(job))
    with pytest.raises(FileExistsError) as raised:
        launcher.handle(start_message(job))
    assert not isinstance(raised.value, launcher.Rejected)


def helper_result(monkeypatch: pytest.MonkeyPatch, code: int, body: dict[str, Any]) -> list[Any]:
    timeouts: list[Any] = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        timeouts.append(kwargs["timeout"])
        return subprocess.CompletedProcess(args, code, json.dumps(body), "")

    monkeypatch.setattr(subprocess, "run", run)
    return timeouts


def test_debian_host_maps_rejection_and_bounds_start(monkeypatch: pytest.MonkeyPatch) -> None:
    timeouts = helper_result(monkeypatch, 1, {"error": "spool_capacity", "rejected": True})
    with pytest.raises(Fault) as raised:
        DebianHost().start("a" * 32, b"{}", time.time() + 30)
    assert (raised.value.code, raised.value.status) == ("host_rejected", 409)
    assert "spool_capacity" in raised.value.message
    assert timeouts == [debian.START_TIMEOUT]
    assert debian.START_TIMEOUT < 10  # below the kernel's SQLite busy_timeout

    helper_result(monkeypatch, 1, {"error": "host_request_failed"})
    with pytest.raises(Fault) as raised:
        DebianHost().status("a" * 32)
    assert (raised.value.code, raised.value.status) == ("host_unavailable", 503)


def test_rejected_start_fails_immediately_without_charge(kernel: Kernel) -> None:
    task_id = admit(kernel)
    host = cast(Host, kernel.host)

    def rejected(job: str, content: bytes, deadline: float, operation: str = "host.sha256") -> None:
        raise Fault("host_rejected", "Host refused the job: spool_capacity", 409)

    host.start = rejected  # type: ignore[method-assign]
    started = time.monotonic()
    assert HostRuntime(kernel).execute(task_id) == "failed"
    assert time.monotonic() - started < 5  # no lease-expiry wait
    assert state(kernel, task_id) == ("failed", "failed")
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        failure = tx.get_task(task_id).failure
        assert failure is not None and failure.code == "host_rejected"
    row = budget_row(kernel, task_id)
    assert (row["tool_ms"], row["output_bytes"], row["settled"]) == (0, 0, 1)


def test_transient_monitoring_faults_are_repolled(kernel: Kernel) -> None:
    task_id = admit(kernel)
    host = cast(Host, kernel.host)
    finished = host.status
    calls = {"status": 0}

    def flaky(job: str) -> dict[str, Any]:
        calls["status"] += 1
        if calls["status"] <= 2:
            raise Fault("host_unavailable", "Host result could not be established", 503)
        return finished(job)

    host.status = flaky  # type: ignore[method-assign]
    assert HostRuntime(kernel).execute(task_id) == "succeeded"
    assert calls["status"] == 3 and host.starts == 1


def test_transient_identity_outage_is_repolled(kernel: Kernel) -> None:
    task_id = admit(kernel)
    auth = kernel.auth
    real = auth.runtime_proof
    host = cast(Host, kernel.host)
    original = host.start
    outage = {"left": 0}

    def started(job: str, content: bytes, deadline: float, operation: str = "host.sha256") -> None:
        original(job, content, deadline, operation)
        outage["left"] = 2

    def proof() -> Any:
        if outage["left"]:
            outage["left"] -= 1
            raise Fault("runtime_identity_unavailable", "Runtime identity unavailable", 503)
        return real()

    host.start = started  # type: ignore[method-assign]
    auth.runtime_proof = proof  # type: ignore[method-assign]
    assert HostRuntime(kernel).execute(task_id) == "succeeded"
    assert outage["left"] == 0


def test_persistent_outage_becomes_outcome_unknown_at_lease_expiry(
    kernel: Kernel, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = admit(kernel)
    host = cast(Host, kernel.host)
    real_now = host_runtime.now  # type: ignore[attr-defined]
    expired: dict[str, datetime | None] = {"at": None}

    def unavailable(job: str) -> dict[str, Any]:
        with kernel.database.engine.begin() as conn, borrow(conn) as tx:
            lease = tx.get_task(task_id).lease
            assert lease is not None
            expired["at"] = lease.expires_at + timedelta(seconds=1)
        raise Fault("host_unavailable", "Host result could not be established", 503)

    host.status = unavailable  # type: ignore[method-assign]
    monkeypatch.setattr(host_runtime, "now", lambda: expired["at"] or real_now())
    assert HostRuntime(kernel).execute(task_id) == "failed"
    assert state(kernel, task_id) == ("failed", "outcome_unknown")


def test_journal_formatter_emits_structured_fields() -> None:
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        record = logging.getLogger("cardea.test").makeRecord(
            "cardea.test",
            logging.ERROR,
            __file__,
            1,
            "dispatch failed",
            (),
            sys.exc_info(),
            extra={"task_id": "task-1", "code": "x", "secret": "dropped"},
        )
    entry = json.loads(JournalFormatter().format(record))
    assert entry["msg"] == "dispatch failed" and entry["task_id"] == "task-1"
    assert entry["code"] == "x" and "secret" not in entry
    assert "RuntimeError: boom" in entry["exc"]
