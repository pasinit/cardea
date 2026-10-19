import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest
from pydantic import ValidationError

from cardea.kernel.lifecycle import ContractError, ContractErrorCode
from cardea.kernel.models import (
    Checkpoint,
    DispatchLease,
    EventWait,
    OperationRequest,
    OperationState,
    ResourceRef,
    RetryClass,
    Task,
    TaskState,
)
from cardea.kernel.store import MAX_OBJECT_BYTES, SQLiteKernelStore, StoreError


def seed_running(directory: Path, task: Task, lease: DispatchLease, now: datetime) -> None:
    with SQLiteKernelStore(directory) as store, store.transaction() as transaction:
        transaction.submit_task(task)
        transaction.change_task(
            task.task_id,
            TaskState.RUNNING,
            expected_revision=0,
            at=now,
            lease=lease,
        )


def test_task_and_journal_survive_reopening(tmp_path: Path, task: Task) -> None:
    directory = tmp_path / "state"
    with SQLiteKernelStore(directory) as store, store.transaction() as transaction:
        transaction.submit_task(task)
    with SQLiteKernelStore(directory) as store, store.transaction(write=False) as transaction:
        assert transaction.get_task(task.task_id) == task
        events = transaction.read_events(task_id=task.task_id)
        assert len(events) == 1
        assert events[0].event_type == "task.submitted"
        assert events[0].payload == {"revision": 0, "state": "queued"}
    assert directory.stat().st_mode & 0o077 == 0
    assert (directory / "kernel.sqlite3").stat().st_mode & 0o077 == 0


@pytest.mark.parametrize(
    "point,committed",
    [
        ("task_written", False),
        ("before_commit", False),
        ("after_commit", True),
    ],
)
def test_task_and_journal_share_commit_even_on_abrupt_process_exit(
    tmp_path: Path,
    task: Task,
    point: str,
    committed: bool,
) -> None:
    script = """
import os, sys
from pathlib import Path
from cardea.kernel.models import Task
from cardea.kernel.store import SQLiteKernelStore
def crash(point):
    if point == sys.argv[3]:
        os._exit(73)
with SQLiteKernelStore(Path(sys.argv[1]), fault=crash) as store:
    with store.transaction() as tx:
        tx.submit_task(Task.model_validate_json(sys.argv[2]))
"""
    directory = tmp_path / "crash-state"
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"))
    child = subprocess.run(
        [sys.executable, "-c", script, str(directory), task.model_dump_json(), point],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert child.returncode == 73, child.stderr
    with SQLiteKernelStore(directory) as store, store.transaction(write=False) as transaction:
        events = transaction.read_events(task_id=task.task_id)
        if committed:
            assert transaction.get_task(task.task_id) == task
            assert len(events) == 1
        else:
            with pytest.raises(StoreError, match="does not exist"):
                transaction.get_task(task.task_id)
            assert events == ()


def test_competing_connections_cannot_both_claim_a_task(
    tmp_path: Path,
    task: Task,
    now: datetime,
) -> None:
    directory = tmp_path / "state"
    with SQLiteKernelStore(directory) as store, store.transaction() as transaction:
        transaction.submit_task(task)
    barrier = Barrier(2)

    def claim(number: int) -> str:
        with SQLiteKernelStore(directory) as connection:
            barrier.wait(timeout=5)
            try:
                with connection.transaction() as transaction:
                    transaction.change_task(
                        task.task_id,
                        TaskState.RUNNING,
                        expected_revision=0,
                        at=now,
                        lease=DispatchLease(
                            lease_id=f"lease-{number}",
                            worker_id=f"worker-{number}",
                            acquired_at=now,
                            expires_at=now + timedelta(minutes=1),
                        ),
                    )
                return "claimed"
            except ContractError as error:
                return error.code.value

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(claim, (1, 2)))
    assert sorted(outcomes) == ["claimed", "revision_conflict"]
    with SQLiteKernelStore(directory) as store, store.transaction(write=False) as transaction:
        assert transaction.get_task(task.task_id).revision == 1
        assert len(transaction.read_events(task_id=task.task_id)) == 2


