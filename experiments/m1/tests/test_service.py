from cardea.service.models import Signal, TaskRequest

from harness import ServiceLab


def request(**changes: object) -> TaskRequest:
    return TaskRequest.model_validate({"request_id": "submission-a", "agent_id": "agent-a",
        "namespace": "test", "objective": "Publish verified JSON", "payload": {"value": 1}, **changes})


def test_authenticated_restart_and_verified_artifact(service: ServiceLab) -> None:
    first = service.start()
    admitted = service.client.submit(request(wait_for_event=True))
    task_id = admitted.task.task_id
    service.wait_state(task_id, "waiting")
    receipt = service.client.request("GET", f"/tasks/{task_id}/receipt")
    service.stop(first)
    service.start()
    assert service.client.inspect(task_id).supervisor_id == "supervisor"
    service.client.control(task_id, Signal(request_id="resume-a"))
    service.wait_state(task_id, "succeeded")
    assert service.client.request("GET", f"/tasks/{task_id}/receipt") == receipt
    assert service.client.request("GET", f"/tasks/{task_id}/output")["content"] == {"value": 1}
    assert service.client.inspect(task_id).task.verification is not None

import json
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from cardea.service.admin import backup, restore
from cardea.service.client import ClientError, KernelClient
from cardea.service.database import Database, Fault
from cardea.service.models import RIGHTS
from harness import OUTSIDER, SUPERVISOR, wait_for


def raw_task(service, task_id):
    database = Database(service.data)
    try:
        from cardea.service.database import borrow
        with database.engine.begin() as conn, borrow(conn) as tx:
            return tx.get_task(task_id)
    finally:
        database.close()


def test_cross_principal_and_retry_identity(service):
    service.start()
    req = request(wait_for_event=True)
    task = service.client.submit(req)
    repeated = service.client.submit(req)
    assert repeated.task.task_id == task.task.task_id
    with pytest.raises(ClientError) as changed:
        service.client.submit(request(payload={"value": 2}))
    assert changed.value.code == "request_conflict"
    outsider = KernelClient.for_workload(service.data / "kernel.sock", service.provider.socket,
                                         OUTSIDER, service.config.audience)
    try:
        with pytest.raises(ClientError) as denied:
            outsider.inspect(task.task.task_id)
        assert denied.value.status == 404
    finally:
        outsider.close()


@pytest.mark.parametrize("revocation", ["permission", "delegation", "platform", "supervisor", "agent", "runtime"])
def test_live_revocation_blocks_new_effects(service, revocation):
    service.start()
    task_id = service.client.submit(request(wait_for_event=True)).task.task_id
    service.wait_state(task_id, "waiting")
    original = raw_task(service, task_id)
    doc = service.document
    if revocation in {"permission", "delegation"}:
        permissions = tuple(p.model_copy(update={"delegate": False}) if revocation == "delegation"
            and p.principal_id == "supervisor" and p.operation == "objects.write" else p
            for p in doc.permissions if not (revocation == "permission" and p.principal_id == "supervisor"
            and p.operation == "objects.write"))
        doc = doc.model_copy(update={"permissions": permissions})
    elif revocation == "platform":
        doc = doc.model_copy(update={"platform": tuple(p for p in doc.platform if p.operation != "objects.write")})
    else:
        identity = {"supervisor": "supervisor", "agent": "agent-a", "runtime": "runtime-a"}[revocation]
        doc = doc.model_copy(update={"principals": tuple(p.model_copy(update={"disabled": True})
            if p.principal_id == identity else p for p in doc.principals)})
    service.apply(doc)
    def failed():
        actual = raw_task(service, task_id)
        assert actual.state.value == "failed"
        assert actual.verification is None
        assert actual.checkpoint == original.checkpoint
    wait_for(failed)
    service.stop(service.processes[-1])
    # Runtime disablement deliberately prevents daemon startup; other disablements persist too.
    database = Database(service.data)
    try:
        with database.engine.begin() as conn:
            if revocation in {"supervisor", "agent", "runtime"}:
                assert conn.exec_driver_sql("SELECT disabled FROM principals WHERE principal_id=?", (identity,)).scalar_one() == 1
    finally:
        database.close()


@pytest.mark.parametrize("mode", ["permission", "delegate", "platform", "ancestor"])
def test_admission_ceiling(service, mode):
    service.start()
    parent = None
    if mode == "ancestor":
        parent = service.client.submit(request(request_id="parent", wait_for_event=True,
            grants=tuple(r for r in RIGHTS if r != "tasks.cancel"))).task.task_id
    else:
        doc = service.document
        field = "platform" if mode == "platform" else "permissions"
        values = tuple(p.model_copy(update={"delegate": False}) if mode == "delegate"
            and p.operation == "objects.write" and p.principal_id == "supervisor" else p
            for p in getattr(doc, field) if not (mode != "delegate" and p.operation == "objects.write"
            and p.principal_id in {"platform", "supervisor"}))
        service.apply(doc.model_copy(update={field: values}))
    with pytest.raises(ClientError) as denied:
        service.client.submit(request(parent_task_id=parent))
    assert denied.value.status == 403


