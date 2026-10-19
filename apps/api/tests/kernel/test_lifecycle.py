from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from cardea.kernel.lifecycle import (
    ContractError,
    ContractErrorCode,
    match_operation_retry,
    transition_operation,
    transition_task,
)
from cardea.kernel.models import (
    Checkpoint,
    DispatchLease,
    EventWait,
    KernelFailure,
    OperationReceipt,
    OperationRequest,
    OperationState,
    ResourceRef,
    ResourceUsage,
    Task,
    TaskState,
    VerificationRecord,
)


def test_headless_task_lifecycle_round_trips_without_mutating_prior_states(
    task: Task,
    running_task: Task,
    checkpoint: Checkpoint,
    verification: VerificationRecord,
    lease: DispatchLease,
    now: datetime,
) -> None:
    waiting = transition_task(
        running_task,
        TaskState.WAITING,
        expected_revision=1,
        at=now,
        checkpoint=checkpoint,
        wait=EventWait(namespace="inputs", event_type="input.changed", after_cursor=7),
    )
    # Serialization is a contract test, not a claim of implemented restart recovery.
    restored = Task.model_validate_json(waiting.model_dump_json())
    ready = transition_task(restored, TaskState.QUEUED, expected_revision=2, at=now)
    resumed = transition_task(
        ready,
        TaskState.RUNNING,
        expected_revision=3,
        at=now,
        lease=lease,
    )
    completed = transition_task(
        resumed,
        TaskState.SUCCEEDED,
        expected_revision=4,
        at=now,
        verification=verification,
    )
    assert completed.revision == 5
    assert completed.verification == verification
    assert completed.lease is None and completed.wait is None
    assert task.state is TaskState.QUEUED and task.revision == 0
    assert running_task.state is TaskState.RUNNING and running_task.checkpoint is None
    checkpoint.state["processed"] = []
    assert completed.checkpoint is not None
    assert completed.checkpoint.state == {"processed": ["input-1"]}


def test_suspend_and_resume_preserves_the_checkpoint(
    running_task: Task,
    checkpoint: Checkpoint,
    now: datetime,
) -> None:
    suspended = transition_task(
        running_task,
        TaskState.SUSPENDED,
        expected_revision=1,
        at=now,
        checkpoint=checkpoint,
    )
    ready = transition_task(suspended, TaskState.QUEUED, expected_revision=2, at=now)
    assert ready.checkpoint == checkpoint
    assert ready.lease is None


@pytest.mark.parametrize("terminal", [TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED])
@pytest.mark.parametrize("target", list(TaskState))
def test_terminal_tasks_never_reopen(
    running_task: Task,
    verification: VerificationRecord,
    now: datetime,
    terminal: TaskState,
    target: TaskState,
) -> None:
    ended = transition_task(
        running_task,
        terminal,
        expected_revision=1,
        at=now,
        verification=verification if terminal is TaskState.SUCCEEDED else None,
        failure=KernelFailure(code="tool_failed", message="Tool failed.")
        if terminal is TaskState.FAILED
        else None,
    )
    with pytest.raises(ContractError) as error:
        transition_task(ended, target, expected_revision=2, at=now)
    assert error.value.code is ContractErrorCode.INVALID_TRANSITION


def test_stale_task_revision_and_time_are_rejected(task: Task, now: datetime) -> None:
    for revision, at, code in (
        (1, now, ContractErrorCode.REVISION_CONFLICT),
        (True, now, ContractErrorCode.REVISION_CONFLICT),
        (0, now - timedelta(seconds=1), ContractErrorCode.TIME_REGRESSION),
    ):
        with pytest.raises(ContractError) as error:
            transition_task(task, TaskState.CANCELLED, expected_revision=revision, at=at)
        assert error.value.code is code


def test_dispatch_requires_a_live_lease_before_deadline(
    task: Task,
    lease: DispatchLease,
    now: datetime,
) -> None:
    with pytest.raises(ValidationError, match="lease"):
        transition_task(task, TaskState.RUNNING, expected_revision=0, at=now)
    with pytest.raises(ContractError) as error:
        transition_task(
            task,
            TaskState.RUNNING,
            expected_revision=0,
            at=task.limits.deadline,
            lease=lease,
        )
    assert error.value.code is ContractErrorCode.DEADLINE_EXCEEDED


@pytest.mark.parametrize("target", [TaskState.WAITING, TaskState.SUSPENDED])
def test_running_task_cannot_yield_without_a_fresh_checkpoint(
    running_task: Task,
    now: datetime,
    target: TaskState,
) -> None:
    with pytest.raises(ContractError) as error:
        transition_task(running_task, target, expected_revision=1, at=now)
    assert error.value.code is ContractErrorCode.CHECKPOINT_REQUIRED


def test_running_task_cannot_be_blindly_requeued(running_task: Task, now: datetime) -> None:
    with pytest.raises(ContractError) as error:
        transition_task(running_task, TaskState.QUEUED, expected_revision=1, at=now)
    assert error.value.code is ContractErrorCode.INVALID_TRANSITION


def test_event_wait_requires_matching_checkpoint_cursor(
    running_task: Task,
    checkpoint: Checkpoint,
    now: datetime,
) -> None:
    with pytest.raises(ValidationError, match="cursor"):
        transition_task(
            running_task,
            TaskState.WAITING,
            expected_revision=1,
            at=now,
            checkpoint=checkpoint,
            wait=EventWait(namespace="inputs", event_type="input.changed", after_cursor=8),
        )


def test_checkpoint_must_belong_to_the_task(
    running_task: Task,
    checkpoint: Checkpoint,
    now: datetime,
) -> None:
    other = Checkpoint.model_validate(checkpoint.model_dump() | {"task_id": "task-2"})
    with pytest.raises(ValidationError, match="different task"):
        transition_task(
            running_task,
            TaskState.SUSPENDED,
            expected_revision=1,
            at=now,
            checkpoint=other,
        )