def test_caught_mutation_failure_still_rolls_back_prior_writes(
    tmp_path: Path,
    now: datetime,
) -> None:
    reference = ResourceRef(namespace="outputs", resource_id="index")
    with SQLiteKernelStore(tmp_path / "state") as store:
        # Catch the inner error before leaving the transaction; it must still roll back the blob.
        with (
            pytest.raises(StoreError) as error,
            store.transaction() as transaction,
            pytest.raises(ValidationError),
        ):
            transaction.put_object(
                reference,
                b"hello",
                expected_version=0,
                producer_id="invalid actor",
                at=now,
            )
        assert error.value.code == "transaction_aborted"
        with store.transaction(write=False) as transaction:
            assert transaction.read_events() == ()
            with pytest.raises(StoreError, match="does not exist"):
                transaction.latest_object(reference)


def test_object_versions_and_digest_survive_reopening(tmp_path: Path, now: datetime) -> None:
    directory = tmp_path / "state"
    reference = ResourceRef(namespace="outputs", resource_id="index")
    with SQLiteKernelStore(directory) as store, store.transaction() as transaction:
        first = transaction.put_object(
            reference,
            b"first",
            expected_version=0,
            producer_id="kernel",
            at=now,
        )
        second = transaction.put_object(
            reference,
            b"second",
            expected_version=1,
            producer_id="kernel",
            at=now,
        )
    with SQLiteKernelStore(directory) as store:
        with store.transaction(write=False) as transaction:
            assert transaction.read_object(first) == b"first"
            assert transaction.read_object(second) == b"second"
            assert transaction.latest_object(reference) == second
        with pytest.raises(StoreError) as error, store.transaction() as transaction:
            transaction.put_object(
                reference,
                b"stale",
                expected_version=1,
                producer_id="kernel",
                at=now,
            )
        assert error.value.code == "object_version_conflict"
        with store.transaction(write=False) as transaction:
            assert len(transaction.read_events()) == 2


def test_corrupt_object_is_not_returned(tmp_path: Path, now: datetime) -> None:
    directory = tmp_path / "state"
    with SQLiteKernelStore(directory) as store, store.transaction() as transaction:
        version = transaction.put_object(
            ResourceRef(namespace="outputs", resource_id="index"),
            b"original",
            expected_version=0,
            producer_id="kernel",
            at=now,
        )
    with sqlite3.connect(directory / "kernel.sqlite3") as connection:
        connection.execute("UPDATE objects SET content = ?", (b"corrupt",))
    with SQLiteKernelStore(directory) as store, store.transaction(write=False) as transaction:
        with pytest.raises(StoreError) as error:
            transaction.read_object(version)
        assert error.value.code == "object_corrupt"


def test_large_objects_and_unbounded_event_pages_are_rejected(
    tmp_path: Path,
    now: datetime,
) -> None:
    with SQLiteKernelStore(tmp_path / "state") as store:
        with pytest.raises(StoreError) as error, store.transaction() as transaction:
            transaction.put_object(
                ResourceRef(namespace="outputs", resource_id="large"),
                b"x" * (MAX_OBJECT_BYTES + 1),
                expected_version=0,
                producer_id="kernel",
                at=now,
            )
        assert error.value.code == "object_too_large"
        with store.transaction(write=False) as transaction:
            for limit in (0, 1001, True):
                with pytest.raises(StoreError):
                    transaction.read_events(limit=limit)


