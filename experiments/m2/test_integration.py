"""Real SPIRE, unprivileged daemon, sudo helper and Debian worker acceptance."""

import base64
import hashlib
import json
import os
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest
import rfc8785
from cardea.kernel.models import ObjectVersion
from cardea.service.client import ClientError
from cardea.service.database import Database, borrow
from cardea.service.models import (
    RIGHTS,
    HostBudget,
    HostObjectTaskRequest,
    HostTaskRequest,
    ObjectPermission,
    Permission,
    Signal,
    TaskRequest,
    now,
)
from harness import SUPERVISOR, ServiceLab, wait_for
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
    extra = Permission(
        principal_id="supervisor",
        namespace="test",
        operation="host.sha256",
        delegate=True,
        expires_at=now() + timedelta(hours=1),
    )
    lab.document = lab.document.model_copy(
        update={
            "host_budgets": (
                HostBudget(
                    principal_id="supervisor",
                    namespace="test",
                    tool_milliseconds=1000000,
                    output_bytes=10000000,
                ),
            ),
            "permissions": (*lab.document.permissions, extra),
            "platform": (
                *lab.document.platform,
                extra.model_copy(update={"principal_id": "platform"}),
            ),
        }
    )
    lab.apply(lab.document)
    try:
        yield lab
    finally:
        lab.close()


def submit(service, **changes):
    request = HostTaskRequest.model_validate(
        {
            "request_id": "host-one",
            "agent_id": "agent-a",
            "namespace": "test",
            "objective": "Verify hash of declared input",
            "payload": {"hello": "Debian"},
            "operation": "host.sha256",
            "grants": [*RIGHTS, "host.sha256"],
            **changes,
        }
    )
    return service.client.request("POST", "/host-tasks", json=request.model_dump(mode="json"))[
        "task"
    ]["task_id"]


def raw_task(service, task_id):
    db = Database(service.data)
    try:
        with db.engine.begin() as conn, borrow(conn) as tx:
            return tx.get_task(task_id)
    finally:
        db.close()


def test_authenticated_real_worker(service):
    service.start()
    task_id = submit(service)
    assert submit(service) == task_id
    service.wait_state(task_id, "succeeded", timeout=40)
    output = service.client.request("GET", f"/tasks/{task_id}/output")["content"]
    source = rfc8785.dumps({"hello": "Debian"})
    assert output == {
        "sha256": hashlib.sha256(source).hexdigest(),
        "bytes": len(source),
    }
    receipt = service.client.request("GET", f"/tasks/{task_id}/receipt")["receipt"]
    assert receipt["state"] == "succeeded" and 0 <= receipt["usage"]["tool_milliseconds"] <= 10000
    assert raw_task(service, task_id).verification.passed
    with pytest.raises(ClientError):
        submit(service, payload={"changed": True})


@pytest.mark.parametrize("point", ["host.intent_committed", "host.started", "host.before_settle"])
def test_killed_daemon_preserves_unknown_without_relaunch(service, point):
    process = service.start(point)
    task_id = submit(service)
    wait_for(lambda: assert_exists(service.data / f"{process.pid}.{point}.entered"), 30)
    service.stop(process)
    service.start()
    service.wait_state(task_id, "failed", timeout=55)
    receipt = service.client.request("GET", f"/tasks/{task_id}/receipt")["receipt"]
    assert receipt["state"] == "outcome_unknown"
    assert raw_task(service, task_id).verification is None


def assert_exists(path):
    assert path.exists()


def test_revoked_after_intent_never_starts(service):
    process = service.start("host.intent_committed")
    task_id = submit(service)
    wait_for(
        lambda: assert_exists(service.data / f"{process.pid}.host.intent_committed.entered"),
        30,
    )
    service.apply(
        service.document.model_copy(
            update={
                "permissions": tuple(
                    p for p in service.document.permissions if p.operation != "host.sha256"
                )
            }
        )
    )
    (service.data / f"{process.pid}.host.intent_committed.release").touch()
    wait_for(lambda: assert_failed(service, task_id), 30)
    assert not Path("/var/lib/cardea-host", task_id.removeprefix("task-")).exists()


