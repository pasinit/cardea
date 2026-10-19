"""Budget serialization, installed operations and recoverable host payload retirement."""

import base64
import json
import platform
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from test_execution import Host, admit, request, state
from test_execution import kernel as kernel

from cardea.hosts import launcher
from cardea.kernel.models import ResourceRef
from cardea.service import host_budget
from cardea.service.admin import initialize, restore
from cardea.service.app import create_app
from cardea.service.database import Database, Fault, borrow
from cardea.service.host_runtime import HostRuntime
from cardea.service.kernel import Kernel


def allocation(kernel: Kernel, tool: int = 10000, output: int = 65536, concurrent: int = 1) -> None:
    with kernel.database.engine.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE host_budget_limits SET tool_ms=?,output_bytes=?,concurrency=?",
            (tool, output, concurrent),
        )


def budget_row(kernel: Kernel, task: str) -> dict[str, Any]:
    with kernel.database.engine.begin() as conn:
        return dict(
            conn.exec_driver_sql("SELECT * FROM host_reservations WHERE task_id=?", (task,))
            .mappings()
            .one()
        )


def test_concurrent_reservations_cannot_overdraw(kernel: Kernel) -> None:
    allocation(kernel)
    ids = [admit(kernel, request_id=f"r{i}") for i in range(8)]

    def reserve(identity: str) -> bool:
        try:
            with kernel.database.engine.begin() as conn:
                host_budget.reserve(conn, identity, "supervisor", "test")
            return True
        except Fault as error:
            assert error.code == "host_budget_exhausted"
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(reserve, ids)) == 1
    with pytest.raises(Fault, match="insufficient"):
        admit(kernel, request_id="exhausted")


@pytest.mark.parametrize("tool,output", [(9999, 65536), (10000, 65535)])
def test_exhausted_admission(kernel: Kernel, tool: int, output: int) -> None:
    allocation(kernel, tool, output)
    with pytest.raises(Fault) as denied:
        admit(kernel)
    assert denied.value.code == "host_budget_exhausted"
    assert cast(Host, kernel.host).starts == 0


def test_measured_settlement_releases_unused_reservation_once(kernel: Kernel) -> None:
    allocation(kernel, 10100, 65636)
    task = admit(kernel)
    host = cast(Host, kernel.host)
    original = host.status
    host.status = lambda job: original(job) | {"wall_ms": 25}  # type: ignore[method-assign]
    runtime = HostRuntime(kernel)
    assert runtime.execute(task) == "succeeded"
    row = budget_row(kernel, task)
    assert row["tool_ms"] == 25 and 0 < row["output_bytes"] < 100
    assert row["settled"] == row["cleaned"] == 1
    assert runtime.execute(task) == "succeeded"
    assert budget_row(kernel, task) == row
    assert admit(kernel, request_id="capacity-released")
    with TestClient(create_app(kernel)) as client:
        summary = client.get(
            "/v0.2/host-budget?namespace=test", headers={"Authorization": "Bearer supervisor"}
        )
        assert summary.json()["charged_tool_ms"] == 25
        assert summary.json()["active_reservations"] == 0


def test_missing_measurement_charges_full_allocation(kernel: Kernel) -> None:
    task = admit(kernel)
    assert HostRuntime(kernel).execute(task) == "succeeded"
    row = budget_row(kernel, task)
    assert row["tool_ms"] == 10000 and row["output_bytes"] == 65536


@pytest.mark.parametrize("phase", ["admission", "dispatch", "publication"])
def test_allocation_withdrawal_blocks_work(kernel: Kernel, phase: str) -> None:
    def withdraw() -> None:
        with kernel.database.engine.begin() as conn:
            conn.exec_driver_sql("DELETE FROM host_budget_limits")

    if phase == "admission":
        withdraw()
        with pytest.raises(Fault):
            admit(kernel)
        return
    task = admit(kernel)
    if phase == "dispatch":
        withdraw()
    else:
        cast(Host, kernel.host).observer = withdraw
    assert HostRuntime(kernel).execute(task) == "failed"
    assert cast(Host, kernel.host).starts == (1 if phase == "publication" else 0)


def test_cleanup_failure_keeps_receipt_and_retries(kernel: Kernel) -> None:
    task = admit(kernel)
    host = cast(Host, kernel.host)

    def failed(job: str) -> dict[str, Any]:
        raise Fault("host_unavailable", "injected cleanup failure")

    host.cleanup = failed  # type: ignore[method-assign]
    runtime = HostRuntime(kernel)
    assert runtime.execute(task) == "succeeded"
    assert budget_row(kernel, task)["cleaned"] == 0
    assert state(kernel, task) == ("succeeded", "succeeded")
    host.cleanup = lambda job: {"cleaned": True}  # type: ignore[method-assign]
    runtime.cleanup()
    assert budget_row(kernel, task)["cleaned"] == 1
    with kernel.database.engine.begin() as conn:
        assert (
            conn.exec_driver_sql(
                "SELECT COUNT(*) FROM events WHERE event_type='host.cleaned'"
            ).scalar_one()
            == 1
        )
    runtime.cleanup()
    assert host.starts == 1


