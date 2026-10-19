from datetime import UTC, datetime, timedelta

import pytest

from cardea.kernel.lifecycle import transition_task
from cardea.kernel.models import (
    BudgetLimits,
    CapabilityGrant,
    Checkpoint,
    DispatchLease,
    ObjectVersion,
    OperationReceipt,
    OperationRequest,
    ResourceRef,
    ResourceScope,
    RetryClass,
    Task,
    TaskState,
    VerificationRecord,
)


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 9, 21, 12, tzinfo=UTC)


@pytest.fixture
def limits(now: datetime) -> BudgetLimits:
    return BudgetLimits(
        max_steps=20,
        max_model_calls=5,
        max_model_tokens=10000,
        max_tool_milliseconds=60000,
        max_output_bytes=1048576,
        max_concurrent_operations=1,
        deadline=now + timedelta(hours=1),
    )


@pytest.fixture
def grant(now: datetime, limits: BudgetLimits) -> CapabilityGrant:
    return CapabilityGrant(
        grant_id="grant-output",
        issuer_id="supervisor",
        subject_id="agent-indexer",
        operations=("objects.write",),
        resources=(ResourceScope(namespace="outputs"),),
        issued_at=now,
        expires_at=limits.deadline,
        limits=limits,
        budget_account_id="budget-1",
    )


@pytest.fixture
def task(now: datetime, limits: BudgetLimits) -> Task:
    return Task(
        task_id="task-1",
        agent_id="agent-indexer",
        objective="Index the input records.",
        runtime_id="deterministic-v1",
        verifier_id="index-verifier-v1",
        capability_ids=("grant-output",),
        budget_account_id="budget-1",
        limits=limits,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def lease(now: datetime) -> DispatchLease:
    return DispatchLease(
        lease_id="lease-1",
        worker_id="worker-1",
        acquired_at=now,
        expires_at=now + timedelta(minutes=5),
    )


@pytest.fixture
def running_task(task: Task, lease: DispatchLease, now: datetime) -> Task:
    return transition_task(
        task,
        TaskState.RUNNING,
        expected_revision=0,
        at=now,
        lease=lease,
    )


@pytest.fixture
def checkpoint(now: datetime) -> Checkpoint:
    return Checkpoint(
        checkpoint_id="checkpoint-1",
        task_id="task-1",
        saved_at=now,
        state={"processed": ["input-1"]},
        event_cursor=7,
    )


@pytest.fixture
def verification(now: datetime) -> VerificationRecord:
    return VerificationRecord(
        verification_id="verification-1",
        task_id="task-1",
        verifier_id="index-verifier-v1",
        checked_at=now,
        passed=True,
        artifacts=(
            ObjectVersion(
                namespace="outputs",
                resource_id="index",
                version=1,
                content_digest="a" * 64,
            ),
        ),
        evidence=(ResourceRef(namespace="checks", resource_id="index-check-1"),),
    )


@pytest.fixture
def operation_request(now: datetime) -> OperationRequest:
    return OperationRequest(
        operation_id="operation-1",
        task_id="task-1",
        subject_id="agent-indexer",
        name="objects.write",
        resource=ResourceRef(namespace="outputs", resource_id="index"),
        arguments={"expected_version": 0, "content": {"items": ["input-1"], "count": 1}},
        deadline=now + timedelta(minutes=5),
    )


@pytest.fixture
def receipt(operation_request: OperationRequest, now: datetime) -> OperationReceipt:
    return OperationReceipt(
        receipt_id="receipt-1",
        request=operation_request,
        request_digest=operation_request.digest(),
        grant_ids=("grant-output",),
        retry_class=RetryClass.IDEMPOTENT,
        created_at=now,
        updated_at=now,
    )