def assert_failed(service, task_id):
    assert raw_task(service, task_id).state.value == "failed"


def test_cancel_after_intent(service):
    process = service.start("host.intent_committed")
    task_id = submit(service)
    wait_for(
        lambda: assert_exists(service.data / f"{process.pid}.host.intent_committed.entered"),
        30,
    )
    service.client.control(task_id, Signal(request_id="cancel-host"), cancel=True)
    (service.data / f"{process.pid}.host.intent_committed.release").touch()
    service.wait_state(task_id, "cancelled", timeout=30)


def test_fixed_sudo_command_scope():
    denied = subprocess.run(["sudo", "-n", "/usr/bin/id"], capture_output=True)
    assert denied.returncode != 0
    denied = subprocess.run(
        ["sudo", "-n", "/usr/local/libexec/cardea-host", "extra"], capture_output=True
    )
    assert denied.returncode != 0
    invalid = subprocess.run(
        ["sudo", "-n", "/usr/local/libexec/cardea-host"],
        input=json.dumps(
            {"action": "start", "job": "../escape", "operation": "shell", "input": ""}
        ),
        text=True,
        capture_output=True,
    )
    assert invalid.returncode != 0
    expired = subprocess.run(
        ["sudo", "-n", "/usr/local/libexec/cardea-host"],
        input=json.dumps(
            {
                "action": "start",
                "job": "f" * 32,
                "operation": "host.sha256",
                "input": "e30=",
                "deadline": 0,
            }
        ),
        text=True,
        capture_output=True,
    )
    assert expired.returncode != 0
    assert not Path("/var/lib/cardea-host", "f" * 32).exists()
    assert os.getuid() != 0


def stored_input(service):
    # Produce the source through the authenticated kernel API, not direct storage writes.
    original = service.client.submit(
        TaskRequest(
            request_id="stored-input",
            agent_id="agent-a",
            namespace="test",
            objective="Store declared input",
            payload={"stored": "Debian object"},
        )
    ).task.task_id
    service.wait_state(original, "succeeded", timeout=40)
    output = service.client.request("GET", f"/tasks/{original}/output")
    reference = ObjectVersion.model_validate(output["artifact"])
    permission = ObjectPermission(
        principal_id="supervisor",
        object=reference,
        delegate=True,
        expires_at=now() + timedelta(hours=1),
    )
    service.document = service.document.model_copy(
        update={
            "object_permissions": (permission,),
            "object_platform": (permission.model_copy(update={"principal_id": "platform"}),),
        }
    )
    service.apply(service.document)
    return reference


def submit_object(service, reference, **changes):
    request = HostObjectTaskRequest.model_validate(
        {
            "request_id": "stored-hash",
            "agent_id": "agent-a",
            "namespace": "test",
            "objective": "Hash only the granted object version",
            "input_object": reference.model_dump(),
            "operation": "host.sha256",
            "grants": [*RIGHTS, "host.sha256"],
            **changes,
        }
    )
    return service.client.submit_host_object(request).task.task_id


def test_authenticated_stored_object_worker(service):
    service.start()
    reference = stored_input(service)
    task_id = submit_object(service, reference)
    assert submit_object(service, reference) == task_id
    service.wait_state(task_id, "succeeded", timeout=40)
    output = service.client.request("GET", f"/tasks/{task_id}/output")["content"]
    assert output["sha256"] == reference.content_digest
    receipt = service.client.request("GET", f"/tasks/{task_id}/receipt")["receipt"]
    assert receipt["request"]["arguments"]["input_object"] == reference.model_dump(mode="json")
    with pytest.raises(ClientError) as denied:
        submit_object(
            service,
            reference.model_copy(update={"resource_id": "ungranted"}),
            request_id="other-object",
        )
    assert denied.value.code == "authority_denied"
    with pytest.raises(ClientError) as denied:
        submit_object(
            service,
            reference.model_copy(update={"version": reference.version + 1}),
            request_id="other-version",
        )
    assert denied.value.code == "authority_denied"