def test_cancel_terminal_and_resume_idempotency(service):
    service.start()
    task_id = service.client.submit(request(wait_for_event=True)).task.task_id
    service.wait_state(task_id, "waiting")
    signal = Signal(request_id="cancel-a")
    service.client.control(task_id, signal, cancel=True)
    service.wait_state(task_id, "cancelled")
    service.client.control(task_id, signal, cancel=True)
    with pytest.raises(ClientError):
        service.client.control(task_id, Signal(request_id="late-resume"))
    assert service.client.inspect(task_id).task.verification is None


def test_deadline_survives_restart(service):
    first = service.start()
    task_id = service.client.submit(request(wait_for_event=True, timeout_seconds=5)).task.task_id
    service.wait_state(task_id, "waiting")
    service.stop(first)
    time.sleep(5)
    service.start()
    service.wait_state(task_id, "failed")
    assert service.client.inspect(task_id).task.failure.code == "deadline_exceeded"


def test_backup_restore_shared_wait(service):
    first = service.start()
    task_id = service.client.submit(request(wait_for_event=True)).task.task_id
    service.wait_state(task_id, "waiting")
    receipt = service.client.request("GET", f"/tasks/{task_id}/receipt")
    with pytest.raises(Fault, match="active service"):
        backup(service.data, service.root / "blocked.sqlite3")
    service.stop(first)
    snapshot = service.root / "snapshot.sqlite3"
    backup(service.data, snapshot)
    service.start()
    service.client.control(task_id, Signal(request_id="after-snapshot"), cancel=True)
    service.stop(service.processes[-1])
    restore(service.data, snapshot)
    database = Database(service.data)
    try:
        with database.read() as conn:
            assert conn.exec_driver_sql("SELECT count(*) FROM principals WHERE disabled=0").scalar_one() == 0
    finally:
        database.close()
    service.apply(service.document)
    service.start()
    assert service.client.inspect(task_id).task.state.value == "failed"
    assert service.client.request("GET", f"/tasks/{task_id}/receipt") == receipt
    with pytest.raises(ClientError) as sealed:
        service.client.control(task_id, Signal(request_id="restored-resume"))
    assert sealed.value.code == "restored_scope_sealed"


@pytest.mark.parametrize("stage", ["admission.before_commit", "admission.after_commit", "publish.before_commit", "publish.after_commit", "finish.before_commit", "finish.after_commit", "control.before_commit", "control.after_commit"])
def test_kill_at_shared_commit_boundary(service, stage):
    process = service.start(stage)
    with ThreadPoolExecutor(max_workers=1) as pool:
        if stage.startswith("control"):
            task_id = service.client.submit(request(wait_for_event=True)).task.task_id
            service.wait_state(task_id, "waiting")
            pending = pool.submit(service.client.control, task_id, Signal(request_id="wake-a"))
        else:
            pending = pool.submit(service.client.submit, request())
        barrier = service.data / f"{process.pid}.{stage}.entered"
        wait_for(lambda: barrier.read_text())
        service.stop(process)
        try:
            pending.result(timeout=12)
        except (httpx.HTTPError, ClientError):
            pass
    service.start()
    if stage.startswith("control"):
        service.client.control(task_id, Signal(request_id="wake-a"))
    else:
        task_id = service.client.submit(request()).task.task_id
    service.wait_state(task_id, "succeeded")
    database = Database(service.data)
    try:
        with database.engine.begin() as conn:
            assert conn.exec_driver_sql("SELECT count(*) FROM task_bindings").scalar_one() == 1
            assert conn.exec_driver_sql("SELECT count(*) FROM objects WHERE resource_id=?", (task_id + ".output",)).scalar_one() == 1
    finally:
        database.close()


@pytest.mark.parametrize("change", [{"schema_version": "0.1"}, {"supervisor_id": "outsider"},
    {"timeout_seconds": "5"}, {"wait_for_event": "yes"}, {"payload": {"secret-marker": "x" * 17000}},
    {"payload": {"value": 9007199254740992}}])
def test_strict_wire(service, change):
    service.start()
    with pytest.raises(ClientError) as invalid:
        service.client.request("POST", "/tasks", json={**request().model_dump(mode="json"), **change})
    assert invalid.value.status == 422
    assert "secret-marker" not in str(invalid.value)


def test_wrong_audience_and_missing_auth(service):
    service.start()
    token = service.provider.token(SUPERVISOR, "other-audience")
    with httpx.Client(transport=httpx.HTTPTransport(uds=str(service.data / "kernel.sock")), base_url="http://localhost") as client:
        assert client.get("/v0.2/system").status_code == 401
        assert client.get("/v0.2/system", headers={"Authorization": "Bearer " + token}).status_code == 401
        assert client.post("/v0.2/tasks", content=b"x" * 33000).status_code == 413