def test_event_pages_preserve_order_and_journal_is_append_only(
    tmp_path: Path,
    task: Task,
    lease: DispatchLease,
    now: datetime,
) -> None:
    directory = tmp_path / "state"
    seed_running(directory, task, lease, now)
    with SQLiteKernelStore(directory) as store, store.transaction(write=False) as transaction:
        first = transaction.read_events(limit=1)
        rest = transaction.read_events(after_cursor=first[0].cursor, task_id=task.task_id)
        assert len(first) == 1 and len(rest) == 1
        assert rest[0].cursor > first[0].cursor
    with (
        sqlite3.connect(directory / "kernel.sqlite3") as connection,
        pytest.raises(sqlite3.IntegrityError, match="append-only"),
    ):
        connection.execute("DELETE FROM events")


def test_wait_registration_observes_an_event_already_in_the_journal(
    tmp_path: Path,
    task: Task,
    lease: DispatchLease,
    now: datetime,
    checkpoint: Checkpoint,
) -> None:
    directory = tmp_path / "state"
    seed_running(directory, task, lease, now)
    with SQLiteKernelStore(directory) as store, store.transaction() as transaction:
        cursor = transaction.latest_cursor()
        transaction.append_event(
            namespace="inputs",
            event_type="input.changed",
            producer_id="input-adapter",
            correlation_id="input-1",
            at=now,
        )
        saved = Checkpoint.model_validate(checkpoint.model_dump() | {"event_cursor": cursor})
        task = transaction.change_task(
            task.task_id,
            TaskState.WAITING,
            expected_revision=1,
            at=now,
            lease_id=lease.lease_id,
            checkpoint=saved,
            wait=EventWait(namespace="inputs", event_type="input.changed", after_cursor=cursor),
        )
        assert task.state is TaskState.QUEUED
        assert task.revision == 3


def test_wait_survives_restart_and_wakes_only_for_a_matching_event(
    tmp_path: Path,
    task: Task,
    lease: DispatchLease,
    now: datetime,
    checkpoint: Checkpoint,
) -> None:
    directory = tmp_path / "state"
    seed_running(directory, task, lease, now)
    with SQLiteKernelStore(directory) as store, store.transaction() as transaction:
        cursor = transaction.latest_cursor()
        saved = Checkpoint.model_validate(checkpoint.model_dump() | {"event_cursor": cursor})
        waiting = transaction.change_task(
            task.task_id,
            TaskState.WAITING,
            expected_revision=1,
            at=now,
            lease_id=lease.lease_id,
            checkpoint=saved,
            wait=EventWait(namespace="inputs", event_type="input.changed", after_cursor=cursor),
        )
    with SQLiteKernelStore(directory) as store:
        with pytest.raises(StoreError) as error, store.transaction() as transaction:
            transaction.wake_task(task.task_id, expected_revision=waiting.revision, at=now)
        assert error.value.code == "wait_not_ready"
        with store.transaction() as transaction:
            transaction.append_event(
                namespace="inputs",
                event_type="input.changed",
                producer_id="input-adapter",
                correlation_id="input-2",
                at=now,
            )
            resumed = transaction.wake_task(
                task.task_id, expected_revision=waiting.revision, at=now
            )
            assert resumed.state is TaskState.QUEUED
            assert resumed.checkpoint == waiting.checkpoint