@pytest.mark.parametrize("point", ["host.intent_committed", "host.before_settle"])
@pytest.mark.parametrize("ceiling", ["allow", "delegate", "platform"])
def test_live_object_ceiling_on_debian(service, point, ceiling):
    process = service.start(point)
    reference = stored_input(service)
    task_id = submit_object(service, reference)
    wait_for(lambda: assert_exists(service.data / f"{process.pid}.{point}.entered"), 30)
    change: dict[str, tuple[ObjectPermission, ...]]
    if ceiling == "delegate":
        change = {
            "object_permissions": (
                service.document.object_permissions[0].model_copy(update={"delegate": False}),
            )
        }
    else:
        change = {"object_platform" if ceiling == "platform" else "object_permissions": ()}
    service.apply(service.document.model_copy(update=change))
    (service.data / f"{process.pid}.{point}.release").touch()
    service.wait_state(task_id, "failed", timeout=40)
    assert raw_task(service, task_id).verification is None
    receipt = service.client.request("GET", f"/tasks/{task_id}/receipt")["receipt"]
    assert receipt["state"] == "failed"
    assert receipt["failure"]["code"] == "authority_denied"
    if point == "host.intent_committed":
        assert not Path("/var/lib/cardea-host", task_id.removeprefix("task-")).exists()
    with pytest.raises(ClientError):
        service.client.request("GET", f"/tasks/{task_id}/output")


def test_stored_object_daemon_crash_never_relaunches(service):
    process = service.start("host.started")
    reference = stored_input(service)
    task_id = submit_object(service, reference)
    wait_for(lambda: assert_exists(service.data / f"{process.pid}.host.started.entered"), 30)
    service.stop(process)
    service.start()
    service.wait_state(task_id, "failed", timeout=55)
    receipt = service.client.request("GET", f"/tasks/{task_id}/receipt")["receipt"]
    assert receipt["state"] == "outcome_unknown"
    assert receipt["request"]["arguments"]["input_object"] == reference.model_dump(mode="json")
    assert raw_task(service, task_id).verification is None
    summary = service.client.request("GET", "/host-budget?namespace=test")
    assert summary["charged_tool_ms"] == 10000
    assert summary["charged_output_bytes"] == 65536


def assert_cleaned(service, task_id):
    database = Database(service.data)
    try:
        with database.engine.begin() as conn:
            assert (
                conn.exec_driver_sql(
                    "SELECT cleaned FROM host_reservations WHERE task_id=?", (task_id,)
                ).scalar_one()
                == 1
            )
    finally:
        database.close()
    assert not Path("/var/lib/cardea-host", task_id.removeprefix("task-")).exists()


def test_copy_measured_accounting_and_automatic_cleanup(service):
    extra = service.document.permissions[-1].model_copy(update={"operation": "host.copy"})
    service.document = service.document.model_copy(
        update={
            "permissions": (*service.document.permissions, extra),
            "platform": (
                *service.document.platform,
                extra.model_copy(update={"principal_id": "platform"}),
            ),
        }
    )
    service.apply(service.document)
    service.start()
    source = stored_input(service)
    task = submit_object(service, source, operation="host.copy", grants=[*RIGHTS, "host.copy"])
    service.wait_state(task, "succeeded", timeout=40)
    output = service.client.request("GET", f"/tasks/{task}/output")["content"]
    assert (
        hashlib.sha256(base64.b64decode(output["content_base64"])).hexdigest()
        == source.content_digest
    )
    receipt = service.client.request("GET", f"/tasks/{task}/receipt")["receipt"]
    assert receipt["request"]["name"] == "host.copy"
    assert 0 < receipt["usage"]["tool_milliseconds"] < 10000
    assert 0 < receipt["usage"]["output_bytes"] < 65536
    wait_for(lambda: assert_cleaned(service, task), 30)
    budget = service.client.request("GET", "/host-budget?namespace=test")
    assert budget["charged_tool_ms"] == receipt["usage"]["tool_milliseconds"]
    assert budget["active_reservations"] == 0 and budget["cleanup_pending"] == 0
    # Reapplying provisioning must not reset spent credits.
    service.apply(service.document)
    assert (
        service.client.request("GET", "/host-budget?namespace=test")["charged_tool_ms"]
        == budget["charged_tool_ms"]
    )


