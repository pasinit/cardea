"""Historical state cannot mint authority, reusable capacity or a second host effect."""

import json
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest
from test_execution import Host, admit
from test_execution import kernel as kernel

from cardea.service import admin
from cardea.service.database import Database, Fault, borrow
from cardea.service.host_runtime import HostRuntime
from cardea.service.kernel import Kernel, provision
from cardea.service.maintenance import verify
from cardea.service.models import (
    RIGHTS,
    HostBudget,
    Permission,
    Principal,
    Provisioning,
    Signal,
    now,
)
from cardea.service.policy import Policy


def fresh(database: Database, namespace: str, *, budget: bool = True) -> None:
    with database.read() as conn:
        principals = tuple(
            Principal.model_validate(dict(row) | {"disabled": False})
            for row in conn.exec_driver_sql("SELECT * FROM principals").mappings()
        )
    permissions = tuple(
        Permission(
            principal_id="supervisor",
            namespace=namespace,
            operation=right,
            delegate=True,
            expires_at=now() + timedelta(hours=1),
        )
        for right in (*RIGHTS, "host.sha256")
    )
    provision(
        database,
        Provisioning(
            principals=principals,
            permissions=permissions,
            platform=tuple(p.model_copy(update={"principal_id": "platform"}) for p in permissions),
            host_budgets=(
                HostBudget(
                    principal_id="supervisor",
                    namespace=namespace,
                    tool_milliseconds=100000,
                    output_bytes=1000000,
                ),
            )
            if budget
            else (),
        ),
    )


def test_restored_authority_and_capacity_are_sealed(kernel: Kernel, tmp_path: Path) -> None:
    task = admit(kernel)
    HostRuntime(kernel).execute(task)
    with kernel.database.read() as conn:
        receipt = conn.exec_driver_sql(
            "SELECT snapshot FROM operations WHERE task_id=?", (task,)
        ).scalar_one()
    snapshot = tmp_path / "snapshot.sqlite3"
    kernel.database.backup(snapshot)
    # Later revocation and spending are absent from the backup.
    with kernel.database.engine.begin() as conn:
        conn.exec_driver_sql("UPDATE principals SET disabled=1 WHERE principal_id='supervisor'")
        conn.exec_driver_sql("UPDATE host_budget_limits SET tool_ms=0")
    kernel.database.close()
    result = admin.restore(kernel.database.directory, snapshot)
    namespace = result["new_namespace_prefix"] + "new"
    kernel.database = Database(kernel.database.directory)
    with pytest.raises(Fault) as denied:
        kernel.inspect(kernel.auth.validate("supervisor"), task)
    assert denied.value.code == "principal_disabled"
    fresh(kernel.database, "test")
    assert kernel.inspect(kernel.auth.validate("supervisor"), task).task.state.value == "succeeded"
    with kernel.database.read() as conn:
        assert (
            conn.exec_driver_sql(
                "SELECT snapshot FROM operations WHERE task_id=?", (task,)
            ).scalar_one()
            == receipt
        )
    for action in (
        lambda: admit(kernel, request_id="new-old"),
        lambda: kernel.control(
            kernel.auth.validate("supervisor"), task, Signal(request_id="resume"), "resume"
        ),
    ):
        with pytest.raises(Fault) as denied:
            action()
        assert denied.value.code == "restored_scope_sealed"
    # Even a scope missing from the snapshot might have spent capacity later.
    fresh(kernel.database, "absent-from-snapshot")
    with pytest.raises(Fault, match="recovery namespace"):
        admit(kernel, namespace="absent-from-snapshot", request_id="absent")
    fresh(kernel.database, namespace, budget=False)
    with pytest.raises(Fault) as denied:
        admit(kernel, namespace=namespace, request_id="no-budget")
    assert denied.value.code == "host_budget_missing"
    fresh(kernel.database, namespace)
    new = admit(kernel, namespace=namespace, request_id="new-epoch")
    assert HostRuntime(kernel).execute(new) == "succeeded"
    assert cast(Host, kernel.host).starts == 2
    verify(kernel.database.directory)


def test_restore_seals_queued_and_expired_effect(
    kernel: Kernel, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queued = admit(kernel, request_id="queued")
    running = admit(kernel, request_id="running")

    def interrupt(stage: str) -> None:
        if stage == "host.started":
            raise SystemExit("lost host worker")

    kernel.fault = interrupt
    with pytest.raises(SystemExit):
        HostRuntime(kernel).execute(running)
    snapshot = tmp_path / "running.sqlite3"
    kernel.database.backup(snapshot)
    with monkeypatch.context() as clock:
        past = now() - timedelta(hours=1)
        clock.setattr(admin, "now", lambda: past)
        with pytest.raises(Fault) as regressed:
            admin.restore(tmp_path / "regressed", snapshot)
        assert regressed.value.code == "recovery_clock_regressed"
    with pytest.raises(Fault) as live:
        admin.restore(tmp_path / "live", snapshot)
    assert live.value.code == "recovery_lease_live"
    # A trusted injected clock tests expired recovery without waiting 40 seconds.
    future = now() + timedelta(seconds=41)
    monkeypatch.setattr(admin, "now", lambda: future)
    admin.restore(tmp_path / "recovered", snapshot)
    database = Database(tmp_path / "recovered")
    try:
        with database.read() as conn, borrow(conn) as tx:
            assert tx.get_task(queued).state.value == "failed"
            assert tx.get_task(running).state.value == "failed"
            receipt = conn.exec_driver_sql(
                "SELECT state FROM operations WHERE task_id=?", (running,)
            ).scalar_one()
            assert receipt == "outcome_unknown"
            assert Policy(conn).recovery_prefix
    finally:
        database.close()
    assert cast(Host, kernel.host).starts == 1


@pytest.mark.parametrize("corruption", ["digest", "missing", "reference"])
def test_restore_rejects_artifact_damage(kernel: Kernel, tmp_path: Path, corruption: str) -> None:
    task = admit(kernel)
    HostRuntime(kernel).execute(task)
    snapshot = tmp_path / "damaged.sqlite3"
    kernel.database.backup(snapshot)
    with sqlite3.connect(snapshot) as conn:
        # Host-owner corruption, outside the append-only API.
        for (trigger,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        ).fetchall():
            conn.execute(f'DROP TRIGGER "{trigger}"')
        if corruption == "digest":
            conn.execute("UPDATE objects SET content=x'01'")
        elif corruption == "missing":
            conn.execute("DELETE FROM objects")
        else:
            row = conn.execute("SELECT snapshot FROM tasks WHERE task_id=?", (task,)).fetchone()
            record = json.loads(row[0])
            record["verification"]["artifacts"][0]["version"] += 1
            conn.execute("UPDATE tasks SET snapshot=? WHERE task_id=?", (json.dumps(record), task))
    with pytest.raises(Fault) as invalid:
        admin.restore(tmp_path / "target", snapshot)
    assert invalid.value.code in {"artifact_corrupt", "artifact_reference_invalid"}
    assert not (tmp_path / "target" / "kernel.sqlite3").exists()
