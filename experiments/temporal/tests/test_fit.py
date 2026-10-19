"""Observation tests; unsafe engine-only behavior is a measured integration gap."""

import asyncio
import signal
import time
from datetime import timedelta

import pytest
from conftest import Lab
from temporalio.client import WorkflowExecutionStatus, WorkflowFailureError
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError


@pytest.mark.parametrize("server_restart", [False, True])
async def test_completed_activity_survives_restart(lab: Lab, server_restart: bool) -> None:
    first = await lab.worker()
    await lab.start("checkpoint")
    await lab.phase("waiting")
    assert lab.effects("output") == 1
    first.kill()
    first.wait(timeout=5)
    if server_restart:
        lab.server.stop()
        lab.server.start()
        lab.client = await lab.server.connect()
        lab.handle = lab.client.get_workflow_handle(lab.id)
    await lab.send("continue", topic="continue")
    await lab.worker()
    assert await lab.result() == 1
    assert lab.effects("output") == 1


@pytest.mark.parametrize("when", ["before", "during", "offline"])
async def test_signals_and_request_id_deduplication(lab: Lab, when: str) -> None:
    proc = await lab.worker()
    await lab.start("events")
    await lab.phase("waiting")
    if when == "during":
        await lab.send("continue", topic="continue")
        await lab.phase("receiving")
    if when == "offline":
        proc.kill()
        proc.wait(timeout=5)
    await lab.send("unrelated", topic="other")
    await lab.send("first", key="message-one")
    await lab.send("first", key="message-one")
    await lab.send("second", key="message-two")
    await lab.send("end", key="message-three")
    await lab.send("continue", topic="continue")
    if when == "offline":
        await lab.worker()
    assert await lab.result() == ["first", "second", "end"]


async def test_high_level_signals_do_not_deduplicate_equal_payloads(lab: Lab) -> None:
    await lab.worker()
    await lab.start("events")
    await lab.phase("waiting")
    await lab.send("same")
    await lab.send("same")
    await lab.send("end")
    await lab.send("continue", topic="continue")
    assert await lab.result() == ["same", "same", "end"]


async def test_timer_survives_worker_restart(lab: Lab) -> None:
    proc = await lab.worker()
    await lab.start("timer")
    await lab.wait_history("EVENT_TYPE_TIMER_STARTED")
    started = time.monotonic()
    proc.kill()
    proc.wait(timeout=5)
    await asyncio.sleep(3.1)
    await lab.worker()
    assert await lab.result() == "awake"
    assert time.monotonic() - started < 6
    assert lab.effects("output") == 1


async def test_child_result_survives_worker_restart(lab: Lab) -> None:
    proc = await lab.worker()
    await lab.start("child")
    await lab.phase("waiting", child=True)
    proc.kill()
    proc.wait(timeout=5)
    await lab.send("continue", topic="continue", child=True)
    await lab.worker()
    assert await lab.result() == 2
    assert lab.effects("output") == 2


@pytest.mark.parametrize("guarded", [False, True])
async def test_effect_commit_gap_requires_intent(lab: Lab, guarded: bool) -> None:
    proc = await lab.worker()
    await lab.start("external_intent" if guarded else "external_raw")
    await lab.wait_file("effect_committed.entered")
    assert lab.effects("external") == 1
    proc.kill()
    proc.wait(timeout=5)
    lab.release("effect_committed")
    await lab.worker()
    assert await lab.result() == ("outcome_unknown" if guarded else "effect_recorded")
    assert lab.effects("external") == (1 if guarded else 2)


async def test_disabling_retry_avoids_repeat_but_does_not_classify_uncertainty(lab: Lab) -> None:
    proc = await lab.worker()
    await lab.start("external_no_retry")
    await lab.wait_file("effect_committed.entered")
    proc.kill()
    proc.wait(timeout=5)
    await lab.worker()
    with pytest.raises(WorkflowFailureError) as failure:
        await lab.result()
    lab.observations["failure"] = str(failure.value.cause)
    assert lab.effects("external") == 1
    assert lab.handle is not None
    assert (await lab.handle.describe()).status == WorkflowExecutionStatus.FAILED


@pytest.mark.parametrize("fresh", [False, True])
@pytest.mark.parametrize("restart", [False, True])
async def test_revocation_needs_fresh_check(lab: Lab, fresh: bool, restart: bool) -> None:
    proc = await lab.worker()
    await lab.start("auth_fresh" if fresh else "auth_cached")
    await lab.phase("authority")
    if restart:
        proc.kill()
        proc.wait(timeout=5)
    lab.revoke()
    await lab.send("continue", topic="continue")
    if restart:
        await lab.worker()
    assert await lab.result() == ("denied" if fresh else "effect_recorded")
    assert lab.effects("external") == (0 if fresh else 1)