def test_budget_exhaustion_while_reserved_then_slot_release(service):
    service.document = service.document.model_copy(
        update={
            "host_budgets": (
                service.document.host_budgets[0].model_copy(update={"concurrent_operations": 1}),
            )
        }
    )
    service.apply(service.document)
    process = service.start("host.intent_committed")
    task = submit(service)
    wait_for(
        lambda: assert_exists(service.data / f"{process.pid}.host.intent_committed.entered"), 30
    )
    budget = service.client.request("GET", "/host-budget?namespace=test")
    assert budget["active_reservations"] == 1 and budget["allocated_tool_ms"] == 10000
    with pytest.raises(ClientError) as denied:
        submit(service, request_id="over-budget")
    assert denied.value.code == "host_budget_exhausted"
    (service.data / f"{process.pid}.host.intent_committed.release").touch()
    service.wait_state(task, "succeeded", timeout=40)
    second = submit(service, request_id="after-release")
    service.wait_state(second, "succeeded", timeout=40)


def test_cleanup_lost_ack_recovers_without_reopening_effect(service):
    process = service.start("host.cleanup_after_delete")
    task = submit(service)
    wait_for(
        lambda: assert_exists(service.data / f"{process.pid}.host.cleanup_after_delete.entered"), 40
    )
    service.stop(process)
    service.start()
    wait_for(lambda: assert_cleaned(service, task), 30)
    service.wait_state(task, "succeeded", timeout=40)
    replay = subprocess.run(
        ["sudo", "-n", "/usr/local/libexec/cardea-host"],
        input=json.dumps(
            {
                "action": "start",
                "job": task.removeprefix("task-"),
                "operation": "host.sha256",
                "deadline": now().timestamp() + 30,
                "input": "e30=",
            }
        ),
        text=True,
        capture_output=True,
    )
    assert replay.returncode != 0
    assert not Path("/var/lib/cardea-host", task.removeprefix("task-")).exists()


@pytest.mark.parametrize("action", ["cancel", "disable"])
def test_running_control_and_active_cleanup_denial(service, action):
    process = service.start("host.started")
    task = submit(service)
    wait_for(lambda: assert_exists(service.data / f"{process.pid}.host.started.entered"), 30)
    denied = subprocess.run(
        ["sudo", "-n", "/usr/local/libexec/cardea-host"],
        input=json.dumps(
            {
                "action": "cleanup",
                "job": task.removeprefix("task-"),
            }
        ),
        text=True,
        capture_output=True,
    )
    assert denied.returncode != 0
    if action == "cancel":
        service.client.control(task, Signal(request_id="cancel-live"), cancel=True)
    else:
        service.apply(
            service.document.model_copy(
                update={
                    "principals": tuple(
                        p.model_copy(update={"disabled": True})
                        if p.principal_id == "supervisor"
                        else p
                        for p in service.document.principals
                    )
                }
            )
        )
    (service.data / f"{process.pid}.host.started.release").touch()

    def terminal():
        assert raw_task(service, task).state.value == (
            "cancelled" if action == "cancel" else "failed"
        )

    wait_for(terminal, 40)
    wait_for(lambda: assert_cleaned(service, task), 30)
    assert raw_task(service, task).verification is None
