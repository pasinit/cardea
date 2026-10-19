"""Crash and denial assertions against real Cardea records, not engine success flags."""

import json
import signal
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

import pytest
from cardea.kernel.lifecycle import ContractError
from cardea.kernel.models import RetryClass
from cardea.kernel.store import SQLiteKernelStore, StoreError
from conftest import Lab

from adapter import QUEUE, TASK_ID, WORKFLOW_ID, admit, borrow, engine, request, task


def test_shared_store_smoke(lab: Lab) -> None:
    lab.submit()
    assert lab.result(lab.worker()) == "succeeded"
    assert lab.task_state() == "succeeded"
    assert lab.query("SELECT COUNT(*) FROM operations WHERE state='succeeded'") == [(1,)]
    assert lab.query("SELECT COUNT(*) FROM objects WHERE resource_id='output'") == [(1,)]


@pytest.mark.parametrize(
    "stage,committed",
    [
        ("admission.written", False),
        ("enqueue.written", False),
        ("admission.before_commit", False),
        ("admission.after_commit", True),
    ],
)
def test_atomic_admission_and_enqueue(lab: Lab, stage: str, committed: bool) -> None:
    proc = lab.submit(stage)
    lab.kill(proc)
    assert lab.query("SELECT COUNT(*) FROM tasks") == [(int(committed),)]
    assert len(lab.client.list_workflows(workflow_ids=[WORKFLOW_ID])) == int(committed)
    assert lab.query("SELECT COUNT(*) FROM events WHERE event_type='task.submitted'") == [
        (int(committed),)
    ]
    if not committed:
        lab.submit()
    assert lab.result(lab.worker()) == "succeeded"
    assert lab.query("SELECT COUNT(*) FROM tasks") == [(1,)]


@pytest.mark.parametrize("engine_first", [False, True])
def test_separate_commits_expose_both_gaps(
    tmp_path: Path, engine_first: bool, record_property: Callable[[str, object], None]
) -> None:
    split = Lab(tmp_path, separate=True)
    try:
        stage = "separate.engine_committed" if engine_first else "separate.cardea_committed"
        proc = split.submit(stage, engine_first=engine_first)
        split.kill(proc)
        assert split.query("SELECT COUNT(*) FROM tasks") == [(int(not engine_first),)]
        assert len(split.client.list_workflows(workflow_ids=[WORKFLOW_ID])) == int(engine_first)
        record_property("observations", json.dumps(split.snapshot()))
    finally:
        split.close()


@pytest.mark.parametrize(
    "stage,visible",
    [
        ("publish.before_commit", 0),
        ("publish.after_commit", 1),
    ],
)
def test_datasource_commit_and_checkpoint_replay(lab: Lab, stage: str, visible: int) -> None:
    lab.submit()
    proc = lab.worker(stage)
    lab.kill(proc)
    assert lab.query("SELECT COUNT(*) FROM objects WHERE resource_id='output'") == [(visible,)]
    assert lab.query("SELECT COUNT(*) FROM datasource_outputs") == [(visible,)]
    lab.observations["before_restart"] = lab.snapshot()
    assert lab.result(lab.worker()) == "succeeded"
    assert lab.query("SELECT COUNT(*) FROM objects WHERE resource_id='output'") == [(1,)]
    assert lab.query("SELECT COUNT(*) FROM operations WHERE state='succeeded'") == [(1,)]
    assert lab.query("SELECT COUNT(*) FROM events WHERE event_type='object.written'") == [(2,)]


@pytest.mark.parametrize("stage", ["finish.before_commit", "finish.after_commit"])
def test_terminal_commit_recovery(lab: Lab, stage: str) -> None:
    lab.submit()
    proc = lab.worker(stage)
    lab.kill(proc)
    assert lab.task_state() == ("succeeded" if stage.endswith("after_commit") else "running")
    lab.observations["before_restart"] = lab.snapshot()
    assert lab.result(lab.worker()) == "succeeded"
    assert lab.query("SELECT COUNT(*) FROM objects WHERE resource_id='verification'") == [(1,)]
    with SQLiteKernelStore(lab.root) as store, store.transaction(write=False) as tx:
        final = tx.get_task(TASK_ID)
        assert final.verification is not None and final.verification.passed
        assert tx.read_object(final.verification.artifacts[0]) == b'{"value":1}'


@pytest.mark.parametrize(
    "stage,effects,final",
    [
        ("intent.after_commit", 0, "failed"),
        ("effect.after_commit", 1, "failed"),
        ("receipt.after_commit", 1, "succeeded"),
    ],
)
def test_external_effect_gap_is_not_blindly_retried(
    lab: Lab, stage: str, effects: int, final: str
) -> None:
    lab.submit(scenario="external")
    proc = lab.worker(stage)
    lab.kill(proc)
    assert lab.snapshot()["provider_effects"] == effects
    assert lab.result(lab.worker()) == final
    snapshot = lab.snapshot()
    assert snapshot["provider_effects"] == effects
    assert snapshot["operations"][0][0] == (
        "succeeded" if final == "succeeded" else "outcome_unknown"
    )


def test_conflicting_admission_and_operation_receipt(lab: Lab) -> None:
    lab.submit()
    assert admit(lab.root) == WORKFLOW_ID
    with pytest.raises(ContractError) as error:
        admit(lab.root, value=99)
    assert error.value.code == "request_conflict"
    assert lab.result(lab.worker()) == "succeeded"
    with SQLiteKernelStore(lab.root) as store:
        with store.transaction() as tx:
            original = tx.get_operation(TASK_ID, "write-output")
            same = tx.admit_operation(
                request(),
                grant_ids=("fictional-grant",),
                retry_class=RetryClass.IDEMPOTENT,
                expected_task_revision=0,
                lease_id="unused",
                at=original.updated_at,
            )
            assert same == original
        with pytest.raises(ContractError) as error, store.transaction() as tx:
            tx.admit_operation(
                request(99),
                grant_ids=("fictional-grant",),
                retry_class=RetryClass.IDEMPOTENT,
                expected_task_revision=0,
                lease_id="unused",
                at=original.updated_at,
            )
        assert error.value.code == "request_conflict"


