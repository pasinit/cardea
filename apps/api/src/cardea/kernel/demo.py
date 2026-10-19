"""Trusted kernel harness that exercises persistence without executing agent code.

This is deliberately not a model adapter or an authorization demonstration.
Each step affects only bounded local objects inside one SQLite transaction.
"""

import json
from datetime import datetime, timedelta
from uuid import uuid4

from cardea.kernel.models import (
    BudgetLimits,
    Checkpoint,
    DispatchLease,
    EventWait,
    KernelFailure,
    ObjectVersion,
    ResourceRef,
    Task,
    TaskState,
    VerificationRecord,
    canonical_json,
)
from cardea.kernel.store import KernelTransaction, SQLiteKernelStore, StoreError

RUNTIME_ID = "deterministic-index-v1"
VERIFIER_ID = "incident-index-v1"
_INPUTS = (
    {"id": "incident-2", "severity": "high"},
    {"id": "incident-1", "severity": "low"},
)


def _claim(transaction: KernelTransaction, task: Task, at: datetime) -> Task:
    return transaction.change_task(
        task.task_id,
        TaskState.RUNNING,
        expected_revision=task.revision,
        at=at,
        lease=DispatchLease(
            lease_id=f"lease-{uuid4().hex}",
            worker_id="trusted-demo-worker",
            acquired_at=at,
            expires_at=min(at + timedelta(minutes=1), task.limits.deadline),
        ),
    )


def start_demo(store: SQLiteKernelStore, *, task_id: str, at: datetime) -> Task:
    with store.transaction() as transaction:
        task = transaction.submit_task(
            Task(
                task_id=task_id,
                agent_id="demo-agent",
                objective="Index the fictional incident records.",
                runtime_id=RUNTIME_ID,
                verifier_id=VERIFIER_ID,
                budget_account_id=task_id,
                limits=BudgetLimits(
                    max_steps=2,
                    max_model_calls=0,
                    max_model_tokens=0,
                    max_tool_milliseconds=0,
                    max_output_bytes=1048576,
                    max_concurrent_operations=1,
                    deadline=at + timedelta(hours=1),
                ),
                created_at=at,
                updated_at=at,
            )
        )
        task = _claim(transaction, task, at)
        assert task.lease is not None
        inputs = transaction.put_object(
            ResourceRef(namespace=task_id, resource_id="inputs"),
            canonical_json(_INPUTS),
            expected_version=0,
            producer_id="demo-inputs",
            task_id=task_id,
            lease_id=task.lease.lease_id,
            at=at,
        )
        records = json.loads(transaction.read_object(inputs))
        index = {record["id"]: record["severity"] for record in records}
        output = transaction.put_object(
            ResourceRef(namespace=task_id, resource_id="index"),
            canonical_json(index),
            expected_version=0,
            producer_id="trusted-demo-worker",
            task_id=task_id,
            lease_id=task.lease.lease_id,
            at=at,
        )
        cursor = transaction.latest_cursor()
        assert task.lease is not None
        return transaction.change_task(
            task_id,
            TaskState.WAITING,
            expected_revision=task.revision,
            lease_id=task.lease.lease_id,
            at=at,
            checkpoint=Checkpoint(
                checkpoint_id=f"checkpoint-{uuid4().hex}",
                task_id=task_id,
                saved_at=at,
                state={"stage": "indexed"},
                input_versions=(inputs, output),
                event_cursor=cursor,
            ),
            wait=EventWait(namespace=task_id, event_type="demo.resume", after_cursor=cursor),
        )


def _verify(
    transaction: KernelTransaction,
    task: Task,
) -> tuple[bool, ObjectVersion, ObjectVersion]:
    inputs = transaction.latest_object(ResourceRef(namespace=task.task_id, resource_id="inputs"))
    output = transaction.latest_object(ResourceRef(namespace=task.task_id, resource_id="index"))
    if task.checkpoint is None or task.checkpoint.input_versions != (inputs, output):
        return False, inputs, output
    try:
        records = json.loads(transaction.read_object(inputs))
        index = json.loads(transaction.read_object(output))
        passed = (
            isinstance(records, list)
            and isinstance(index, dict)
            and len(index) == len(records)
            and all(
                isinstance(record, dict)
                and set(record) == {"id", "severity"}
                and isinstance(record["id"], str)
                and isinstance(record["severity"], str)
                and record["id"] in index
                and index[record["id"]] == record["severity"]
                for record in records
            )
        )
    except (ValueError, TypeError):
        passed = False
    return passed, inputs, output


def resume_demo(store: SQLiteKernelStore, *, task_id: str, at: datetime) -> Task:
    with store.transaction() as transaction:
        task = transaction.get_task(task_id)
        if task.runtime_id != RUNTIME_ID or task.verifier_id != VERIFIER_ID:
            raise StoreError("unsupported_runtime", "Task is not the installed persistence demo.")
        if task.state is TaskState.SUCCEEDED:
            return task
        if task.state is TaskState.WAITING:
            transaction.append_event(
                namespace=task_id,
                event_type="demo.resume",
                producer_id="demo-supervisor",
                correlation_id=task_id,
                task_id=task_id,
                at=at,
            )
            task = transaction.wake_task(task_id, expected_revision=task.revision, at=at)
        if task.state is not TaskState.QUEUED or task.checkpoint is None:
            raise StoreError("demo_not_ready", "Demo needs a saved checkpoint before resumption.")
        task = _claim(transaction, task, at)
        assert task.lease is not None
        passed, inputs, output = _verify(transaction, task)
        evidence = transaction.put_object(
            ResourceRef(namespace=task_id, resource_id="verification"),
            canonical_json(
                {
                    "passed": passed,
                    "input_digest": inputs.content_digest,
                    "output_digest": output.content_digest,
                }
            ),
            expected_version=0,
            producer_id=VERIFIER_ID,
            at=at,
            task_id=task_id,
            lease_id=task.lease.lease_id,
        )
        assert task.lease is not None
        return transaction.change_task(
            task_id,
            TaskState.SUCCEEDED if passed else TaskState.FAILED,
            expected_revision=task.revision,
            at=at,
            lease_id=task.lease.lease_id,
            verification=VerificationRecord(
                verification_id=f"verification-{uuid4().hex}",
                task_id=task_id,
                verifier_id=VERIFIER_ID,
                checked_at=at,
                passed=True,
                artifacts=(output,),
                evidence=(
                    ResourceRef(namespace=evidence.namespace, resource_id=evidence.resource_id),
                ),
            )
            if passed
            else None,
            failure=None
            if passed
            else KernelFailure(
                code="verification_failed",
                message="Stored index or input versions failed checks.",
            ),
        )
