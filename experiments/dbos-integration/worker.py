"""Bounded trusted integration worker; the DBOS queue is the sole dispatcher."""

import argparse
import json
import os
import sqlite3
import time
from pathlib import Path

import sqlalchemy as sa
from cardea.kernel.lifecycle import match_operation_retry
from cardea.kernel.models import (
    Checkpoint,
    DispatchLease,
    KernelFailure,
    OperationRequest,
    OperationState,
    ResourceRef,
    RetryClass,
    TaskState,
    VerificationRecord,
    canonical_json,
)
from cardea.kernel.store import KernelTransaction, StoreError
from dbos import DBOS, SQLAlchemyDatasource
from sqlalchemy.orm import Session

from adapter import TASK_ID, WORKFLOW_ID, authority, barrier, borrow, engine, new_lease, now, url

root = Path(os.environ["FIT_ROOT"])
application = engine(root)
ds = SQLAlchemyDatasource.create(url(root), engine=application)


@sa.event.listens_for(Session, "before_commit")
def before_commit(session: Session) -> None:
    if "phase" in session.info:
        barrier(root, session.info["phase"] + ".before_commit")


@sa.event.listens_for(Session, "after_commit")
def after_commit(session: Session) -> None:
    if "phase" in session.info:
        barrier(root, session.info["phase"] + ".after_commit")


def acquire(current_lease: str | None = None) -> str | None:
    # Runs on every invocation, outside replay-cached datasource steps.
    while True:
        with application.begin() as conn, borrow(conn) as tx:
            task = tx.get_task(TASK_ID)
            if task.state in {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED}:
                return None
            if task.state is TaskState.RUNNING:
                assert task.lease is not None
                if now() < task.lease.expires_at:
                    if task.lease.lease_id == current_lease:
                        return current_lease
                    busy = True
                else:
                    task = tx.recover_task(TASK_ID, expected_revision=task.revision, at=now())
                    busy = False
            else:
                busy = False
            if not busy:
                if task.state is TaskState.FAILED:
                    return None
                if task.state is TaskState.SUSPENDED:
                    task = tx.change_task(
                        TASK_ID, TaskState.QUEUED, expected_revision=task.revision, at=now()
                    )
                if not authority(conn):
                    tx.change_task(
                        TASK_ID,
                        TaskState.FAILED,
                        expected_revision=task.revision,
                        at=now(),
                        failure=KernelFailure(
                            code="authority_denied", message="Fictional live ceiling denied"
                        ),
                    )
                    return None
                lease = DispatchLease.model_validate(new_lease(DBOS.executor_id))
                tx.change_task(
                    TASK_ID,
                    TaskState.RUNNING,
                    expected_revision=task.revision,
                    at=lease.acquired_at,
                    lease=lease,
                )
                return lease.lease_id
        time.sleep(0.05)


def check_lease(conn: sa.Connection | Session, lease: str) -> None:
    with borrow(conn) as tx:
        task = tx.get_task(TASK_ID)
        if (
            task.state is not TaskState.RUNNING
            or task.lease is None
            or task.lease.lease_id != lease
            or now() >= task.lease.expires_at
        ):
            raise StoreError("stale_lease", "Expired or replaced broker owner")


def check(conn: sa.Connection | Session, lease: str) -> None:
    check_lease(conn, lease)
    if not authority(conn):
        raise StoreError("authority_denied", "Fictional live ceiling denied")


def deny_undispatched(lease: str) -> None:
    with application.begin() as conn, borrow(conn) as tx:
        check_lease(conn, lease)
        task = tx.get_task(TASK_ID)
        # Existing store rejects this transition if there is unresolved dispatched work.
        tx.change_task(
            TASK_ID,
            TaskState.FAILED,
            expected_revision=task.revision,
            lease_id=lease,
            at=now(),
            failure=KernelFailure(
                code="authority_denied", message="Fictional ceiling denied new dispatch"
            ),
        )


def checkpoint(tx: KernelTransaction, req: OperationRequest, lease: str, content: bytes) -> None:
    artifact = tx.put_object(
        req.resource,
        content,
        expected_version=0,
        producer_id="fit-broker",
        task_id=TASK_ID,
        lease_id=lease,
        at=now(),
    )
    receipt = tx.get_operation(TASK_ID, req.operation_id)
    tx.change_operation(
        TASK_ID,
        req.operation_id,
        OperationState.SUCCEEDED,
        expected_revision=receipt.revision,
        lease_id=lease,
        at=now(),
        result={"artifact": artifact.model_dump(mode="json")},
    )
    task = tx.get_task(TASK_ID)
    at = now()
    tx.change_task(
        TASK_ID,
        TaskState.SUSPENDED,
        expected_revision=task.revision,
        lease_id=lease,
        at=at,
        checkpoint=Checkpoint(
            checkpoint_id="published-output",
            task_id=TASK_ID,
            saved_at=at,
            state={"published": True},
            input_versions=(artifact,),
            event_cursor=tx.latest_cursor(),
        ),
    )


def intent(tx: KernelTransaction, req: OperationRequest, lease: str) -> None:
    task = tx.get_task(TASK_ID)
    receipt = tx.admit_operation(
        req,
        grant_ids=("fictional-grant",),
        retry_class=RetryClass.RECONCILIATION_REQUIRED,
        expected_task_revision=task.revision,
        lease_id=lease,
        at=now(),
    )
    tx.change_operation(
        TASK_ID,
        req.operation_id,
        OperationState.RUNNING,
        expected_revision=receipt.revision,
        lease_id=lease,
        at=now(),
    )