def test_bad_artifact_rejected_even_when_engine_finishes(lab: Lab) -> None:
    lab.submit(scenario="bad_artifact")
    assert lab.result(lab.worker()) == "failed"
    assert lab.snapshot()["engine_status"] == "SUCCESS"
    with SQLiteKernelStore(lab.root) as store, store.transaction(write=False) as tx:
        task = tx.get_task(TASK_ID)
        assert task.failure is not None and task.failure.code == "verification_failed"
        assert task.verification is None


@pytest.mark.parametrize(
    "field",
    [
        "supervisor_enabled",
        "supervisor_permission",
        "supervisor_delegation",
        "ancestor",
        "task_grant",
        "platform",
    ],
)
def test_resume_rechecks_fictional_ceiling(lab: Lab, field: str) -> None:
    lab.submit()
    old = lab.worker("broker.before_effect")
    lab.kill(old)
    lab.revoke(field)
    assert lab.result(lab.worker()) == "failed"
    assert lab.query("SELECT COUNT(*) FROM objects") == [(0,)]
    assert lab.query("SELECT COUNT(*) FROM operations") == [(0,)]


def test_mutation_failure_cannot_commit_borrowed_transaction(lab: Lab) -> None:
    application = engine(lab.root)
    try:
        with (
            pytest.raises(StoreError, match="caught mutation"),
            application.begin() as conn,
            borrow(conn) as tx,
        ):
            tx.submit_task(task())
            with suppress(StoreError):
                tx.submit_task(task())
        assert lab.query("SELECT COUNT(*) FROM tasks") == [(0,)]
        assert lab.query("SELECT COUNT(*) FROM events") == [(0,)]
        with application.begin() as conn, borrow(conn) as tx:
            pass
        with pytest.raises(StoreError):
            tx.get_task(TASK_ID)
    finally:
        application.dispose()


def test_distinct_executor_takeover_fences_old_broker(lab: Lab) -> None:
    lab.submit()
    old = lab.worker("broker.before_effect", executor="executor-a")
    old.send_signal(signal.SIGSTOP)
    time.sleep(3.1)
    lab.client.resume_workflow(WORKFLOW_ID, queue_name=QUEUE)
    assert lab.result(lab.worker(executor="executor-b")) == "succeeded"
    old.send_signal(signal.SIGCONT)
    (lab.root / f"{old.pid}.broker.before_effect.release").touch()
    lab.wait_file(f"{old.pid}.denied")
    assert (lab.root / f"{old.pid}.denied").read_text() == "stale_lease"
    assert lab.query("SELECT COUNT(*) FROM objects WHERE resource_id='output'") == [(1,)]
    assert lab.task_state() == "succeeded"
    old.wait(timeout=15)
    assert lab.snapshot()["engine_status"] == "SUCCESS"
    lab.observations["takeover"] = (
        "explicit public resume to executor-b after lease expiry; old broker denied"
    )


@pytest.mark.parametrize("retry", [False, True])
def test_revocation_at_new_dispatch_and_retry(lab: Lab, retry: bool) -> None:
    lab.submit(scenario="retry" if retry else "local")
    stage = "retry.before_failure" if retry else "broker.before_effect"
    proc = lab.worker(stage)
    lab.revoke()
    (lab.root / f"{proc.pid}.{stage}.release").touch()
    assert lab.result(proc) == "failed"
    assert lab.snapshot()["provider_effects"] == 0
    assert lab.query("SELECT COUNT(*) FROM operations") == [(0,)]


def test_engine_resume_cannot_reopen_cancelled_cardea_task(lab: Lab) -> None:
    from cardea.kernel.models import TaskState

    from adapter import now

    lab.submit()
    with SQLiteKernelStore(lab.root) as store, store.transaction() as tx:
        tx.change_task(TASK_ID, TaskState.CANCELLED, expected_revision=0, at=now())
    lab.client.cancel_workflow(WORKFLOW_ID)
    lab.client.resume_workflow(WORKFLOW_ID, queue_name=QUEUE)
    assert lab.result(lab.worker()) == "cancelled"
    assert lab.query("SELECT COUNT(*) FROM objects") == [(0,)]
    assert lab.task_state() == "cancelled"


def test_existing_cardea_schema_and_data_survive_bootstrap(tmp_path: Path) -> None:
    from cardea.kernel.demo import resume_demo, start_demo
    from cardea.kernel.store import APPLICATION_ID, STORAGE_VERSION

    from adapter import now

    with SQLiteKernelStore(tmp_path) as store:
        start_demo(store, task_id="existing-task", at=now())
        saved = resume_demo(store, task_id="existing-task", at=now())
        with store.transaction(write=False) as tx:
            events = tx.read_events()
    combined = Lab(tmp_path)
    try:
        with SQLiteKernelStore(tmp_path) as store, store.transaction(write=False) as tx:
            assert tx.get_task("existing-task") == saved
            assert tx.read_events() == events
            assert saved.verification is not None
            for artifact in saved.verification.artifacts:
                tx.read_object(artifact)
        assert combined.query("PRAGMA application_id") == [(APPLICATION_ID,)]
        assert combined.query("PRAGMA user_version") == [(STORAGE_VERSION,)]
        combined.submit()
        assert combined.result(combined.worker()) == "succeeded"
    finally:
        combined.close()