@pytest.mark.parametrize(
    "change",
    [
        {"passed": False},
        {"task_id": "other-task"},
        {"verifier_id": "agent-self-report"},
    ],
)
def test_false_or_misattributed_verification_cannot_complete_a_task(
    running_task: Task,
    verification: VerificationRecord,
    now: datetime,
    change: dict[str, object],
) -> None:
    invalid = VerificationRecord.model_validate(verification.model_dump() | change)
    with pytest.raises(ValidationError, match="verification"):
        transition_task(
            running_task,
            TaskState.SUCCEEDED,
            expected_revision=1,
            at=now,
            verification=invalid,
        )


def test_missing_or_stale_verification_cannot_complete_a_task(
    running_task: Task,
    verification: VerificationRecord,
    now: datetime,
) -> None:
    stale = VerificationRecord.model_validate(
        verification.model_dump() | {"checked_at": now - timedelta(seconds=1)},
    )
    for evidence in (None, stale):
        with pytest.raises(ContractError) as error:
            transition_task(
                running_task,
                TaskState.SUCCEEDED,
                expected_revision=1,
                at=now,
                verification=evidence,
            )
        assert error.value.code is ContractErrorCode.VERIFICATION_REQUIRED


def test_uncertain_effect_never_reexecutes_and_needs_reconciliation(
    receipt: OperationReceipt,
    operation_request: OperationRequest,
    now: datetime,
) -> None:
    running = transition_operation(receipt, OperationState.RUNNING, expected_revision=0, at=now)
    unknown = transition_operation(
        running,
        OperationState.OUTCOME_UNKNOWN,
        expected_revision=1,
        at=now,
        failure=KernelFailure(code="receipt_lost", message="Worker exited before receipt."),
    )
    assert match_operation_retry(unknown, operation_request) == unknown
    with pytest.raises(ContractError) as error:
        transition_operation(unknown, OperationState.RUNNING, expected_revision=2, at=now)
    assert error.value.code is ContractErrorCode.INVALID_TRANSITION
    with pytest.raises(ContractError) as error:
        transition_operation(
            unknown,
            OperationState.SUCCEEDED,
            expected_revision=2,
            at=now,
            result={"version": 1},
        )
    assert error.value.code is ContractErrorCode.RECONCILIATION_REQUIRED
    settled = transition_operation(
        unknown,
        OperationState.SUCCEEDED,
        expected_revision=2,
        at=now,
        result={"version": 1},
        reconciliation_evidence=(ResourceRef(namespace="checks", resource_id="observed-output"),),
    )
    assert settled.failure is None and settled.result == {"version": 1}
    assert receipt.state is OperationState.ACCEPTED and unknown.failure is not None


@pytest.mark.parametrize(
    "change",
    [
        {"arguments": {"content": "different"}},
        {"task_id": "other-task"},
        {"subject_id": "other-agent"},
        {"operation_id": "new-operation"},
        {"name": "objects.read"},
        {"resource": {"namespace": "private", "resource_id": "index"}},
    ],
)
def test_duplicate_id_is_bound_to_the_entire_request(
    receipt: OperationReceipt,
    operation_request: OperationRequest,
    change: dict[str, object],
) -> None:
    changed = OperationRequest.model_validate(operation_request.model_dump() | change)
    with pytest.raises(ContractError) as error:
        match_operation_retry(receipt, changed)
    assert error.value.code is ContractErrorCode.REQUEST_CONFLICT


def test_completed_operation_retry_returns_receipt_without_reexecution(
    receipt: OperationReceipt,
    operation_request: OperationRequest,
    now: datetime,
) -> None:
    running = transition_operation(receipt, OperationState.RUNNING, expected_revision=0, at=now)
    finished = transition_operation(
        running,
        OperationState.SUCCEEDED,
        expected_revision=1,
        at=now,
        result={"version": 1},
        usage=ResourceUsage(steps=1, output_bytes=20),
    )
    assert match_operation_retry(finished, operation_request) == finished
    with pytest.raises(ContractError) as error:
        transition_operation(finished, OperationState.RUNNING, expected_revision=2, at=now)
    assert error.value.code is ContractErrorCode.INVALID_TRANSITION


def test_operation_rejects_expired_dispatch_and_stale_revision(
    receipt: OperationReceipt,
    now: datetime,
) -> None:
    for revision, at, code in (
        (1, now, ContractErrorCode.REVISION_CONFLICT),
        (0, receipt.request.deadline, ContractErrorCode.DEADLINE_EXCEEDED),
    ):
        with pytest.raises(ContractError) as error:
            transition_operation(receipt, OperationState.RUNNING, expected_revision=revision, at=at)
        assert error.value.code is code


def test_usage_cannot_be_erased_when_an_operation_fails(
    receipt: OperationReceipt,
    now: datetime,
) -> None:
    running = transition_operation(
        receipt,
        OperationState.RUNNING,
        expected_revision=0,
        at=now,
        usage=ResourceUsage(steps=1, model_tokens=10),
    )
    with pytest.raises(ContractError) as error:
        transition_operation(
            running,
            OperationState.FAILED,
            expected_revision=1,
            at=now,
            failure=KernelFailure(code="worker_failed", message="Worker failed."),
            usage=ResourceUsage(),
        )
    assert error.value.code is ContractErrorCode.USAGE_REGRESSION
    failed = transition_operation(
        running,
        OperationState.FAILED,
        expected_revision=1,
        at=now,
        failure=KernelFailure(code="worker_failed", message="Worker failed."),
    )
    assert failed.usage == running.usage
