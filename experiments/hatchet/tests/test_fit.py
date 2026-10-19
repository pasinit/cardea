"""Record engine behavior independently of fictional external effect counts."""

import pytest
from conftest import Lab


def test_child_result(lab: Lab) -> None:
    lab.worker()
    lab.start("smoke")
    assert lab.result() == {"value": 1}
    assert lab.effects("output") == 1


def test_completed_child_survives_worker_death(lab: Lab) -> None:
    lab.worker()
    lab.start()
    lab.wait_file("checkpoint.saved")
    lab.wait_registration()
    lab.close()
    lab.emit()
    lab.worker()
    assert lab.result(timeout=100) == {"value": 1}
    assert lab.effects("output") == 1


@pytest.mark.parametrize("when", ["before", "during", "offline"])
def test_events_with_scoped_lookback(lab: Lab, when: str) -> None:
    lab.worker()
    if when == "before":
        lab.emit()
        lab.emit("second", "second")
    lab.start("events")
    lab.wait_file("checkpoint.saved")
    if when != "before":
        lab.wait_registration()
    if when == "offline":
        lab.close()
    if when != "before":
        lab.emit(scope="unrelated")
        lab.emit()
        lab.emit("second", "second")
    if when == "offline":
        lab.worker()
    assert lab.result(timeout=100) == {"events": [{"value": "first"}, {"value": "second"}]}
    assert lab.effects("output") == 1


def test_timer_survives_worker_death(lab: Lab) -> None:
    lab.worker()
    lab.start("timer")
    lab.wait_file("timer.entered")
    lab.wait_registration("SLEEP")
    lab.close()
    lab.worker()
    assert lab.result(timeout=100) == {"status": "awake"}
    assert lab.effects("output") == 1


@pytest.mark.parametrize("fresh", [False, True])
@pytest.mark.parametrize("restart", [False, True])
def test_live_revocation(lab: Lab, fresh: bool, restart: bool) -> None:
    lab.worker()
    lab.start("auth_fresh" if fresh else "auth_cached")
    lab.wait_file("permission.saved")
    lab.wait_registration()
    if restart:
        lab.close()
    lab.revoke()
    lab.emit()
    if restart:
        lab.worker()
    assert lab.result(timeout=100) == {"status": "denied" if fresh else "effect_recorded"}
    assert lab.effects("external") == (0 if fresh else 1)


def test_retry_rechecks_authority(lab: Lab) -> None:
    lab.worker()
    lab.start("auth_retry")
    lab.wait_file("before_retry.entered")
    lab.revoke()
    lab.release("before_retry")
    assert lab.result() == {"status": "denied"}
    assert lab.effects("external") == 0


@pytest.mark.parametrize("intent", [False, True])
@pytest.mark.parametrize("crash", [False, True])
def test_lost_effect_result_on_retry(lab: Lab, intent: bool, crash: bool) -> None:
    lab.worker()
    lab.start("external_intent" if intent else "external_raw")
    lab.wait_file("effect_committed.entered")
    assert lab.effects("external") == 1
    if crash:
        lab.close()
    lab.release("effect_committed")
    if crash:
        lab.worker()
    assert lab.result(timeout=100) == {"status": "outcome_unknown" if intent else "effect_recorded"}
    assert lab.effects("external") == (1 if intent else 2)


def test_idempotency_collision_does_not_compare_arguments(lab: Lab) -> None:
    from hatchet_sdk.exceptions import IdempotencyCollisionError

    lab.worker()
    first = lab.start()
    lab.wait_file("checkpoint.saved")
    for value in (1, 99):
        with pytest.raises(IdempotencyCollisionError) as error:
            lab.start(value=value)
        assert error.value.existing_run_external_id == first
    lab.emit()
    assert lab.result() == {"value": 1}
    for value in (1, 99):
        with pytest.raises(IdempotencyCollisionError) as error:
            lab.start(value=value)
        assert error.value.existing_run_external_id == first
    assert lab.effects("output") == 1
    lab.observations["collision"] = "same and changed input both rejected, original ID returned"


def test_repeated_scoped_lookback_can_read_same_event(lab: Lab) -> None:
    lab.worker()
    lab.emit()
    lab.start("repeat_event")
    assert lab.result() == {"events": [{"value": "first"}, {"value": "first"}]}


def test_cancelled_parent_does_not_stop_inflight_sync_child(lab: Lab) -> None:
    lab.worker()
    run = lab.start("cancel_inflight")
    lab.wait_file("inflight.entered")
    lab.hatchet.runs.cancel(run)
    lab.wait_state("CANCELLED")
    lab.release("inflight")
    lab.wait_file("effect.exited")
    assert lab.effects("external") == 1
