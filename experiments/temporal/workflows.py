"""Deterministic Temporal fixtures using native activities, signals and timers."""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from activities import (
        external_effect,
        output,
        pause,
        permission_snapshot,
        retry_effect,
        uninterruptible_effect,
    )


@workflow.defn
class FitWorkflow:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.proceed = False
        self.phase = "starting"

    @workflow.signal
    def input(self, value: str, topic: str) -> None:
        if topic == "input":
            self.messages.append(value)
        elif topic == "continue":
            self.proceed = True

    @workflow.query
    def state(self) -> str:
        return self.phase

    @workflow.run
    async def run(self, root: str, scenario: str, value: int = 1) -> int | str | list[str]:
        saved = await workflow.execute_activity(
            output,
            args=[root, value],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        self.phase = "waiting"
        if scenario in {"checkpoint", "child"}:
            if scenario == "child":
                return await workflow.execute_child_workflow(
                    FitWorkflow.run,
                    args=[root, "checkpoint", 2],
                    id=workflow.info().workflow_id + "-child",
                )
            await workflow.wait_condition(lambda: self.proceed)
            return saved
        if scenario == "events":
            await workflow.wait_condition(lambda: self.proceed)
            self.phase = "receiving"
            await workflow.wait_condition(lambda: len(self.messages) >= 3)
            return self.messages
        if scenario == "timer":
            self.phase = "sleeping"
            await workflow.sleep(3)
            return "awake"
        retry = RetryPolicy(
            initial_interval=timedelta(milliseconds=200),
            maximum_interval=timedelta(milliseconds=200),
            maximum_attempts=2,
        )
        if scenario in {"auth_cached", "auth_fresh"}:
            approved = await workflow.execute_activity(
                permission_snapshot,
                root,
                start_to_close_timeout=timedelta(seconds=30),
            )
            self.phase = "authority"
            await workflow.wait_condition(lambda: self.proceed)
            if not approved:
                return "denied"
            return await workflow.execute_activity(
                external_effect,
                args=[root, "fresh" if scenario == "auth_fresh" else "cached"],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=retry,
            )
        if scenario == "auth_retry":
            return await workflow.execute_activity(
                retry_effect,
                root,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=retry,
            )
        if scenario in {"cancel", "cancel_cooperative"}:
            await workflow.execute_activity(
                pause,
                args=[root, scenario == "cancel_cooperative"],
                start_to_close_timeout=timedelta(seconds=30),
                heartbeat_timeout=timedelta(seconds=2)
                if scenario == "cancel_cooperative"
                else None,
                retry_policy=retry,
            )
            return await workflow.execute_activity(
                external_effect,
                args=[root, "cached"],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=retry,
            )
        guards = {
            "external_raw": "none",
            "external_intent": "intent",
            "external_no_retry": "none",
            "overlap_inflight": "overlap",
            "overlap_default": "overlap",
            "overlap_fenced": "fenced",
            "cancel_inflight": "blocked",
        }
        guard = guards[scenario]
        return await workflow.execute_activity(
            uninterruptible_effect
            if scenario in {"overlap_inflight", "overlap_fenced"}
            else external_effect,
            args=[root, guard],
            start_to_close_timeout=timedelta(seconds=30 if scenario == "cancel_inflight" else 3),
            retry_policy=RetryPolicy(maximum_attempts=1)
            if scenario == "external_no_retry"
            else retry,
        )
