"""Assert observed behavior, including explicit limitations, for pinned DBOS 3.0.0."""

import signal
import time

import pytest
from conftest import Lab


def test_completed_step_survives_hard_restart(lab: Lab) -> None:
    first = lab.start("checkpoint")
    lab.wait_file("checkpoint.entered")
    assert lab.effects("output") == 1
    first.kill()
    first.wait(timeout=5)
    lab.release("checkpoint")
    second = lab.start("checkpoint", mode="recover")
    assert lab.result(second) == 1
    assert lab.effects("output") == 1


@pytest.mark.parametrize("when", ["before", "during", "offline"])
def test_durable_messages_and_explicit_deduplication(lab: Lab, when: str) -> None:
    proc = lab.start("events")
    lab.wait_file("before_recv.entered")
    if when == "during":
        lab.release("before_recv")
        assert lab.event("receiving") is True
    if when == "offline":
        proc.kill()
        proc.wait(timeout=5)
    lab.send("unrelated", topic="other")
    lab.send("first", key="message-one")
    lab.send("first", key="message-one")
    lab.send("second", key="message-two")
    lab.release("before_recv")
    if when == "offline":
        proc = lab.start("events", mode="recover")
    assert lab.result(proc) == ["first", "second"]


def test_timer_survives_hard_restart(lab: Lab) -> None:
    first = lab.start("timer")
    assert lab.event("sleeping") is True
    # Wait for the sleep operation to be journaled, not only the preceding event.
    deadline = time.monotonic() + 10
    while not any(
        "sleep" in str(step).lower() for step in lab.connect().list_workflow_steps("fit-task")
    ):
        assert time.monotonic() < deadline
        time.sleep(0.03)
    started = time.monotonic()
    first.kill()
    first.wait(timeout=5)
    time.sleep(3.1)
    second = lab.start("timer", mode="recover")
    assert lab.result(second) == "awake"
    assert time.monotonic() - started < 6  # A fresh full sleep would exceed this.
    assert lab.effects("output") == 1


@pytest.mark.parametrize("guarded", [False, True])
def test_effect_commit_gap_requires_broker_intent(lab: Lab, guarded: bool) -> None:
    scenario = "external_intent" if guarded else "external_raw"
    first = lab.start(scenario)
    lab.wait_file("effect_committed.entered")
    assert lab.effects("external") == 1
    first.kill()
    first.wait(timeout=5)
    lab.release("effect_committed")
    second = lab.start(scenario, mode="recover")
    assert lab.result(second) == ("outcome_unknown" if guarded else "effect_recorded")
    # The raw case is a measured limitation, NOT acceptable Cardea behavior.
    assert lab.effects("external") == (1 if guarded else 2)


@pytest.mark.parametrize("fresh", [False, True])
@pytest.mark.parametrize("restart", [False, True])
def test_revocation_needs_fresh_check_at_effect(lab: Lab, fresh: bool, restart: bool) -> None:
    scenario = "auth_fresh" if fresh else "auth_cached"
    proc = lab.start(scenario)
    assert lab.event("waiting_for_authority_test") is True
    if restart:
        proc.kill()
        proc.wait(timeout=5)
    lab.revoke()
    lab.send("continue", topic="continue")
    if restart:
        proc = lab.start(scenario, mode="recover")
    assert lab.result(proc) == ("denied" if fresh else "effect_recorded")
    # A cached successful permission step is stale after revocation.
    assert lab.effects("external") == (0 if fresh else 1)


def test_retry_rechecks_current_authority(lab: Lab) -> None:
    proc = lab.start("auth_retry")
    lab.wait_file("before_retry.entered")
    lab.revoke()
    lab.release("before_retry")
    assert lab.result(proc) == "denied"
    assert lab.effects("external") == 0


def test_reused_workflow_id_does_not_validate_changed_arguments(lab: Lab) -> None:
    first = lab.start("checkpoint")
    lab.wait_file("checkpoint.entered")
    lab.release("checkpoint")
    assert lab.result(first) == 1
    second = lab.start("checkpoint", value=99)
    assert lab.result(second) == 1
    assert lab.effects("output") == 1  # Cardea must add its request-digest conflict check.


def test_cancellation_does_not_preempt_a_running_sync_step(lab: Lab) -> None:
    proc = lab.start("cancel")
    lab.wait_file("cancel.entered")
    lab.connect().cancel_workflow("fit-task")
    assert lab.connect().retrieve_workflow("fit-task").get_status().status == "CANCELLED"
    assert proc.poll() is None
    lab.release("cancel")
    deadline = time.monotonic() + 10
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert lab.effects("external") == 0


@pytest.mark.parametrize("scenario", ["overlap", "overlap_inflight", "overlap_fenced"])
def test_replacement_fences_step_boundary_but_not_inflight_external_code(
    lab: Lab, scenario: str
) -> None:
    old = lab.start(scenario)
    checkpoint = "overlap" if scenario == "overlap" else "inflight"
    lab.wait_file(checkpoint + ".entered")
    old.send_signal(signal.SIGSTOP)
    lab.replace_owner()
    new = lab.start(scenario, mode="recover", generation=2)
    lab.wait_file(f"{new.pid}.launched")
    # This deliberately simulates premature replacement with the same executor
    # identity. It is not a supported two-live-executor configuration or a lease
    # expiry test. It distinguishes engine checkpoint fencing from external I/O.
    time.sleep(0.5)
    lab.release(checkpoint)
    assert lab.result(new) == "effect_recorded"
    old.send_signal(signal.SIGCONT)
    assert lab.result(old) == "effect_recorded"  # Adopts winning recorded outcome.
    assert lab.effects("external") == (2 if scenario == "overlap_inflight" else 1)


def test_child_result_survives_restart(lab: Lab) -> None:
    first = lab.start("child")
    lab.wait_file("child.entered")
    first.kill()
    first.wait(timeout=5)
    lab.release("child")
    second = lab.start("child", mode="recover")
    assert lab.result(second) == 2
    assert lab.effects("output") == 2  # One parent output and one child output.


def test_cancelled_status_does_not_stop_an_inflight_external_effect(lab: Lab) -> None:
    lab.start("cancel_inflight")
    lab.wait_file("inflight.entered")
    lab.connect().cancel_workflow("fit-task")
    assert lab.connect().retrieve_workflow("fit-task").get_status().status == "CANCELLED"
    lab.release("inflight")
    deadline = time.monotonic() + 10
    while lab.effects("external") != 1 and time.monotonic() < deadline:
        time.sleep(0.03)
    assert lab.effects("external") == 1  # Measured gap; host/broker still required.