def test_expired_worker_is_fenced_and_lease_ids_cannot_be_reused(
    tmp_path: Path,
    task: Task,
    lease: DispatchLease,
    now: datetime,
) -> None:
    directory = tmp_path / "state"
    seed_running(directory, task, lease, now)
    later = lease.expires_at + timedelta(seconds=1)
    with SQLiteKernelStore(directory) as store:
        with pytest.raises(StoreError) as error, store.transaction() as transaction:
            transaction.recover_task(task.task_id, expected_revision=1, at=now)
        assert error.value.code == "lease_not_expired"
        with store.transaction() as transaction:
            recovered = transaction.recover_task(task.task_id, expected_revision=1, at=later)
            assert recovered.state is TaskState.QUEUED
        recycled = DispatchLease.model_validate(
            lease.model_dump()
            | {
                "acquired_at": later,
                "expires_at": later + timedelta(minutes=1),
            }
        )
        with pytest.raises(StoreError) as error, store.transaction() as transaction:
            transaction.change_task(
                task.task_id,
                TaskState.RUNNING,
                expected_revision=2,
                at=later,
                lease=recycled,
            )
        assert error.value.code == "lease_reused"
        with store.transaction() as transaction:
            transaction.change_task(
                task.task_id,
                TaskState.RUNNING,
                expected_revision=2,
                at=later,
                lease=DispatchLease.model_validate(
                    recycled.model_dump() | {"lease_id": "new-lease"}
                ),
            )
        with pytest.raises(StoreError) as error, store.transaction() as transaction:
            transaction.change_task(
                task.task_id,
                TaskState.CANCELLED,
                expected_revision=3,
                at=later,
                lease_id=lease.lease_id,
            )
        assert error.value.code == "stale_lease"


def test_interrupted_effect_is_persisted_as_unknown_without_reexecution(
    tmp_path: Path,
    task: Task,
    lease: DispatchLease,
    now: datetime,
    operation_request: OperationRequest,
) -> None:
    directory = tmp_path / "state"
    seed_running(directory, task, lease, now)
    with SQLiteKernelStore(directory) as store, store.transaction() as transaction:
        receipt = transaction.admit_operation(
            operation_request,
            grant_ids=("grant-output",),
            retry_class=RetryClass.IDEMPOTENT,
            expected_task_revision=1,
            lease_id=lease.lease_id,
            at=now,
        )
        transaction.change_operation(
            task.task_id,
            operation_request.operation_id,
            OperationState.RUNNING,
            expected_revision=0,
            at=now,
            lease_id=lease.lease_id,
        )
    later = lease.expires_at + timedelta(seconds=1)
    with SQLiteKernelStore(directory) as store, store.transaction() as transaction:
        recovered = transaction.recover_task(task.task_id, expected_revision=1, at=later)
        assert recovered.state is TaskState.FAILED
        assert recovered.failure is not None
        assert recovered.failure.code == "reconciliation_required"
    with SQLiteKernelStore(directory) as store:
        with store.transaction() as transaction:
            unknown = transaction.admit_operation(
                operation_request,
                grant_ids=("grant-output",),
                retry_class=RetryClass.IDEMPOTENT,
                expected_task_revision=1,
                lease_id=lease.lease_id,
                at=later,
            )
            assert unknown.state is OperationState.OUTCOME_UNKNOWN
            assert unknown.receipt_id == receipt.receipt_id
        with store.transaction() as transaction:
            evidence = ResourceRef(namespace="checks", resource_id="observed-effect")
            transaction.put_object(
                evidence,
                b'{"observed":true}',
                expected_version=0,
                producer_id="verifier",
                at=later,
            )
            settled = transaction.reconcile_operation(
                task.task_id,
                operation_request.operation_id,
                OperationState.SUCCEEDED,
                expected_revision=2,
                at=later,
                evidence=(evidence,),
                result={"version": 1},
            )
            assert settled.state is OperationState.SUCCEEDED
            assert transaction.get_task(task.task_id).state is TaskState.FAILED


def test_changed_retry_cannot_replace_persisted_request(
    tmp_path: Path,
    task: Task,
    lease: DispatchLease,
    now: datetime,
    operation_request: OperationRequest,
) -> None:
    directory = tmp_path / "state"
    seed_running(directory, task, lease, now)
    with SQLiteKernelStore(directory) as store:
        with store.transaction() as transaction:
            transaction.admit_operation(
                operation_request,
                grant_ids=("grant-output",),
                retry_class=RetryClass.IDEMPOTENT,
                expected_task_revision=1,
                lease_id=lease.lease_id,
                at=now,
            )
        with pytest.raises(ContractError) as error, store.transaction() as transaction:
            transaction.admit_operation(
                OperationRequest.model_validate(operation_request.model_dump() | {"arguments": {}}),
                grant_ids=("grant-output",),
                retry_class=RetryClass.IDEMPOTENT,
                expected_task_revision=1,
                lease_id=lease.lease_id,
                at=now,
            )
        assert error.value.code is ContractErrorCode.REQUEST_CONFLICT
        with store.transaction(write=False) as transaction:
            stored = transaction.get_operation(task.task_id, operation_request.operation_id)
            assert stored.request == operation_request


