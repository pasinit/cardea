"""Real SPIRE, native A2A over Unix HTTP, DBOS and confined Debian host workers."""

import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from cardea.service.database import Database, borrow
from cardea.service.models import (
    RIGHTS,
    DelegationTaskRequest,
    HostBudget,
    Permission,
    Principal,
    Signal,
    now,
)
from harness import ROOT, SUPERVISOR, ServiceLab, wait_for
from provider_lab import Lab, eventually


@pytest.fixture(scope="module")
def provider():
    lab = Lab()
    try:
        lab.start()
        lab.register(SUPERVISOR)
        eventually(lambda: lab.token(SUPERVISOR))
        yield lab
    finally:
        lab.close()


@pytest.fixture
def service(provider):
    lab = ServiceLab(provider, host_execution=True)
    extra = tuple(
        Permission(
            principal_id="supervisor",
            namespace="test",
            operation=r,
            delegate=True,
            expires_at=now() + timedelta(hours=1),
        )
        for r in ("tasks.delegate", "host.sha256", "host.copy")
    )
    lab.document = lab.document.model_copy(
        update={
            "principals": (
                *lab.document.principals,
                Principal(principal_id="agent-b", kind="agent", runtime_id="runtime-a"),
            ),
            "permissions": (*lab.document.permissions, *extra),
            "platform": (
                *lab.document.platform,
                *(p.model_copy(update={"principal_id": "platform"}) for p in extra),
            ),
            "host_budgets": (
                HostBudget(
                    principal_id="supervisor",
                    namespace="test",
                    tool_milliseconds=1000000,
                    output_bytes=10000000,
                ),
            ),
        }
    )
    lab.apply(lab.document)
    try:
        yield lab
    finally:
        lab.close()


def submit(service, request_id="delegation", **changes):
    request = DelegationTaskRequest.model_validate(
        {
            "request_id": request_id,
            "agent_id": "agent-a",
            "namespace": "test",
            "objective": "Verify the delegated input hash and copy",
            "grants": [*RIGHTS, "tasks.delegate", "host.sha256", "host.copy"],
            "children": [
                {
                    "agent_id": "agent-b",
                    "operation": "host.sha256",
                    "payload": {"one": 1},
                },
                {
                    "agent_id": "agent-b",
                    "operation": "host.copy",
                    "payload": {"two": 2},
                },
            ],
            **changes,
        }
    )
    return service.client.request(
        "POST", "/delegation-tasks", json=request.model_dump(mode="json")
    )["task"]["task_id"]


def entered(service, process, stage):
    path = service.data / f"{process.pid}.{stage}.entered"

    def reached():
        assert path.exists(), service.logs()

    wait_for(reached, 90)


def release(service, process, stage):
    (service.data / f"{process.pid}.{stage}.release").touch()


def evidence(service, parent, label):
    database = Database(service.data)
    try:
        with database.engine.begin() as conn, borrow(conn) as tx:
            links = list(
                conn.exec_driver_sql(
                    "SELECT slot,child_id,message_id FROM delegation_links "
                    "WHERE parent_id=? ORDER BY slot",
                    (parent,),
                )
            )
            data = {
                "case": label,
                "parent": tx.get_task(parent).model_dump(mode="json"),
                "children": [tx.get_task(r[1]).model_dump(mode="json") for r in links],
                "links": [list(r) for r in links],
                "receipts": [
                    tx.get_operation(r[1], "execute").model_dump(mode="json")
                    for r in links
                    if conn.exec_driver_sql(
                        "SELECT 1 FROM operations WHERE task_id=?", (r[1],)
                    ).first()
                ],
            }
        location = os.environ.get("M4_RUN_EVIDENCE")
        if location:
            path = Path(location)
            results = json.loads(path.read_text()) if path.exists() else []
            results.append(data)
            path.write_text(json.dumps(results, indent=2))
        return data
    finally:
        database.close()


def test_native_success(service):
    service.start()
    parent = submit(service, minimum_success=2)
    service.wait_state(parent, "succeeded", timeout=120)
    result = service.client.request("GET", f"/tasks/{parent}/output")
    assert result["content"]["verified_children"] == 2
    data = evidence(service, parent, "native-success")
    assert len(data["receipts"]) == 2
    assert all(r["state"] == "succeeded" for r in data["receipts"])


@pytest.mark.parametrize("stage", ["delegation.child_admitted", "delegation.before_verify"])
def test_restart_reconciles_same_children(service, stage):
    process = service.start(stage)
    parent = submit(service)
    entered(service, process, stage)
    service.stop(process)
    service.start()
    service.wait_state(parent, "succeeded", timeout=150)
    data = evidence(service, parent, stage)
    assert len(data["children"]) == 2
    assert len(data["receipts"]) == 2
    assert len({r["request"]["task_id"] for r in data["receipts"]}) == 2
    assert any(r["state"] == "succeeded" for r in data["receipts"])


def test_revocation_before_peer_admission(service):
    stage = "delegation.before_peer"
    process = service.start(stage)
    parent = submit(service)
    entered(service, process, stage)
    changed = service.document.model_copy(
        update={
            "permissions": tuple(
                p
                for p in service.document.permissions
                if not (p.principal_id == "supervisor" and p.operation == "tasks.delegate")
            )
        }
    )
    service.apply(changed)
    release(service, process, stage)
    service.wait_state(parent, "failed", timeout=90)
    assert evidence(service, parent, "revoked")["children"] == []


def test_parent_cancel_stops_workers(service):
    stage = "host.started"
    process = service.start(stage)
    parent = submit(service)
    entered(service, process, stage)
    service.client.control(parent, Signal(request_id="cancel-parent"), cancel=True)
    release(service, process, stage)
    service.wait_state(parent, "cancelled", timeout=90)
    data = evidence(service, parent, "cancel")
    assert all(t["state"] in {"cancelled", "failed"} for t in data["children"])


def test_shared_budget_denies_extra_child(service):
    service.start()
    parent = submit(service, tool_milliseconds=10000)
    service.wait_state(parent, "failed", timeout=90)
    data = evidence(service, parent, "budget")
    assert len(data["children"]) == 1


def test_failed_child_does_not_create_false_success(service):
    env = {
        k: v for k, v in os.environ.items() if not k.startswith(("DBOS_", "OTEL_", "M1_", "M4_"))
    }
    env.update(
        PYTHONPATH=str(ROOT / "apps/api/src"),
        M1_CONFIG=str(service.config_path),
        M4_CORRUPT_HASH="1",
    )
    with (service.root / "daemon-failure.log").open("wb") as log:
        process = subprocess.Popen(
            [sys.executable, str(ROOT / "experiments/m4/daemon_probe.py")],
            env=env,
            cwd=service.root,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    service.processes.append(process)
    wait_for(lambda: service.client.request("GET", "/system"), 60)
    parent = submit(service, minimum_success=1)
    service.wait_state(parent, "succeeded", timeout=120)
    data = evidence(service, parent, "child-verification-failure")
    assert [t["state"] for t in data["children"]] == ["failed", "succeeded"]
    assert data["receipts"][0]["failure"]["code"] == "verification_failed"
    output = service.client.request("GET", f"/tasks/{parent}/output")
    assert output["content"]["verified_children"] == 1