def test_copy_registry_authority_and_verified_publication(kernel: Kernel) -> None:
    body = request(operation="host.copy", grants=[*request().grants[:-1], "host.copy"])
    with pytest.raises(Fault):
        kernel.submit(kernel.auth.validate("supervisor"), body)
    with kernel.database.engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO policies SELECT principal_id,namespace,'host.copy',mode,expires_at "
            "FROM policies WHERE operation='host.sha256'"
        )
    host = cast(Host, kernel.host)

    def observed(job: str) -> dict[str, Any]:
        return {
            "state": "finished",
            "result": "success",
            "exit_status": "0",
            "wall_ms": 10,
            "output": base64.b64encode(
                json.dumps(
                    {
                        "content_base64": base64.b64encode(host.content).decode(),
                        "bytes": len(host.content),
                    }
                ).encode()
            ).decode(),
        }

    host.status = observed  # type: ignore[method-assign]
    task = kernel.submit(kernel.auth.validate("supervisor"), body).task.task_id
    assert HostRuntime(kernel).execute(task) == "succeeded"
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        output = tx.read_object(
            tx.latest_object(ResourceRef(namespace="test", resource_id=f"{task}.output"))
        )
        assert base64.b64decode(json.loads(output)["content_base64"]) == host.content
        assert tx.get_operation(task, "execute").request.name == "host.copy"
    with TestClient(create_app(kernel)) as client:
        response = client.get(
            "/v0.2/operations?namespace=test", headers={"Authorization": "Bearer supervisor"}
        )
        assert {op["name"] for op in response.json()["installed"]} == {"host.sha256", "host.copy"}
        with kernel.database.engine.begin() as conn:
            conn.exec_driver_sql(
                "DELETE FROM policies WHERE operation='host.copy' AND mode='delegate'"
            )
        response = client.get(
            "/v0.2/operations?namespace=test", headers={"Authorization": "Bearer supervisor"}
        )
        assert "host.copy" not in response.json()["operations"]


@pytest.fixture
def helper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "spool"
    root.mkdir()
    monkeypatch.setattr(launcher, "ROOT", root)
    monkeypatch.setattr(launcher, "preflight", lambda: {})

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args, 0, "ActiveState=inactive\nLoadState=not-found\n", ""
        )

    monkeypatch.setattr(launcher, "run", run)
    return root


def start_message(job: str) -> dict[str, Any]:
    return {
        "action": "start",
        "job": job,
        "operation": "host.sha256",
        "deadline": time.time() + 30,
        "input": "e30=",
    }


def test_retirement_reclaims_capacity_but_never_reopens_job(helper: Path) -> None:
    job = "a" * 32
    launcher.handle(start_message(job))
    assert (helper / job / "input/data").exists()
    assert launcher.handle({"action": "cleanup", "job": job}) == {"cleaned": True}
    assert not (helper / job).exists()
    assert launcher.handle({"action": "cleanup", "job": job}) == {"cleaned": True}
    with pytest.raises(ValueError, match="retired_job"):
        launcher.handle(start_message(job))
    assert launcher.handle(start_message("b" * 32)) == {"started": True}


