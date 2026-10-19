"""Versioned records for the kernel, independent of the legacy workflow app.

These validate data, not authority. Only a future authenticated kernel service
may issue grants, record verifier results, or admit operations.
"""

import json
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    model_validator,
)

Identifier = Annotated[str, Field(strict=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
OperationName = Annotated[
    str, Field(strict=True, max_length=128, pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
]
NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
PositiveInt = Annotated[int, Field(strict=True, gt=0)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Text = Annotated[str, Field(strict=True, min_length=1, max_length=4096)]


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


Timestamp = Annotated[AwareDatetime, AfterValidator(_utc)]


def canonical_json(value: object) -> bytes:
    """v0.1 encoding: sorted keys, compact ASCII JSON, finite numbers only.

    This is not RFC 8785. In particular, 1 and 1.0 are distinct arguments.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _bounded_document(value: dict[str, JsonValue]) -> dict[str, JsonValue]:
    if len(canonical_json(value)) > 65536:
        raise ValueError("JSON documents must not exceed 65536 encoded bytes")
    return value


Document = Annotated[dict[str, JsonValue], AfterValidator(_bounded_document)]


class KernelModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_default=True,
        revalidate_instances="always",
        allow_inf_nan=False,
    )

    schema_version: Literal["0.1"] = "0.1"


class AgentIdentity(KernelModel):
    agent_id: Identifier
    owner_id: Identifier
    display_name: Text
    created_at: Timestamp
    disabled: Annotated[bool, Field(strict=True)] = False


class ResourceScope(KernelModel):
    namespace: Identifier
    # None explicitly grants namespace-wide scope, not an implicit glob pattern.
    resource_id: Identifier | None = None


class ResourceRef(KernelModel):
    namespace: Identifier
    resource_id: Identifier


class ObjectVersion(ResourceRef):
    version: PositiveInt
    content_digest: Digest


class BudgetLimits(KernelModel):
    max_steps: NonNegativeInt
    max_model_calls: NonNegativeInt
    max_model_tokens: NonNegativeInt
    max_tool_milliseconds: NonNegativeInt
    max_output_bytes: NonNegativeInt
    max_concurrent_operations: PositiveInt
    deadline: Timestamp


class ResourceUsage(KernelModel):
    steps: NonNegativeInt = 0
    model_calls: NonNegativeInt = 0
    model_tokens: NonNegativeInt = 0
    tool_milliseconds: NonNegativeInt = 0
    output_bytes: NonNegativeInt = 0


class CapabilityGrant(KernelModel):
    grant_id: Identifier
    issuer_id: Identifier
    subject_id: Identifier
    operations: Annotated[tuple[OperationName, ...], Field(min_length=1)]
    resources: Annotated[tuple[ResourceScope, ...], Field(min_length=1)]
    issued_at: Timestamp
    expires_at: Timestamp
    revoked_at: Timestamp | None = None
    parent_grant_id: Identifier | None = None
    # Children must refer to the same budget account; attenuation is an M4 policy check.
    budget_account_id: Identifier
    limits: BudgetLimits
    delegation_allowed: Annotated[bool, Field(strict=True)] = False

    @model_validator(mode="after")
    def consistent_grant(self) -> Self:
        if self.expires_at <= self.issued_at:
            raise ValueError("grant expiry must follow issuance")
        if self.revoked_at is not None and self.revoked_at < self.issued_at:
            raise ValueError("revocation cannot precede issuance")
        if self.limits.deadline > self.expires_at or self.limits.deadline <= self.issued_at:
            raise ValueError("grant budget deadline must be within its validity interval")
        if self.parent_grant_id == self.grant_id:
            raise ValueError("a grant cannot delegate from itself")
        if len(set(self.operations)) != len(self.operations):
            raise ValueError("operation scopes must be unique")
        return self


class KernelFailure(KernelModel):
    code: Identifier
    message: Text
    retryable: Annotated[bool, Field(strict=True)] = False
    details: Document = Field(default_factory=dict)


class Checkpoint(KernelModel):
    checkpoint_id: Identifier
    task_id: Identifier
    saved_at: Timestamp
    state: Document = Field(default_factory=dict)
    input_versions: tuple[ObjectVersion, ...] = ()
    event_cursor: NonNegativeInt = 0
    outstanding_operation_ids: tuple[Identifier, ...] = ()


class EventWait(KernelModel):
    kind: Literal["event"] = "event"
    namespace: Identifier
    event_type: Identifier
    after_cursor: NonNegativeInt


class TimerWait(KernelModel):
    kind: Literal["timer"] = "timer"
    wake_at: Timestamp


class AuthorityWait(KernelModel):
    kind: Literal["authority"] = "authority"
    request_id: Identifier


class DependencyWait(KernelModel):
    kind: Literal["dependency"] = "dependency"
    task_id: Identifier


WaitCondition = Annotated[
    EventWait | TimerWait | AuthorityWait | DependencyWait, Field(discriminator="kind")
]


class DispatchLease(KernelModel):
    lease_id: Identifier
    worker_id: Identifier
    acquired_at: Timestamp
    expires_at: Timestamp

    @model_validator(mode="after")
    def positive_duration(self) -> Self:
        if self.expires_at <= self.acquired_at:
            raise ValueError("lease expiry must follow acquisition")
        return self


class VerificationRecord(KernelModel):
    verification_id: Identifier
    task_id: Identifier
    verifier_id: Identifier
    checked_at: Timestamp
    passed: Annotated[bool, Field(strict=True)]
    artifacts: Annotated[tuple[ObjectVersion, ...], Field(min_length=1)]
    evidence: Annotated[tuple[ResourceRef, ...], Field(min_length=1)]


class TaskState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    SUSPENDED = "suspended"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Task(KernelModel):
    task_id: Identifier
    agent_id: Identifier
    objective: Text
    runtime_id: Identifier
    verifier_id: Identifier
    capability_ids: tuple[Identifier, ...] = ()
    budget_account_id: Identifier
    limits: BudgetLimits
    parent_task_id: Identifier | None = None
    state: TaskState = TaskState.QUEUED
    revision: NonNegativeInt = 0
    created_at: Timestamp
    updated_at: Timestamp
    checkpoint: Checkpoint | None = None
    wait: WaitCondition | None = None
    lease: DispatchLease | None = None
    verification: VerificationRecord | None = None
    failure: KernelFailure | None = None

    @model_validator(mode="after")
    def consistent_state(self) -> Self:
        if self.updated_at < self.created_at or self.limits.deadline <= self.created_at:
            raise ValueError("invalid task timestamps or budget deadline")
        if self.parent_task_id == self.task_id:
            raise ValueError("a task cannot be its own parent")
        if self.checkpoint is not None:
            if self.checkpoint.task_id != self.task_id:
                raise ValueError("checkpoint belongs to a different task")
            if not self.created_at <= self.checkpoint.saved_at <= self.updated_at:
                raise ValueError("checkpoint time is outside task history")
        if (self.state is TaskState.RUNNING) != (self.lease is not None):
            raise ValueError("only running tasks must have a dispatch lease")
        if self.lease is not None:
            if not self.created_at <= self.lease.acquired_at <= self.updated_at:
                raise ValueError("lease acquisition is outside task history")
            if not self.updated_at < self.lease.expires_at <= self.limits.deadline:
                raise ValueError("lease must be live at update time and bounded by task deadline")
        if (self.state is TaskState.WAITING) != (self.wait is not None):
            raise ValueError("only waiting tasks must have a wait condition")
        if self.state in {TaskState.WAITING, TaskState.SUSPENDED} and self.checkpoint is None:
            raise ValueError("waiting and suspended tasks require a checkpoint")
        if (
            isinstance(self.wait, EventWait)
            and self.checkpoint is not None
            and self.wait.after_cursor != self.checkpoint.event_cursor
        ):
            raise ValueError("event wait must use the checkpoint cursor")
        if isinstance(self.wait, TimerWait) and self.wait.wake_at <= self.updated_at:
            raise ValueError("timer wait must wake after its registration")
        if isinstance(self.wait, DependencyWait) and self.wait.task_id == self.task_id:
            raise ValueError("a task cannot wait for itself")
        if (self.state is TaskState.FAILED) != (self.failure is not None):
            raise ValueError("only failed tasks must have a failure reason")
        if (self.state is TaskState.SUCCEEDED) != (self.verification is not None):
            raise ValueError("only succeeded tasks must have accepted verification")
        if self.verification is not None:
            result = self.verification
            if not result.passed or result.task_id != self.task_id:
                raise ValueError("verification must pass for this task")
            if result.verifier_id != self.verifier_id:
                raise ValueError("verification must come from the assigned verifier")
            if not self.created_at <= result.checked_at <= self.updated_at:
                raise ValueError("verification time is outside task history")
        return self


class OperationRequest(KernelModel):
    operation_id: Identifier
    task_id: Identifier
    subject_id: Identifier
    name: OperationName
    resource: ResourceRef
    arguments: Document = Field(default_factory=dict)
    deadline: Timestamp

    def digest(self) -> str:
        # Revalidate because frozen Pydantic models do not freeze nested JSON containers.
        request = OperationRequest.model_validate(self.model_dump())
        return sha256(canonical_json(request.model_dump(mode="json"))).hexdigest()


class RetryClass(StrEnum):
    READ_ONLY = "read_only"
    IDEMPOTENT = "idempotent"
    EXTERNALLY_DEDUPLICATED = "externally_deduplicated"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class OperationState(StrEnum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    OUTCOME_UNKNOWN = "outcome_unknown"


class OperationReceipt(KernelModel):
    receipt_id: Identifier
    request: OperationRequest
    request_digest: Digest
    # Evidence of an admission decision, not a substitute for checking current grants.
    grant_ids: Annotated[tuple[Identifier, ...], Field(min_length=1)]
    retry_class: RetryClass
    state: OperationState = OperationState.ACCEPTED
    revision: NonNegativeInt = 0
    created_at: Timestamp
    updated_at: Timestamp
    usage: ResourceUsage = Field(default_factory=ResourceUsage)
    result: Document | None = None
    failure: KernelFailure | None = None
    reconciliation_evidence: tuple[ResourceRef, ...] = ()

    @model_validator(mode="after")
    def consistent_receipt(self) -> Self:
        if self.request_digest != self.request.digest():
            raise ValueError("receipt digest does not match its admitted request")
        if self.updated_at < self.created_at or self.request.deadline <= self.created_at:
            raise ValueError("invalid operation timestamps or deadline")
        if (self.state is OperationState.SUCCEEDED) != (self.result is not None):
            raise ValueError("only successful operations must have a result document")
        needs_failure = self.state in {OperationState.FAILED, OperationState.OUTCOME_UNKNOWN}
        if needs_failure != (self.failure is not None):
            raise ValueError("failed or unknown operations require an error record")
        return self


class KernelEvent(KernelModel):
    event_id: Identifier
    namespace: Identifier
    cursor: PositiveInt
    event_type: Identifier
    producer_id: Identifier
    task_id: Identifier | None = None
    correlation_id: Identifier
    occurred_at: Timestamp
    payload: Document = Field(default_factory=dict)
