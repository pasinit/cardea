"""Observable behavior of pinned Absurd, including explicit integration gaps."""

import json
import signal
import time

import pytest
from absurd_sdk import Absurd
from conftest import Lab


@pytest.mark.parametrize("database_restart", [False, True])
def test_completed_step_survives_restart(lab: Lab, database_restart: bool) -> None:
    lab.worker()
    lab.spawn("checkpoint")
    lab.wait_wait("continue")
    assert lab.effects("output") == 1
    lab.stop_workers()
    if database_restart:
        lab.client.close()
        lab.pg.stop()
        lab.pg.start()
        lab.client = Absurd(lab.url)
    lab.client.emit_event("continue", True)
    lab.worker()
    assert lab.result() == 1
    assert lab.effects("output") == 1


@pytest.mark.parametrize("when", ["before", "during", "offline"])
def test_named_events_around_wait_and_first_write_wins(lab: Lab, when: str) -> None:
    lab.worker()
    lab.spawn("events")
    lab.wait_wait("gate")
    if when == "during":
        lab.client.emit_event("gate", True)
        lab.wait_wait("input-one")
    if when == "offline":
        lab.stop_workers()
    lab.client.emit_event("unrelated", "ignore")
    lab.client.emit_event("input-one", "first")
    lab.client.emit_event("input-one", "changed")
    lab.client.emit_event("input-two", "second")
    lab.client.emit_event("gate", True)
    if when == "offline":
        lab.worker()
    assert lab.result() == ["first", "second"]


def test_repeated_wait_on_same_event_reads_same_cached_payload(lab: Lab) -> None:
    lab.client.emit_event("input", "first")
    lab.client.emit_event("input", "second")
    lab.worker()
    lab.spawn("repeat_event")
    assert lab.result() == ["first", "first"]


def test_event_name_is_shared_across_tasks_in_one_queue(lab: Lab) -> None:
    lab.worker()
    lab.client.emit_event("input", "first-task-data")
    first = lab.spawn("repeat_event")
    assert lab.result() == ["first-task-data", "first-task-data"]
    lab.client.emit_event("input", "second-task-data")
    second = lab.spawn("repeat_event")
    assert second["task_id"] != first["task_id"]
    assert lab.result() == ["first-task-data", "first-task-data"]
    assert lab.effects("output") == 2


def test_timer_survives_restart(lab: Lab) -> None:
    lab.worker()
    lab.spawn("timer")
    lab.wait_state("sleeping")
    started = time.monotonic()
    lab.stop_workers()
    time.sleep(3.1)
    lab.worker()
    assert lab.result() == "awake"
    assert time.monotonic() - started < 6
    assert lab.effects("output") == 1


def test_child_result_survives_restart_with_separate_queue(lab: Lab) -> None:
    lab.worker()
    lab.worker(queue="children")
    lab.spawn("child")
    lab.wait_wait("continue", child=True)
    lab.stop_workers()
    lab.client.emit_event("continue", True, queue_name="children")
    lab.worker()
    lab.worker(queue="children")
    assert lab.result() == {"child_state": "completed", "result": 2}
    assert lab.effects("output") == 2


@pytest.mark.parametrize("guarded", [False, True])
def test_external_commit_gap_requires_broker_intent(lab: Lab, guarded: bool) -> None:
    lab.worker()
    lab.spawn("external_intent" if guarded else "external_raw", max_attempts=2)
    lab.wait_file("effect_committed.entered")
    assert lab.effects("external") == 1
    lab.stop_workers()
    lab.release("effect_committed")
    lab.worker()
    assert lab.result() == ("outcome_unknown" if guarded else "effect_recorded")
    assert lab.effects("external") == (1 if guarded else 2)


def test_single_attempt_does_not_repeat_but_needs_uncertainty_mapping(lab: Lab) -> None:
    lab.worker()
    lab.spawn("external_no_retry", max_attempts=1)
    lab.wait_file("effect_committed.entered")
    lab.stop_workers()
    lab.worker()
    lab.wait_state("failed")
    assert lab.effects("external") == 1


