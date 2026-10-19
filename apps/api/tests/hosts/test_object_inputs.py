"""Exact-version authority and immutable stored inputs for the installed host operation."""

import json
import time
from datetime import timedelta
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_execution import Crash, Host, state
from test_execution import kernel as kernel

from cardea.kernel.models import ObjectVersion, ResourceRef
from cardea.kernel.store import StoreError
from cardea.service import host_runtime
from cardea.service.app import create_app
from cardea.service.database import Fault, borrow
from cardea.service.host_runtime import HostRuntime
from cardea.service.kernel import Kernel, provision
from cardea.service.models import (
    RIGHTS,
    HostBudget,
    HostObjectTaskRequest,
    HostTaskRequest,
    ObjectPermission,
    Permission,
    Provisioning,
    TaskRequest,
    now,
)
from cardea.service.policy import Policy, object_key


def seed(
    kernel: Kernel, content: bytes = b"declared\x00bytes\xff", **changes: Any
) -> ObjectVersion:
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        return tx.put_object(
            ResourceRef(namespace=changes.get("namespace", "test"), resource_id="source"),
            content,
            expected_version=changes.get("expected_version", 0),
            producer_id="fixture",
            at=now(),
        )


def grant(kernel: Kernel, reference: ObjectVersion) -> None:
    expiry = now() + timedelta(hours=1)
    permissions = tuple(
        Permission(
            principal_id="supervisor",
            namespace=ns,
            operation=right,
            delegate=True,
            expires_at=expiry,
        )
        for ns in {"test", reference.namespace}
        for right in (*RIGHTS, "host.sha256")
    )
    permission = ObjectPermission(
        principal_id="supervisor",
        object=reference,
        delegate=True,
        expires_at=expiry,
    )
    provision(
        kernel.database,
        Provisioning(
            principals=(),
            permissions=permissions,
            platform=tuple(p.model_copy(update={"principal_id": "platform"}) for p in permissions),
            host_budgets=(
                HostBudget(
                    principal_id="supervisor",
                    namespace="test",
                    tool_milliseconds=1000000,
                    output_bytes=10000000,
                ),
            ),
            object_permissions=(permission,),
            object_platform=(permission.model_copy(update={"principal_id": "platform"}),),
        ),
    )


def object_request(reference: ObjectVersion, **changes: Any) -> HostObjectTaskRequest:
    return HostObjectTaskRequest.model_validate(
        {
            "request_id": "object-one",
            "agent_id": "agent",
            "namespace": "test",
            "objective": "Hash one stored version",
            "input_object": reference.model_dump(),
            "operation": "host.sha256",
            "grants": [*RIGHTS, "host.sha256"],
            **changes,
        }
    )


def submit(kernel: Kernel, reference: ObjectVersion, **changes: Any) -> str:
    return kernel.submit(
        kernel.auth.validate("supervisor"), object_request(reference, **changes)
    ).task.task_id


def test_pinned_bytes_survive_new_version_and_reference_is_in_receipt(kernel: Kernel) -> None:
    reference = seed(kernel)
    grant(kernel, reference)
    task_id = submit(kernel, reference)
    assert submit(kernel, reference) == task_id
    newer = seed(kernel, b"new version", expected_version=1)
    with pytest.raises(Fault) as conflict:
        submit(kernel, newer)
    assert conflict.value.code == "request_conflict"
    assert HostRuntime(kernel).execute(task_id) == "succeeded"
    host = cast(Host, kernel.host)
    assert host.content == b"declared\x00bytes\xff" and host.starts == 1
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        receipt = tx.get_operation(task_id, "execute")
        assert receipt.request.arguments["input_object"] == reference.model_dump(mode="json")
        output = tx.latest_object(ResourceRef(namespace="test", resource_id=f"{task_id}.output"))
        assert json.loads(tx.read_object(output)) == {
            "sha256": reference.content_digest,
            "bytes": len(host.content),
        }


