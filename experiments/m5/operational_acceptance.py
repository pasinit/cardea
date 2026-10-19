"""Explicit phases against installed packages on the disposable Debian acceptance host."""

import argparse
import hashlib
import json
import os
import pwd
import shutil
import sqlite3
import time
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from cardea.service import admin
from cardea.service.client import ClientError, KernelClient
from cardea.service.database import SCHEMA_HEAD, Database, Fault
from cardea.service.kernel import provision
from cardea.service.models import (
    RIGHTS,
    DelegationTaskRequest,
    HostBudget,
    HostTaskRequest,
    Permission,
    Provisioning,
    ServiceConfig,
    Signal,
    TaskRequest,
    now,
)
from installed_acceptance import CONFIG, OBSERVATIONS, SUPERVISOR, wait

RESULT = OBSERVATIONS.parent / "completion.json"
SNAPSHOT = OBSERVATIONS.parent / "completion-snapshot.sqlite3"


def policy(namespace: str = "test") -> None:
    original = Provisioning.model_validate_json(
        (OBSERVATIONS.parent / "policy.json").read_bytes()
    )
    permissions = tuple(
        Permission(
            principal_id="supervisor",
            namespace=namespace,
            operation=right,
            delegate=True,
            expires_at=now() + timedelta(hours=8),
        )
        for right in (*RIGHTS, "tasks.delegate", "host.sha256", "host.copy")
    )
    document = original.model_copy(
        update={
            "permissions": permissions,
            "platform": tuple(
                p.model_copy(update={"principal_id": "platform"}) for p in permissions
            ),
            "host_budgets": (
                HostBudget(
                    principal_id="supervisor",
                    namespace=namespace,
                    tool_milliseconds=1000000,
                    output_bytes=10000000,
                ),
            ),
        }
    )
    database = Database(Path("/var/lib/cardea"))
    try:
        provision(database, document)
    finally:
        database.close()


def receipts() -> str:
    database = Database(Path("/var/lib/cardea"))
    try:
        with database.read() as conn:
            values = [
                r[0]
                for r in conn.exec_driver_sql(
                    "SELECT snapshot FROM operations ORDER BY receipt_id"
                )
            ]
        return hashlib.sha256(json.dumps(values).encode()).hexdigest()
    finally:
        database.close()