@pytest.mark.parametrize("fresh", [False, True])
@pytest.mark.parametrize("restart", [False, True])
def test_revocation_needs_fresh_check(lab: Lab, fresh: bool, restart: bool) -> None:
    lab.worker()
    lab.spawn("auth_fresh" if fresh else "auth_cached")
    lab.wait_wait("continue")
    if restart:
        lab.stop_workers()
    lab.revoke()
    lab.client.emit_event("continue", True)
    if restart:
        lab.worker()
    assert lab.result() == ("denied" if fresh else "effect_recorded")
    assert lab.effects("external") == (0 if fresh else 1)


def test_retry_rechecks_current_authority(lab: Lab) -> None:
    lab.worker()
    lab.spawn("auth_retry", max_attempts=2)
    lab.wait_file("before_retry.entered")
    lab.revoke()
    lab.release("before_retry")
    assert lab.result() == "denied"
    assert lab.effects("external") == 0


def test_idempotency_key_does_not_check_changed_arguments(lab: Lab) -> None:
    lab.worker()
    first = lab.spawn("checkpoint", idempotency_key="stable-operation")
    lab.observations["spawn_id_runtime_type"] = type(first["task_id"]).__name__
    lab.wait_wait("continue")
    running_reuse = lab.spawn("checkpoint", value=99, idempotency_key="stable-operation")
    assert running_reuse["task_id"] == first["task_id"]
    lab.client.emit_event("continue", True)
    assert lab.result() == 1
    for value in (1, 99):
        reused = lab.spawn("checkpoint", value=value, idempotency_key="stable-operation")
        assert reused["task_id"] == first["task_id"]
        assert lab.result() == 1
    assert lab.effects("output") == 1


@pytest.mark.parametrize("scenario", ["cancel", "cancel_cooperative", "cancel_inflight"])
def test_cancellation_boundaries(lab: Lab, scenario: str) -> None:
    proc = lab.worker()
    task = lab.spawn(scenario)
    barrier = "inflight" if scenario == "cancel_inflight" else "pause"
    lab.wait_file(barrier + ".entered")
    lab.client.cancel_task(task["task_id"])
    lab.wait_state("cancelled")
    assert proc.poll() is None
    if scenario == "cancel_cooperative":
        lab.wait_file("heartbeat.cancelled")
    else:
        lab.release(barrier)
    lab.wait_file(f"{proc.pid}.handler_exited")
    assert lab.effects("external") == (1 if scenario == "cancel_inflight" else 0)


@pytest.mark.parametrize("scenario", ["overlap_boundary", "overlap_inflight", "overlap_fenced"])
def test_expired_claim_fences_checkpoints_but_not_inflight_effects(lab: Lab, scenario: str) -> None:
    old = lab.worker()
    lab.spawn(scenario, max_attempts=2)
    barrier = "pause" if scenario == "overlap_boundary" else "inflight"
    lab.wait_file(f"{barrier}.{old.pid}.entered")
    old.send_signal(signal.SIGSTOP)
    lab.replace_owner()
    new = lab.worker(generation=2)
    lab.wait_file(f"{barrier}.{new.pid}.entered")
    lab.release(f"{barrier}.{new.pid}")
    assert lab.result() == "effect_recorded"
    old.send_signal(signal.SIGCONT)
    lab.release(f"{barrier}.{old.pid}")
    lab.wait_file(f"{old.pid}.handler_exited")
    assert lab.effects("external") == (2 if scenario == "overlap_inflight" else 1)
    assert lab.snapshot().result == "effect_recorded"
    if scenario == "overlap_fenced":
        lab.wait_file(f"{old.pid}.denied")


def test_queued_delay_limit_is_enforced_when_worker_claims(lab: Lab) -> None:
    lab.spawn("checkpoint", cancellation={"max_delay": 2})
    time.sleep(2.2)
    lab.observations["state_before_worker"] = lab.snapshot().state
    lab.worker()
    lab.wait_state("cancelled")
    assert lab.effects("output") == 0


def test_explicit_retry_can_reopen_a_failed_task_id(lab: Lab) -> None:
    lab.worker()
    task = lab.spawn("auth_retry", max_attempts=1)
    lab.wait_file("before_retry.entered")
    lab.release("before_retry")
    lab.wait_state("failed")
    retried = lab.client.retry_task(task["task_id"], max_attempts=2)
    assert retried["task_id"] == task["task_id"]
    assert lab.result() == "effect_recorded"
    assert lab.effects("output") == 1  # Existing checkpoints reused under the same task.
    assert lab.effects("external") == 1
    lab.observations["explicit_retry"] = json.loads(json.dumps(retried, default=str))
