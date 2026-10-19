"""Pure transition checks for trusted callers; no persistence or authorization.

The future store must atomically compare revisions and save state plus events.
Checking a revision here alone does not lock concurrent callers out.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import TypeAdapter

from cardea.kernel.models import (
    Checkpoint,
    DispatchLease,
    Document,
    KernelFailure,
    OperationReceipt,
    OperationRequest,
    OperationState,
    ResourceRef,
    ResourceUsage,
    Task,
    TaskState,
    Timestamp,
    VerificationRecord,
    WaitCondition,
)


class ContractErrorCode(StrEnum):
    INVALID_TRANSITION = "invalid_transition"
    REVISION_CONFLICT = "revision_conflict"
    TIME_REGRESSION = "time_regression"
    CHECKPOINT_REQUIRED = "checkpoint_required"
    VERIFICATION_REQUIRED = "verification_required"
    REQUEST_CONFLICT = "request_conflict"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    USAGE_REGRESSION = "usage_regression"


class ContractError(ValueError):
    def __init__(self, code: ContractErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


_TASK_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.QUEUED: frozenset({TaskState.RUNNING, TaskState.CANCELLED, TaskState.FAILED}),
    TaskState.RUNNING: frozenset(
        {
            TaskState.WAITING,
            TaskState.SUSPENDED,
            TaskState.SUCCEEDED,
            TaskState.FAILED,
            TaskState.CANCELLED,
        }
    ),
    TaskState.WAITING: frozenset(
        {
            TaskState.QUEUED,
            TaskState.SUSPENDED,
            TaskState.FAILED,
            TaskState.CANCELLED,
        }
    ),
    TaskState.SUSPENDED: frozenset({TaskState.QUEUED, TaskState.FAILED, TaskState.CANCELLED}),
    TaskState.SUCCEEDED: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.CANCELLED: frozenset(),
}

_OPERATION_TRANSITIONS: dict[OperationState, frozenset[OperationState]] = {
    OperationState.ACCEPTED: frozenset(
        {
            OperationState.RUNNING,
            OperationState.FAILED,
            OperationState.CANCELLED,
        }
    ),
    OperationState.RUNNING: frozenset(
        {
            OperationState.SUCCEEDED,
            OperationState.FAILED,
            OperationState.CANCELLED,
            OperationState.OUTCOME_UNKNOWN,
        }
    ),
    OperationState.OUTCOME_UNKNOWN: frozenset(
        {
            OperationState.SUCCEEDED,
            OperationState.FAILED,
            OperationState.CANCELLED,
        }
    ),
    OperationState.SUCCEEDED: frozenset(),
    OperationState.FAILED: frozenset(),
    OperationState.CANCELLED: frozenset(),
}

_timestamp = TypeAdapter(Timestamp)


def _check_revision_and_time(
    revision: int,
    expected_revision: int,
    updated_at: datetime,
    at: datetime,
) -> datetime:
    if type(expected_revision) is not int or expected_revision != revision:
        raise ContractError(ContractErrorCode.REVISION_CONFLICT, "State revision has changed.")
    now = _timestamp.validate_python(at)
    if now < updated_at:
        raise ContractError(ContractErrorCode.TIME_REGRESSION, "Transition predates current state.")
    return now


def transition_task(
    task: Task,
    target: TaskState,
    *,
    expected_revision: int,
    at: datetime,
    checkpoint: Checkpoint | None = None,
    wait: WaitCondition | None = None,
    lease: DispatchLease | None = None,
    verification: VerificationRecord | None = None,
    failure: KernelFailure | None = None,
) -> Task:
    target = TaskState(target)
    task = Task.model_validate(task.model_dump())
    now = _check_revision_and_time(task.revision, expected_revision, task.updated_at, at)
    if target not in _TASK_TRANSITIONS[task.state]:
        raise ContractError(
            ContractErrorCode.INVALID_TRANSITION,
            f"Cannot move task from {task.state} to {target}.",
        )
    if target is TaskState.RUNNING and now >= task.limits.deadline:
        raise ContractError(ContractErrorCode.DEADLINE_EXCEEDED, "Task deadline has elapsed.")
    if (
        task.state is TaskState.RUNNING
        and target in {TaskState.WAITING, TaskState.SUSPENDED}
        and (checkpoint is None or checkpoint.saved_at < task.updated_at)
    ):
        raise ContractError(
            ContractErrorCode.CHECKPOINT_REQUIRED,
            "A fresh checkpoint is required to yield.",
        )
    if target is TaskState.SUCCEEDED and (
        verification is None or verification.checked_at < task.updated_at
    ):
        raise ContractError(
            ContractErrorCode.VERIFICATION_REQUIRED,
            "Current verifier evidence is required.",
        )
    data = task.model_dump()
    data.update(
        state=target,
        revision=task.revision + 1,
        updated_at=now,
        checkpoint=checkpoint if checkpoint is not None else task.checkpoint,
        wait=wait,
        lease=lease,
        verification=verification,
        failure=failure,
    )
    return Task.model_validate(data)


def transition_operation(
    receipt: OperationReceipt,
    target: OperationState,
    *,
    expected_revision: int,
    at: datetime,
    result: Document | None = None,
    failure: KernelFailure | None = None,
    usage: ResourceUsage | None = None,
    reconciliation_evidence: tuple[ResourceRef, ...] = (),
) -> OperationReceipt:
    target = OperationState(target)
    receipt = OperationReceipt.model_validate(receipt.model_dump())
    now = _check_revision_and_time(receipt.revision, expected_revision, receipt.updated_at, at)
    if target not in _OPERATION_TRANSITIONS[receipt.state]:
        raise ContractError(
            ContractErrorCode.INVALID_TRANSITION,
            f"Cannot move operation from {receipt.state} to {target}.",
        )
    if target is OperationState.RUNNING and now >= receipt.request.deadline:
        raise ContractError(ContractErrorCode.DEADLINE_EXCEEDED, "Operation deadline has elapsed.")
    if receipt.state is OperationState.OUTCOME_UNKNOWN and not reconciliation_evidence:
        raise ContractError(
            ContractErrorCode.RECONCILIATION_REQUIRED,
            "Unknown effects need reconciliation evidence.",
        )
    if usage is not None:
        previous = receipt.usage.model_dump(exclude={"schema_version"})
        reported = usage.model_dump(exclude={"schema_version"})
        if any(reported[name] < amount for name, amount in previous.items()):
            raise ContractError(
                ContractErrorCode.USAGE_REGRESSION,
                "Recorded resource usage cannot decrease.",
            )
    data = receipt.model_dump()
    data.update(
        state=target,
        revision=receipt.revision + 1,
        updated_at=now,
        result=result,
        failure=failure,
        usage=usage if usage is not None else receipt.usage,
        reconciliation_evidence=reconciliation_evidence,
    )
    return OperationReceipt.model_validate(data)


def match_operation_retry(
    receipt: OperationReceipt,
    request: OperationRequest,
) -> OperationReceipt:
    """Return the recorded outcome, including uncertainty; never dispatch work.

    Callers must authenticate and recheck authority before exposing the receipt.
    v0.1 compares the entire request, including deadline, identity, and target.
    """
    snapshot = OperationReceipt.model_validate(receipt.model_dump())
    if snapshot.request_digest != request.digest():
        raise ContractError(
            ContractErrorCode.REQUEST_CONFLICT,
            "Operation ID is bound to a different request.",
        )
    return snapshot