@pytest.mark.parametrize(
    "change",
    [
        {"namespace": "other"},
        {"resource_id": "other"},
        {"version": 2},
        {"content_digest": "f" * 64},
    ],
)
def test_exact_object_identity_denies_substitutions(kernel: Kernel, change: dict[str, Any]) -> None:
    reference = seed(kernel)
    grant(kernel, reference)
    with pytest.raises(Fault) as denied:
        submit(kernel, reference.model_copy(update=change))
    assert denied.value.code == "authority_denied"
    assert cast(Host, kernel.host).starts == 0


@pytest.mark.parametrize("phase", ["admission", "dispatch", "after_intent", "publication"])
@pytest.mark.parametrize("mode", ["allow", "delegate", "platform", "expired"])
def test_resource_ceiling_at_each_boundary(kernel: Kernel, phase: str, mode: str) -> None:
    reference = seed(kernel)
    grant(kernel, reference)

    def revoke() -> None:
        with kernel.database.engine.begin() as conn:
            if mode == "expired":
                conn.exec_driver_sql(
                    "UPDATE policies SET expires_at=0 WHERE namespace=?", (object_key(reference),)
                )
            else:
                conn.exec_driver_sql(
                    "DELETE FROM policies WHERE namespace=? AND principal_id=? AND mode=?",
                    (
                        object_key(reference),
                        "platform" if mode == "platform" else "supervisor",
                        "allow" if mode == "platform" else mode,
                    ),
                )

    if phase == "admission":
        revoke()
        with pytest.raises(Fault):
            submit(kernel, reference)
        return
    task_id = submit(kernel, reference)
    if phase == "dispatch":
        revoke()
    elif phase == "after_intent":
        kernel.fault = lambda stage: revoke() if stage == "host.intent_committed" else None
    else:
        cast(Host, kernel.host).observer = revoke
    assert HostRuntime(kernel).execute(task_id) == "failed"
    assert cast(Host, kernel.host).starts == (1 if phase == "publication" else 0)
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        assert tx.get_task(task_id).verification is None
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM objects").scalar_one() == 1


def test_namespace_permission_and_explicit_source_are_both_required(kernel: Kernel) -> None:
    reference = seed(kernel, namespace="source-ns")
    with pytest.raises(Fault):
        submit(kernel, reference)
    grant(kernel, reference)
    with kernel.database.engine.begin() as conn:
        conn.exec_driver_sql("DELETE FROM policies WHERE namespace='source-ns'")
    with pytest.raises(Fault):
        submit(kernel, reference)
    grant(kernel, reference)
    task_id = submit(kernel, reference)
    assert HostRuntime(kernel).execute(task_id) == "succeeded"


def test_omitted_resource_policies_revoke_and_unknown_principal_is_atomic(kernel: Kernel) -> None:
    reference = seed(kernel)
    grant(kernel, reference)
    invalid = ObjectPermission(
        principal_id="unknown", object=reference, expires_at=now() + timedelta(hours=1)
    )
    with pytest.raises(Fault):
        provision(
            kernel.database,
            Provisioning(principals=(), permissions=(), platform=(), object_permissions=(invalid,)),
        )
    with kernel.database.engine.begin() as conn:
        Policy(conn).object_read("supervisor", reference)
    provision(kernel.database, Provisioning(principals=(), permissions=(), platform=()))
    with kernel.database.engine.begin() as conn:
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM policies").scalar_one() == 0


@pytest.mark.parametrize("kind", ["corrupt", "missing", "oversized"])
def test_invalid_stored_input_never_starts(kernel: Kernel, kind: str) -> None:
    reference = seed(kernel, b"x" * 16385 if kind == "oversized" else b"valid")
    grant(kernel, reference)
    if kind != "oversized":
        with kernel.database.engine.begin() as conn:
            conn.exec_driver_sql(
                "DELETE FROM objects" if kind == "missing" else "UPDATE objects SET content=x'ff'"
            )
    with pytest.raises((Fault, StoreError)):
        submit(kernel, reference)
    assert cast(Host, kernel.host).starts == 0