def test_read_only_and_closed_transactions_cannot_mutate(tmp_path: Path, task: Task) -> None:
    with SQLiteKernelStore(tmp_path / "state") as store:
        with pytest.raises(StoreError), store.transaction(write=False) as transaction:
            transaction.submit_task(task)
        with store.transaction() as transaction:
            transaction.submit_task(task)
        with pytest.raises(StoreError, match="closed"):
            transaction.get_task(task.task_id)


def test_future_schema_and_symlink_store_paths_fail_closed(tmp_path: Path) -> None:
    directory = tmp_path / "state"
    with SQLiteKernelStore(directory):
        pass
    with sqlite3.connect(directory / "kernel.sqlite3") as connection:
        connection.execute("PRAGMA user_version = 999")
    with pytest.raises(StoreError) as error:
        SQLiteKernelStore(directory)
    assert error.value.code == "unsupported_store"
    link = tmp_path / "linked-state"
    link.symlink_to(directory, target_is_directory=True)
    with pytest.raises(StoreError) as error:
        SQLiteKernelStore(link)
    assert error.value.code == "unsafe_store_path"


@pytest.mark.parametrize("point", ["object_written", "operation_written", "before_commit"])
def test_object_and_receipt_failure_leave_no_partial_publication(
    tmp_path: Path,
    task: Task,
    lease: DispatchLease,
    now: datetime,
    operation_request: OperationRequest,
    point: str,
) -> None:
    directory = tmp_path / "state"
    seed_running(directory, task, lease, now)

    def fail(stage: str) -> None:
        if stage == point:
            raise RuntimeError("injected write failure")

    with (
        SQLiteKernelStore(directory, fault=fail) as store,
        pytest.raises(RuntimeError, match="injected"),
        store.transaction() as transaction,
    ):
        transaction.put_object(
            operation_request.resource,
            b"output",
            expected_version=0,
            producer_id=task.agent_id,
            task_id=task.task_id,
            lease_id=lease.lease_id,
            at=now,
        )
        transaction.admit_operation(
            operation_request,
            grant_ids=("grant-output",),
            retry_class=RetryClass.IDEMPOTENT,
            expected_task_revision=1,
            lease_id=lease.lease_id,
            at=now,
        )
    with SQLiteKernelStore(directory) as store, store.transaction(write=False) as transaction:
        with pytest.raises(StoreError, match="does not exist"):
            transaction.latest_object(operation_request.resource)
        with pytest.raises(StoreError, match="does not exist"):
            transaction.get_operation(task.task_id, operation_request.operation_id)
        assert len(transaction.read_events()) == 2
        assert transaction.get_task(task.task_id).revision == 1


def test_expired_worker_cannot_publish_an_object(
    tmp_path: Path,
    task: Task,
    lease: DispatchLease,
    now: datetime,
) -> None:
    directory = tmp_path / "state"
    seed_running(directory, task, lease, now)
    with SQLiteKernelStore(directory) as store:
        with pytest.raises(StoreError) as error, store.transaction() as transaction:
            transaction.put_object(
                ResourceRef(namespace="outputs", resource_id="late"),
                b"late",
                expected_version=0,
                producer_id=task.agent_id,
                task_id=task.task_id,
                lease_id=lease.lease_id,
                at=lease.expires_at,
            )
        assert error.value.code == "stale_lease"


