import json
from datetime import datetime, timedelta
from pathlib import Path

from cardea.kernel.demo import resume_demo, start_demo
from cardea.kernel.models import DispatchLease, ResourceRef, TaskState
from cardea.kernel.smoke import run_smoke
from cardea.kernel.store import SQLiteKernelStore


def test_demo_runs_across_two_processes_without_a_model() -> None:
    result = run_smoke()
    assert result["first_process"] == "waiting"
    assert result["second_process"] == "succeeded"
    assert result["checkpoint_preserved"] is True
    assert result["model_calls"] == 0


def test_resuming_completed_demo_does_not_repeat_writes(tmp_path: Path, now: datetime) -> None:
    directory = tmp_path / "state"
    with SQLiteKernelStore(directory) as store:
        start_demo(store, task_id="demo", at=now)
    with SQLiteKernelStore(directory) as store:
        completed = resume_demo(store, task_id="demo", at=now + timedelta(seconds=1))
        with store.transaction(write=False) as transaction:
            cursor = transaction.latest_cursor()
        assert resume_demo(store, task_id="demo", at=now + timedelta(seconds=2)) == completed
        with store.transaction(write=False) as transaction:
            assert transaction.latest_cursor() == cursor
            assert completed.verification is not None
            content = transaction.read_object(completed.verification.artifacts[0])
            assert json.loads(content) == {"incident-1": "low", "incident-2": "high"}


def test_verifier_rejects_output_changed_after_checkpoint(tmp_path: Path, now: datetime) -> None:
    with SQLiteKernelStore(tmp_path / "state") as store:
        start_demo(store, task_id="demo", at=now)
        with store.transaction() as transaction:
            transaction.put_object(
                ResourceRef(namespace="demo", resource_id="index"),
                b'{"wrong":"output"}',
                expected_version=1,
                producer_id="test-admin",
                at=now + timedelta(seconds=1),
            )
        result = resume_demo(store, task_id="demo", at=now + timedelta(seconds=2))
        assert result.state is TaskState.FAILED
        assert result.failure is not None and result.failure.code == "verification_failed"
        assert result.verification is None


def test_expired_running_task_recovers_its_previous_checkpoint(
    tmp_path: Path,
    now: datetime,
) -> None:
    directory = tmp_path / "state"
    with SQLiteKernelStore(directory) as store:
        waiting = start_demo(store, task_id="demo", at=now)
        with store.transaction() as transaction:
            transaction.append_event(
                namespace="demo",
                event_type="demo.resume",
                producer_id="test-supervisor",
                correlation_id="demo",
                at=now + timedelta(seconds=1),
            )
            ready = transaction.wake_task(
                "demo",
                expected_revision=waiting.revision,
                at=now + timedelta(seconds=1),
            )
            running = transaction.change_task(
                "demo",
                TaskState.RUNNING,
                expected_revision=ready.revision,
                at=now + timedelta(seconds=1),
                lease=DispatchLease(
                    lease_id="interrupted-worker",
                    worker_id="worker",
                    acquired_at=now + timedelta(seconds=1),
                    expires_at=now + timedelta(seconds=2),
                ),
            )
    with SQLiteKernelStore(directory) as store:
        with store.transaction() as transaction:
            recovered = transaction.recover_task(
                "demo",
                expected_revision=running.revision,
                at=now + timedelta(seconds=3),
            )
            assert recovered.state is TaskState.QUEUED
            assert recovered.checkpoint == waiting.checkpoint
        completed = resume_demo(store, task_id="demo", at=now + timedelta(seconds=4))
        assert completed.state is TaskState.SUCCEEDED
        assert completed.checkpoint == waiting.checkpoint