def test_cleanup_refuses_live_unit_and_symlink(
    helper: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = "a" * 32
    launcher.handle(start_message(job))
    inactive = launcher.run
    monkeypatch.setattr(
        launcher,
        "run",
        lambda args, **kw: subprocess.CompletedProcess(args, 0, "ActiveState=active\n", ""),
    )
    with pytest.raises(ValueError, match="cleanup_active"):
        launcher.handle({"action": "cleanup", "job": job})
    assert (helper / job / "input/data").exists()
    monkeypatch.setattr(launcher, "run", inactive)
    (helper / ("b" * 32)).symlink_to(helper / job, target_is_directory=True)
    with pytest.raises(ValueError, match="unsafe_job"):
        launcher.handle({"action": "cleanup", "job": "b" * 32})
    assert (helper / job / "input/data").exists()


def test_cleanup_crash_preserves_tombstone(helper: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    job = "a" * 32
    launcher.handle(start_message(job))
    original = shutil.rmtree

    def failed(path: Path) -> None:
        raise OSError("injected deletion failure")

    cast(Any, failed).avoids_symlink_attacks = True
    monkeypatch.setattr(shutil, "rmtree", failed)
    with pytest.raises(OSError):
        launcher.handle({"action": "cleanup", "job": job})
    with pytest.raises(ValueError, match="retired_job"):
        launcher.handle(start_message(job))
    monkeypatch.setattr(shutil, "rmtree", original)
    launcher.handle({"action": "cleanup", "job": job})
    assert not (helper / job).exists()


def test_monotonic_host_measurements() -> None:
    assert (
        launcher.elapsed(
            {"ExecMainStartTimestampMonotonic": "1000", "ExecMainExitTimestampMonotonic": "3501"}
        )
        == 3
    )
    assert launcher.elapsed({}) is None
    assert (
        launcher.elapsed(
            {"ExecMainStartTimestampMonotonic": "1000", "ExecMainExitTimestampMonotonic": "0"}
        )
        is None
    )


def test_unsupported_host_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    with pytest.raises(ValueError, match="unsupported_host"):
        launcher.preflight()


def test_m1_restore_migrates_charges_without_rewriting_receipt(
    kernel: Kernel, tmp_path: Path
) -> None:
    task = admit(kernel)
    HostRuntime(kernel).execute(task)
    with kernel.database.engine.begin() as conn:
        before = conn.exec_driver_sql(
            "SELECT snapshot FROM operations WHERE task_id=?", (task,)
        ).scalar_one()
        for table in (
            "recovery_epochs",
            "trigger_events",
            "triggers",
            "effect_scopes",
            "delegation_links",
            "agent_effects",
            "agent_runs",
            "agent_inputs",
            "model_budget_limits",
        ):
            conn.exec_driver_sql(f"DROP TABLE {table}")
        conn.exec_driver_sql("DROP TABLE host_reservations")
        conn.exec_driver_sql("DROP TABLE host_budget_limits")
        conn.exec_driver_sql("UPDATE cardea_alembic_version SET version_num='m1_001'")
    snapshot = tmp_path / "old.sqlite3"
    kernel.database.backup(snapshot)
    target = tmp_path / "restored"
    restore(target, snapshot)
    assert initialize(target) is None
    upgraded = Database(target)
    try:
        upgraded.check_version()
        with upgraded.engine.begin() as conn:
            assert (
                conn.exec_driver_sql(
                    "SELECT snapshot FROM operations WHERE task_id=?", (task,)
                ).scalar_one()
                == before
            )
            assert conn.exec_driver_sql(
                "SELECT tool_ms,output_bytes,settled FROM host_reservations WHERE task_id=?",
                (task,),
            ).one() == (10000, 65536, 1)
            assert conn.exec_driver_sql("SELECT COUNT(*) FROM host_budget_limits").scalar_one() == 0
    finally:
        upgraded.close()


def test_failed_m2_migration_is_atomic(kernel: Kernel) -> None:
    import sqlalchemy as sa

    with kernel.database.engine.begin() as conn:
        for table in (
            "recovery_epochs",
            "trigger_events",
            "triggers",
            "effect_scopes",
            "delegation_links",
            "agent_effects",
            "agent_runs",
            "agent_inputs",
            "model_budget_limits",
        ):
            conn.exec_driver_sql(f"DROP TABLE {table}")
        conn.exec_driver_sql("DROP TABLE host_reservations")
        conn.exec_driver_sql("DROP TABLE host_budget_limits")
        conn.exec_driver_sql("UPDATE cardea_alembic_version SET version_num='m1_001'")

    def fail(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool
    ) -> None:
        if "CREATE TABLE host_reservations" in statement:
            raise RuntimeError("interrupted migration")

    sa.event.listen(kernel.database.engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError):
            kernel.database.migrate()
    finally:
        sa.event.remove(kernel.database.engine, "before_cursor_execute", fail)
    with kernel.database.engine.begin() as conn:
        assert (
            conn.exec_driver_sql("SELECT version_num FROM cardea_alembic_version").scalar_one()
            == "m1_001"
        )
        assert (
            conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE name='host_budget_limits'"
            ).first()
            is None
        )
    kernel.database.migrate()
    kernel.database.check_version()


def test_retention_cannot_break_capacity_bound(helper: Path) -> None:
    for i in range(256):
        (helper / f"{i:032x}").mkdir()
    job = "f" * 32
    with pytest.raises(ValueError, match="spool_capacity"):
        launcher.handle(start_message(job))
    launcher.handle({"action": "cleanup", "job": "0" * 32})
    assert launcher.handle(start_message(job)) == {"started": True}


def test_cleanup_does_not_follow_nested_symlink(helper: Path, tmp_path: Path) -> None:
    job = "a" * 32
    launcher.handle(start_message(job))
    secret = tmp_path / "secret"
    secret.write_bytes(b"preserve")
    (helper / job / "link").symlink_to(secret)
    launcher.handle({"action": "cleanup", "job": job})
    assert secret.read_bytes() == b"preserve"