async def test_retry_rechecks_authority(lab: Lab) -> None:
    await lab.worker()
    await lab.start("auth_retry")
    await lab.wait_file("before_retry.entered")
    lab.revoke()
    lab.release("before_retry")
    assert await lab.result() == "denied"
    assert lab.effects("external") == 0


async def test_default_id_reuse_creates_new_run(lab: Lab) -> None:
    await lab.worker()
    await lab.start("checkpoint")
    await lab.send("continue", topic="continue")
    assert await lab.result() == 1
    assert lab.handle is not None
    old_run = lab.handle.result_run_id
    await lab.start("checkpoint", value=99)
    await lab.send("continue", topic="continue")
    assert await lab.result() == 99
    assert lab.handle.result_run_id != old_run
    assert lab.effects("output") == 2


async def test_reject_duplicate_rejects_both_same_and_changed_arguments(lab: Lab) -> None:
    await lab.worker()
    await lab.start("checkpoint")
    await lab.send("continue", topic="continue")
    assert await lab.result() == 1
    for value in (1, 99):
        with pytest.raises(WorkflowAlreadyStartedError):
            await lab.start(
                "checkpoint", value=value, id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE
            )
    assert lab.effects("output") == 1


async def test_use_existing_running_id_does_not_validate_arguments(lab: Lab) -> None:
    await lab.worker()
    await lab.start("checkpoint")
    await lab.phase("waiting")
    await lab.start(
        "checkpoint", value=99, id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING
    )
    await lab.send("continue", topic="continue")
    assert await lab.result() == 1
    assert lab.effects("output") == 1


@pytest.mark.parametrize("scenario", ["cancel", "cancel_inflight", "cancel_cooperative"])
async def test_cancellation_boundaries(lab: Lab, scenario: str) -> None:
    proc = await lab.worker()
    await lab.start(scenario)
    barrier = "inflight" if scenario == "cancel_inflight" else "pause"
    await lab.wait_file(barrier + ".entered")
    assert lab.handle is not None
    await lab.handle.cancel()
    with pytest.raises(WorkflowFailureError):
        await lab.result()
    assert (await lab.handle.describe()).status == WorkflowExecutionStatus.CANCELED
    assert proc.poll() is None
    if scenario == "cancel_cooperative":
        await lab.wait_file("pause.cancelled")
        await lab.wait_file("pause.exited")
    else:
        lab.release(barrier)
        await lab.wait_file(
            f"{proc.pid}.effect" if scenario == "cancel_inflight" else "pause.exited"
        )
    assert lab.effects("external") == (1 if scenario == "cancel_inflight" else 0)


@pytest.mark.parametrize("mode", ["default", "uninterruptible", "fenced"])
async def test_activity_timeout_thread_interruption_and_effect_fencing(lab: Lab, mode: str) -> None:
    old = await lab.worker()
    scenario = {
        "default": "overlap_default",
        "uninterruptible": "overlap_inflight",
        "fenced": "overlap_fenced",
    }[mode]
    await lab.start(scenario)
    await lab.wait_file(f"inflight.{old.pid}.1.entered")
    old.send_signal(signal.SIGSTOP)
    lab.replace_owner()
    new = await lab.worker(generation=2)
    # Real server-side start-to-close expiry dispatches attempt 2 to a distinct worker.
    await lab.wait_file(f"inflight.{new.pid}.2.entered")
    lab.release(f"inflight.{new.pid}")
    assert await lab.result() == "effect_recorded"
    old.send_signal(signal.SIGCONT)
    if mode == "default":
        # Keep the old Python barrier closed so the SDK has an interruptible boundary.
        await lab.wait_file(f"{old.pid}.cancelled")
    else:
        lab.release(f"inflight.{old.pid}")
        await lab.wait_file(f"{old.pid}.denied" if mode == "fenced" else f"{old.pid}.effect")
    lab.observations["thread_cancellation_mode"] = mode
    assert lab.effects("external") == (2 if mode == "uninterruptible" else 1)


async def test_execution_timeout_includes_queue_time(lab: Lab) -> None:
    await lab.start("checkpoint", execution_timeout=timedelta(seconds=2))
    with pytest.raises(WorkflowFailureError):
        await lab.result()
    await lab.worker()
    assert lab.handle is not None
    assert (await lab.handle.describe()).status == WorkflowExecutionStatus.TIMED_OUT
    assert lab.effects("output") == 0