def test_sigterm_preserves_wait_and_exclusive_owner(service):
    first = service.start()
    task_id = service.client.submit(request(wait_for_event=True)).task.task_id
    service.wait_state(task_id, "waiting")
    from cardea.service.database import service_lock
    with pytest.raises(Fault, match="active service"), service_lock(service.data):
        pass
    first.terminate()
    assert first.wait(timeout=15) in (0, -15)  # Uvicorn re-raises SIGTERM after draining.
    service.start()
    service.client.control(task_id, Signal(request_id="after-sigterm"))
    service.wait_state(task_id, "succeeded")


def test_ancestor_revocation_and_grants_never_expand(service):
    service.start()
    parent = service.client.submit(request(request_id="parent", wait_for_event=True,
        grants=tuple(r for r in RIGHTS if r != "tasks.cancel"))).task.task_id
    child = service.client.submit(request(request_id="child", parent_task_id=parent,
        grants=tuple(r for r in RIGHTS if r != "tasks.cancel"), wait_for_event=True)).task.task_id
    service.wait_state(child, "waiting")
    with pytest.raises(ClientError) as denied:
        service.client.control(child, Signal(request_id="forbidden"), cancel=True)
    assert denied.value.code == "task_grant_denied"
    service.apply(service.document.model_copy(update={"permissions": tuple(p for p in service.document.permissions
        if not (p.principal_id == "supervisor" and p.operation == "objects.write"))}))
    for task_id in (parent, child):
        wait_for(lambda: assert_failed(service, task_id))
        assert raw_task(service, task_id).verification is None


def assert_failed(service, task_id):
    assert raw_task(service, task_id).state.value == "failed"


def test_early_event_and_event_pages(service):
    process = service.start("workflow.before_publish")
    task_id = service.client.submit(request(wait_for_event=True)).task.task_id
    wait_for(lambda: (service.data / f"{process.pid}.workflow.before_publish.entered").read_text())
    assert service.client.inspect(task_id).task.state.value == "queued"
    service.client.control(task_id, Signal(request_id="early"))
    (service.data / f"{process.pid}.workflow.before_publish.release").touch()
    service.wait_state(task_id, "succeeded")
    events = []
    cursor = 0
    while True:
        page = service.client.request("GET", f"/tasks/{task_id}/events", params={"limit": 2, "after": cursor})
        if not page:
            break
        events.extend(page)
        cursor = page[-1]["cursor"]
    assert len(events) > 4
    assert [e["cursor"] for e in events] == sorted({e["cursor"] for e in events})
    assert all(e["task_id"] == task_id for e in events)


def test_verifier_rejects_corrupt_artifact(service):
    service.start()
    task_id = service.client.submit(request(wait_for_event=True)).task.task_id
    service.wait_state(task_id, "waiting")
    database = Database(service.data)
    try:
        with database.engine.begin() as conn:
            conn.exec_driver_sql("UPDATE objects SET content=? WHERE resource_id=?", (b'{"value":2}', task_id + ".output"))
    finally:
        database.close()
    service.client.control(task_id, Signal(request_id="verify-corrupt"))
    service.wait_state(task_id, "failed")
    assert service.client.inspect(task_id).task.verification is None


@pytest.mark.parametrize("mode", ["revoked", "expired", "cancelled"])
def test_dispatch_rechecks_before_any_publication(service, mode):
    process = service.start("workflow.before_publish")
    task_id = service.client.submit(request()).task.task_id
    wait_for(lambda: (service.data / f"{process.pid}.workflow.before_publish.entered").read_text())
    if mode == "cancelled":
        service.client.control(task_id, Signal(request_id="cancel-before-dispatch"), cancel=True)
    else:
        from datetime import timedelta
        from cardea.service.models import now
        service.apply(service.document.model_copy(update={"permissions": tuple(
            p.model_copy(update={"expires_at": now() - timedelta(seconds=1)}) if mode == "expired"
            and p.principal_id == "supervisor" and p.operation == "objects.write" else p
            for p in service.document.permissions if not (mode == "revoked"
            and p.principal_id == "supervisor" and p.operation == "objects.write"))}))
    (service.data / f"{process.pid}.workflow.before_publish.release").touch()
    service.wait_state(task_id, "cancelled" if mode == "cancelled" else "failed")
    database = Database(service.data)
    try:
        with database.engine.begin() as conn:
            assert conn.exec_driver_sql("SELECT count(*) FROM objects").scalar_one() == 0
    finally:
        database.close()


def test_spire_agent_outage_fails_closed(service):
    service.start()
    token = service.provider.token(SUPERVISOR, service.config.audience)
    client = KernelClient(service.data / "kernel.sock", lambda: ("spiffe", token))
    service.provider.stop(service.provider.agent)
    try:
        started = time.monotonic()
        with pytest.raises(ClientError) as denied:
            client.request("GET", "/system")
        assert denied.value.status == 401
        assert time.monotonic() - started < 4
    finally:
        client.close()
        service.provider.start_agent()
        wait_for(lambda: service.provider.token(SUPERVISOR))