@ds.transaction()
def publish(req_json: str, lease: str, bad: bool) -> None:
    req = OperationRequest.model_validate_json(req_json)
    session = ds.sql_session()
    session.info["phase"] = "publish"
    check(session, lease)
    with borrow(session) as tx:
        intent(tx, req, lease)
        checkpoint(tx, req, lease, canonical_json({"value": -1 if bad else req.arguments["value"]}))


@DBOS.step(retries_allowed=True, max_attempts=2, interval_seconds=0.05)
def external(req_json: str, lease: str, retry: bool) -> None:
    req = OperationRequest.model_validate_json(req_json)
    if retry and not (root / "attempted").exists():
        (root / "attempted").touch()
        barrier(root, "retry.before_failure")
        raise RuntimeError("Fictional failure before intent")
    with application.begin() as conn:
        allowed = authority(conn)
    if not allowed:
        deny_undispatched(lease)
        return
    with application.begin() as conn, borrow(conn) as tx:
        check(conn, lease)
        try:
            receipt = tx.get_operation(TASK_ID, req.operation_id)
        except StoreError as error:
            if error.code != "operation_not_found":
                raise
        else:
            match_operation_retry(receipt, req)
            if receipt.state is OperationState.SUCCEEDED:
                return
            raise StoreError(
                "reconciliation_required", "Existing dispatched intent must not repeat"
            )
        intent(tx, req, lease)
    barrier(root, "intent.after_commit")
    # Fake provider transaction, ordered against the broker guard by a SQLite lock.
    # This is NOT a remote-provider atomicity or confinement guarantee.
    with application.begin() as conn:
        check(conn, lease)
        with sqlite3.connect(root / "provider.sqlite3") as provider:
            provider.execute(
                "INSERT INTO effects VALUES (?,?)", (req.model_dump_json(), os.getpid())
            )
    barrier(root, "effect.after_commit")
    with application.begin() as conn, borrow(conn) as tx:
        check(conn, lease)
        checkpoint(tx, req, lease, canonical_json(req.arguments))
    barrier(root, "receipt.after_commit")


@ds.transaction()
def finish(req_json: str, lease: str) -> str:
    req = OperationRequest.model_validate_json(req_json)
    session = ds.sql_session()
    session.info["phase"] = "finish"
    check(session, lease)
    with borrow(session) as tx:
        task = tx.get_task(TASK_ID)
        artifact = tx.latest_object(req.resource)
        passed = tx.read_object(artifact) == canonical_json(req.arguments)
        evidence = tx.put_object(
            ResourceRef(namespace="fit", resource_id="verification"),
            canonical_json({"passed": passed, "digest": artifact.content_digest}),
            expected_version=0,
            producer_id=task.verifier_id,
            task_id=TASK_ID,
            lease_id=lease,
            at=now(),
        )
        if passed:
            target = TaskState.SUCCEEDED
            verification = VerificationRecord(
                verification_id="verified-output",
                task_id=TASK_ID,
                verifier_id=task.verifier_id,
                checked_at=now(),
                passed=True,
                artifacts=(artifact,),
                evidence=(
                    ResourceRef(namespace=evidence.namespace, resource_id=evidence.resource_id),
                ),
            )
            tx.change_task(
                TASK_ID,
                target,
                expected_revision=task.revision,
                lease_id=lease,
                at=now(),
                verification=verification,
            )
        else:
            target = TaskState.FAILED
            tx.change_task(
                TASK_ID,
                target,
                expected_revision=task.revision,
                lease_id=lease,
                at=now(),
                failure=KernelFailure(
                    code="verification_failed", message="Artifact bytes differ from expected value"
                ),
            )
        return target.value


def state() -> str:
    with application.begin() as conn, borrow(conn) as tx:
        return tx.get_task(TASK_ID).state.value


@DBOS.workflow(name="integration")
def workflow(directory: str, req_json: str, scenario: str) -> str:
    lease = acquire()
    if lease is None:
        return state()
    barrier(root, "broker.before_effect")
    try:
        with application.begin() as conn:
            check(conn, lease)
        if scenario in {"external", "retry"}:
            external(req_json, lease, scenario == "retry")
        else:
            publish(req_json, lease, scenario == "bad_artifact")
    except StoreError as error:
        (root / f"{os.getpid()}.denied").write_text(error.code)
        if error.code == "authority_denied":
            deny_undispatched(lease)
            return "failed"
        # Do not invent a successful task or reopen a terminal task on engine failure.
        raise
    lease = acquire(lease)
    if lease is None:
        return state()
    return finish(req_json, lease)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--executor", default="local-stable")
    args = parser.parse_args()
    DBOS(
        config={
            "name": "cardea-integration-fit",
            "system_database_url": url(root),
            "application_version": "fit-v1",
            "executor_id": args.executor,
            "enable_otlp": False,
            "log_level": "WARNING",
            "use_listen_notify": False,
            "notification_listener_polling_interval_sec": 0.05,
        }
    )
    DBOS.launch()
    (root / f"{os.getpid()}.ready").touch()
    try:
        result: str = DBOS.retrieve_workflow(WORKFLOW_ID).get_result()
        (root / f"{os.getpid()}.result.json").write_text(json.dumps(result))
    finally:
        DBOS.destroy()
        application.dispose()