@pytest.mark.parametrize("phase", ["dispatch", "publication"])
def test_corrupt_after_admission_fails_without_result(kernel: Kernel, phase: str) -> None:
    reference = seed(kernel)
    grant(kernel, reference)
    task_id = submit(kernel, reference)

    def corrupt() -> None:
        with kernel.database.engine.begin() as conn:
            conn.exec_driver_sql("UPDATE objects SET content=x'ff'")

    if phase == "dispatch":
        corrupt()
    else:
        cast(Host, kernel.host).observer = corrupt
    assert HostRuntime(kernel).execute(task_id) == "failed"
    assert cast(Host, kernel.host).starts == (1 if phase == "publication" else 0)


@pytest.mark.parametrize("key_kind", ["object", "source_namespace"])
def test_source_expiry_attenuates_worker_deadline(kernel: Kernel, key_kind: str) -> None:
    reference = seed(kernel, namespace="source-ns")
    grant(kernel, reference)
    task_id = submit(kernel, reference)
    expiry = time.time() + 10
    with kernel.database.engine.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE policies SET expires_at=? WHERE namespace=?",
            (expiry, object_key(reference) if key_kind == "object" else "source-ns"),
        )
    host = cast(Host, kernel.host)
    original = host.start

    def start(job: str, content: bytes, deadline: float, operation: str = "host.sha256") -> None:
        assert deadline <= expiry
        original(job, content, deadline)

    host.start = start  # type: ignore[method-assign]
    assert HostRuntime(kernel).execute(task_id) == "succeeded"


@pytest.mark.parametrize("point", ["host.intent_committed", "host.started", "host.before_settle"])
def test_interrupted_object_intent_does_not_relaunch(
    kernel: Kernel,
    monkeypatch: pytest.MonkeyPatch,
    point: str,
) -> None:
    reference = seed(kernel)
    grant(kernel, reference)
    task_id = submit(kernel, reference)

    def crash(stage: str) -> None:
        if stage == point:
            raise Crash()

    kernel.fault = crash
    with pytest.raises(Crash):
        HostRuntime(kernel).execute(task_id)
    starts = cast(Host, kernel.host).starts
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        task = tx.get_task(task_id)
        assert task.lease
        expired = task.lease.expires_at + timedelta(seconds=1)
    kernel.fault = lambda stage: None
    monkeypatch.setattr(host_runtime, "now", lambda: expired)
    assert HostRuntime(kernel).execute(task_id) == "failed"
    assert cast(Host, kernel.host).starts == starts
    assert state(kernel, task_id) == ("failed", "outcome_unknown")


def test_api_has_separate_strict_input_contract(kernel: Kernel) -> None:
    reference = seed(kernel)
    grant(kernel, reference)
    body = object_request(reference).model_dump(mode="json")
    headers = {"Authorization": "Bearer supervisor"}
    with TestClient(create_app(kernel)) as client:
        assert client.post("/v0.2/host-object-tasks", json=body).status_code == 401
        for change in (
            {"payload": {"override": True}},
            {"path": "/etc/passwd"},
            {"parent_task_id": "parent"},
            {"grants": list(RIGHTS)},
        ):
            assert (
                client.post(
                    "/v0.2/host-object-tasks", json=body | change, headers=headers
                ).status_code
                == 422
            )
        assert client.post("/v0.2/host-tasks", json=body, headers=headers).status_code == 422
        assert client.post("/v0.2/host-object-tasks", json=body, headers=headers).status_code == 202
    with pytest.raises(ValidationError):
        object_request(reference, wait_for_event=True)


def test_existing_request_digests_unchanged() -> None:
    common: dict[str, Any] = dict(
        request_id="compat",
        agent_id="agent",
        namespace="test",
        objective="compatibility",
        payload={"value": 1},
    )
    assert TaskRequest(**common).digest("supervisor") == (
        "1c60586a8d1d3a17c4862ea5df590ac492922eabbe6f78961c925cf5dd3b2354"
    )
    inline = HostTaskRequest(**common, operation="host.sha256", grants=(*RIGHTS, "host.sha256"))
    assert inline.digest("supervisor") == (
        "bed749a0c1364d7f1617f45e79a7f58e45fd82c3149ef74da71b93cdce26f06c"
    )
    assert "input_object" not in inline.model_dump()