def test_recovery_cancels_undispatched_intent_and_requeues_initial_state(
    tmp_path: Path,
    task: Task,
    lease: DispatchLease,
    now: datetime,
    operation_request: OperationRequest,
) -> None:
    directory = tmp_path / "state"
    seed_running(directory, task, lease, now)
    with SQLiteKernelStore(directory) as store:
        with store.transaction() as transaction:
            transaction.admit_operation(
                operation_request,
                grant_ids=("grant-output",),
                retry_class=RetryClass.IDEMPOTENT,
                expected_task_revision=1,
                lease_id=lease.lease_id,
                at=now,
            )
        with store.transaction() as transaction:
            recovered = transaction.recover_task(
                task.task_id,
                expected_revision=1,
                at=lease.expires_at,
            )
            assert recovered.state is TaskState.QUEUED
            assert (
                transaction.get_operation(
                    task.task_id,
                    operation_request.operation_id,
                ).state
                is OperationState.CANCELLED
            )


def test_recovery_does_not_repeat_completed_work_without_a_checkpoint(
    tmp_path: Path,
    task: Task,
    lease: DispatchLease,
    now: datetime,
    operation_request: OperationRequest,
) -> None:
    directory = tmp_path / "state"
    seed_running(directory, task, lease, now)
    with SQLiteKernelStore(directory) as store, store.transaction() as transaction:
        transaction.admit_operation(
            operation_request,
            grant_ids=("grant-output",),
            retry_class=RetryClass.IDEMPOTENT,
            expected_task_revision=1,
            lease_id=lease.lease_id,
            at=now,
        )
        transaction.change_operation(
            task.task_id,
            operation_request.operation_id,
            OperationState.RUNNING,
            expected_revision=0,
            at=now,
            lease_id=lease.lease_id,
        )
        transaction.change_operation(
            task.task_id,
            operation_request.operation_id,
            OperationState.SUCCEEDED,
            expected_revision=1,
            at=now,
            lease_id=lease.lease_id,
            result={"done": True},
        )
    with SQLiteKernelStore(directory) as store, store.transaction() as transaction:
        recovered = transaction.recover_task(task.task_id, expected_revision=1, at=lease.expires_at)
        assert recovered.state is TaskState.FAILED
        assert recovered.failure is not None and recovered.failure.code == "checkpoint_missing"


def test_recovery_fails_an_expired_task_budget(
    tmp_path: Path,
    task: Task,
    lease: DispatchLease,
    now: datetime,
) -> None:
    directory = tmp_path / "state"
    seed_running(directory, task, lease, now)
    with SQLiteKernelStore(directory) as store, store.transaction() as transaction:
        recovered = transaction.recover_task(
            task.task_id,
            expected_revision=1,
            at=task.limits.deadline,
        )
        assert recovered.state is TaskState.FAILED
        assert recovered.failure is not None and recovered.failure.code == "deadline_exceeded"


def test_pending_operations_prevent_task_completion_or_yield(
    tmp_path: Path,
    task: Task,
    lease: DispatchLease,
    now: datetime,
    operation_request: OperationRequest,
) -> None:
    directory = tmp_path / "state"
    seed_running(directory, task, lease, now)
    with SQLiteKernelStore(directory) as store:
        with store.transaction() as transaction:
            transaction.admit_operation(
                operation_request,
                grant_ids=("grant-output",),
                retry_class=RetryClass.IDEMPOTENT,
                expected_task_revision=1,
                lease_id=lease.lease_id,
                at=now,
            )
        for target in (TaskState.SUCCEEDED, TaskState.WAITING, TaskState.CANCELLED):
            with pytest.raises(StoreError) as error, store.transaction() as transaction:
                transaction.change_task(
                    task.task_id,
                    target,
                    expected_revision=1,
                    at=now,
                    lease_id=lease.lease_id,
                )
            assert error.value.code == "unresolved_operations"