def run(phase: str) -> None:
    if phase != "baseline":
        from cardea.service.maintenance import diagnostics, prune_backups, verify

    assert os.getuid() == pwd.getpwnam("cardea-kernel").pw_uid
    config = ServiceConfig.model_validate_json(CONFIG.read_bytes())
    result = json.loads(RESULT.read_text()) if RESULT.exists() else {}
    client = KernelClient.for_workload(
        config.data_dir / "kernel.sock",
        config.spiffe_socket,
        SUPERVISOR,
        config.audience,
    )

    def state(task: str, expected: str) -> None:
        assert client.inspect(task).task.state.value == expected

    try:
        if phase == "baseline":
            policy()
            wait(lambda: client.request("GET", "/system"), 180)
            old = json.loads(OBSERVATIONS.read_text())
            assert (
                client.request("GET", f"/tasks/{old['task_id']}/receipt")
                == old["receipt"]
            )
            result["prior_receipts_digest"] = receipts()
            result["prior_task"] = old["task_id"]
        elif phase == "upgraded":
            assert receipts() == result["prior_receipts_digest"]
            assert verify(config.data_dir)["schema"] == SCHEMA_HEAD
            assert admin.initialize(config.data_dir) is None
            result["upgrade_preserved_receipts"] = True
        elif phase == "before-reboot":
            wait(lambda: client.request("GET", "/system"), 180)
            task = client.submit(
                TaskRequest(
                    request_id="m5-durable-wait",
                    agent_id="agent-a",
                    namespace="test",
                    objective="Wait across full-machine reboot",
                    payload={"m5": "wait"},
                    wait_for_event=True,
                    timeout_seconds=3600,
                )
            )
            result["wait_task"] = task.task.task_id
            wait(lambda: state(result["wait_task"], "waiting"), 180)
            result["wait_receipt"] = client.request(
                "GET", f"/tasks/{result['wait_task']}/receipt"
            )
            host = HostTaskRequest(
                request_id="m5-interrupted-host",
                agent_id="agent-a",
                namespace="test",
                objective="Interrupt after host start before settlement",
                payload={"m5": "interrupt"},
                operation="host.sha256",
                grants=(*RIGHTS, "host.sha256"),
                timeout_seconds=3600,
            )
            result["host_task"] = client.submit_host(host).task.task_id
            wait(lambda: (config.data_dir / "m5-host-started").read_text(), 180)
            state(result["host_task"], "running")
            result["boot_before"] = (
                Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            )
        elif phase == "after-reboot":
            wait(lambda: client.request("GET", "/system"), 180)
            result["boot_after"] = (
                Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            )
            assert result["boot_after"] != result["boot_before"]
            wait(lambda: state(result["host_task"], "failed"), 180)
            receipt = client.request("GET", f"/tasks/{result['host_task']}/receipt")
            assert receipt["receipt"]["state"] == "outcome_unknown"
            assert receipt["receipt"]["result"] is None
            result["interrupted_receipt"] = receipt
            state(result["wait_task"], "waiting")
            assert (
                client.request("GET", f"/tasks/{result['wait_task']}/receipt")
                == result["wait_receipt"]
            )
            client.control(result["wait_task"], Signal(request_id="m5-after-boot-wake"))
            wait(lambda: state(result["wait_task"], "succeeded"), 180)
            result["wait_resumed_after_machine_reboot"] = True
            result["online_diagnostics"] = diagnostics(config)
            assert result["online_diagnostics"]["ready"]
        elif phase == "snapshot":
            admin.backup(config.data_dir, SNAPSHOT)
            result["snapshot_receipts_digest"] = receipts()
            result["snapshot_verified"] = verify(config.data_dir)
        elif phase == "after-snapshot":
            wait(lambda: client.request("GET", "/system"), 180)
            host = HostTaskRequest(
                request_id="m5-after-snapshot-spend",
                agent_id="agent-a",
                namespace="test",
                objective="Spend after the snapshot",
                payload={"snapshot": "later"},
                operation="host.sha256",
                grants=(*RIGHTS, "host.sha256"),
            )
            task = client.submit_host(host).task.task_id
            wait(lambda: state(task, "succeeded"), 180)
            database = Database(config.data_dir)
            try:
                with database.engine.begin() as conn:
                    conn.exec_driver_sql(
                        "DELETE FROM policies WHERE principal_id='supervisor'"
                    )
            finally:
                database.close()
            try:
                client.submit_host(
                    host.model_copy(update={"request_id": "m5-revoked-before-restore"})
                )
            except ClientError as error:
                assert error.status == 403
            else:
                raise AssertionError("Post-snapshot revocation was bypassed")
            result["post_snapshot_task"] = task
        elif phase == "restore":
            result["recovery"] = admin.restore(config.data_dir, SNAPSHOT)
            assert receipts() == result["snapshot_receipts_digest"]
            database = Database(config.data_dir)
            try:
                with database.read() as conn:
                    assert (
                        conn.exec_driver_sql(
                            "SELECT count(*) FROM principals WHERE disabled=0"
                        ).scalar_one()
                        == 0
                    )
                    assert (
                        conn.exec_driver_sql(
                            "SELECT count(*) FROM policies"
                        ).scalar_one()
                        == 0
                    )
            finally:
                database.close()
            result["restore_verified"] = verify(config.data_dir)
            result["offline_diagnostics"] = diagnostics(config)
            assert not result["offline_diagnostics"]["ready"]
            # Re-provisioning is explicitly outside restoration; old scopes still sealed.
            policy()
        elif phase == "restored-work":
            wait(lambda: client.request("GET", "/system"), 180)
            description = client.request("GET", "/system")
            assert (
                description["new_namespace_prefix"]
                == result["recovery"]["new_namespace_prefix"]
            )
            old = json.loads(OBSERVATIONS.read_text())
            assert (
                client.request("GET", f"/tasks/{old['task_id']}/output")
                == old["output"]
            )
            assert (
                client.request("GET", f"/tasks/{old['task_id']}/children")
                == old["children"]
            )
            assert (
                client.request("GET", f"/tasks/{old['task_id']}/receipt")
                == old["receipt"]
            )
            stale = TaskRequest(
                request_id="m5-stale-scope",
                agent_id="agent-a",
                namespace="test",
                objective="Must refuse restored spending",
                payload={},
            )
            try:
                client.submit(stale)
            except ClientError as error:
                assert error.code == "restored_scope_sealed"
            else:
                raise AssertionError("Historical namespace was reopened")
            namespace = result["recovery"]["new_namespace_prefix"] + "acceptance"
            policy(namespace)
            delegation = DelegationTaskRequest.model_validate(
                {
                    "request_id": "m5-fresh-recovery-delegation",
                    "agent_id": "agent-a",
                    "namespace": namespace,
                    "objective": "Execute with fresh authority and budget after restore",
                    "grants": [*RIGHTS, "tasks.delegate", "host.sha256", "host.copy"],
                    "children": [
                        {
                            "agent_id": "agent-b",
                            "operation": "host.sha256",
                            "payload": {"fresh": 1},
                        },
                        {
                            "agent_id": "agent-b",
                            "operation": "host.copy",
                            "payload": {"fresh": 2},
                        },
                    ],
                }
            )
            task = client.submit_delegation(delegation).task.task_id
            wait(lambda: state(task, "succeeded"), 180)
            result["fresh_delegation"] = client.request("GET", f"/tasks/{task}/output")
            result["restored_old_scope_denied"] = True
            result["fresh_scope_verified"] = True
        elif phase == "maintenance":
            before = receipts()
            generated = []
            for _ in range(4):
                path = config.data_dir / f"before-migration-{uuid4().hex}.sqlite3"
                admin.backup(config.data_dir, path)
                timestamp = time.time() - 10 * 86400
                os.utime(path, (timestamp, timestamp))
                generated.append(path.name)
            preview = prune_backups(config.data_dir, keep=2, days=7)
            assert preview["eligible"] >= 2 and set(preview["backups"]) <= set(
                generated
            )
            applied = prune_backups(config.data_dir, keep=2, days=7, apply=True)
            assert preview["backups"] == applied["backups"]
            assert receipts() == before and SNAPSHOT.exists()
            result["retention"] = applied
            damaged = OBSERVATIONS.parent / "damaged.sqlite3"
            shutil.copyfile(SNAPSHOT, damaged)
            damaged.chmod(0o600)
            connection = sqlite3.connect(damaged)
            try:
                for (trigger,) in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                ).fetchall():
                    connection.execute(f'DROP TRIGGER "{trigger}"')
                connection.execute("UPDATE objects SET content=x'01'")
                connection.commit()
            finally:
                connection.close()
            target = OBSERVATIONS.parent / "fault-recovery"
            try:
                admin.restore(target, damaged)
            except Fault as error:
                assert error.code == "artifact_corrupt"
            else:
                raise AssertionError("Damaged snapshot accepted")
            assert not (target / "kernel.sqlite3").exists()
            for stage in ("marked", "published"):

                def crash(point: str, barrier: str = stage) -> None:
                    if point == barrier:
                        raise RuntimeError("fixture interruption")

                try:
                    admin.restore(target, SNAPSHOT, fault=crash)
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("Restore barrier not reached")
                try:
                    Database(target)
                except Fault as error:
                    assert error.code == "restore_incomplete"
                else:
                    raise AssertionError("Interrupted restore allowed startup")
                admin.restore(target, SNAPSHOT)
                assert verify(target)["integrity"] == "ok"
            result["installed_corruption_and_restore_faults"] = True
        else:
            raise ValueError("Unknown phase")
        RESULT.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps({"phase": phase, "passed": True}))
    finally:
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase")
    assert Path("/etc/cardea-disposable-m2-vm").read_text().strip() == "yes"
    run(parser.parse_args().phase)
