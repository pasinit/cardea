"""Named response bodies of the v0.2 API, so every route documents its output.

Task submissions, inspection and control return `TaskView` and events return `KernelEvent`s;
these classes cover every other JSON route. Each mirrors exactly what its handler returned
before it was typed: top-level bodies carry `schema_version`, nested parts do not. Payloads
that belong to an operation or a client stay `JsonValue`, described where they appear.
"""

from typing import Literal

from pydantic import Field, JsonValue

from cardea.kernel.models import KernelFailure, ObjectVersion, OperationReceipt
from cardea.service.models import Record, Wire

# System


class SystemView(Wire):
    """`GET /v0.2/system`: what this kernel runs and the limits its listeners enforce."""

    runtime: str = Field(description="Runtime of the deterministic default profile.")
    host_execution: bool = Field(description="The Debian host adapter is enabled.")
    agent_execution: bool = Field(
        description="An agent session runtime is configured, so agent runs can be admitted."
    )
    delegation_execution: bool
    new_namespace_prefix: str | None = Field(
        description="Prefix new namespaces must use after a snapshot restore; null otherwise."
    )
    max_request_bytes: int = Field(description="Request-body cap of every listener.")
    max_task_request_bytes: int = Field(description="Canonical size cap of one task request.")
    max_response_bytes: int


# Operations


class HostOperationInfo(Record):
    """A fixed M2 host operation the caller may submit as a host task."""

    name: str
    description: str
    input_modes: list[str]
    max_input_bytes: int
    reserved_tool_milliseconds: int
    reserved_output_bytes: int
    network: str
    output_scope: str
    retry: str


class RegisteredOperation(Record):
    """An installed operation (ADR-023) the caller may use in the namespace."""

    name: str
    tool: dict[str, JsonValue] = Field(
        description="The operation's MCP tool definition: description and JSON schemas."
    )
    version: int
    effect: Literal["read", "effect"]
    timeout_seconds: int
    max_output_bytes: int
    requires: list[str] = Field(description="Further grants the operation needs.")
    retry: Literal["read_only", "reconciliation_required"]


class OperationCatalog(Wire):
    """`GET /v0.2/operations`: what the caller may do in a namespace under the live ceiling."""

    operations: list[str] = Field(description="Every right and operation name the caller holds.")
    installed: list[HostOperationInfo]
    registered: list[RegisteredOperation]


# Host budget


class HostBudgetLimits(Record):
    tool_ms: int
    output_bytes: int
    concurrency: int


class HostBudgetView(Wire):
    """`GET /v0.2/host-budget`: the caller's host allocation and its cumulative use."""

    limits: HostBudgetLimits | None = Field(description="Null when no allocation exists.")
    allocated_tool_ms: int
    allocated_output_bytes: int
    active_reservations: int
    charged_tool_ms: int
    charged_output_bytes: int
    cleanup_pending: int


# Triggers


class TriggerScheduled(Wire):
    """`PUT /v0.2/triggers/{trigger_id}`: the trigger and what the request changed."""

    trigger_id: str
    change: Literal["installed", "updated", "enabled", "disabled", "unchanged"]


class TriggerRemoved(Wire):
    """`DELETE /v0.2/triggers/{trigger_id}`: removal is idempotent."""

    trigger_id: str
    change: Literal["removed"]


class TriggerEvent(Record):
    event_id: int
    kind: str = Field(description="installed, updated, admitted, missed, failed, removed, ...")
    slot: str | None = Field(description="The scheduled slot (ISO 8601, UTC) it concerns.")
    task_id: str | None = Field(description="The task admitted for the slot, if any.")
    code: str | None = Field(description="Refusal code when admission failed.")
    at: str
    detail: dict[str, JsonValue] = Field(description="Event-specific facts, e.g. missed counts.")


class TriggerEntry(Record):
    trigger_id: str
    namespace: str
    definition: dict[str, JsonValue] = Field(description="The installed TriggerSpec.")
    definition_sha256: str
    enabled: bool
    removed: bool
    last_slot: str | None
    events: list[TriggerEvent] = Field(description="Most recent history first.")


class TriggerList(Wire):
    """`GET /v0.2/triggers`: the caller's own triggers and their recent slot history."""

    triggers: list[TriggerEntry]
    next: str | None = Field(description="Pass as `after` for the next page; null on the last.")


# Runs


class FailureDetails(Record):
    """Why a call failed (ADR-026): a fixed reason and a bounded, sanitized summary."""

    reason: str = Field(
        description="timeout, queue_timeout, worker_exit, worker_refused, "
        "upstream_http_error, invalid_output, rejected, denied or error."
    )
    summary: str = Field(
        description="At most 200 printable characters; never exception text or stderr."
    )


class RunCall(Record):
    """One receipted call of a run: an agent-run context (`ctx-NN`) or tool (`call-NNNN`)
    call, or a single-operation task's `execute`."""

    operation_id: str
    operation: str
    state: str = Field(
        description="accepted, running, succeeded, failed, cancelled or outcome_unknown."
    )
    receipt_id: str
    created_at: str
    updated_at: str
    failure: str | None = Field(description="Failure code of a failed call.")
    failure_details: FailureDetails | dict[str, JsonValue] | None = Field(
        union_mode="left_to_right",
        description="Why the call failed; receipts written before ADR-026's reasons have {}.",
    )
    reconciled: bool = Field(description="The outcome was settled by reconciliation.")
    deduplicated: bool = Field(
        description="Answered from an earlier effect in the same idempotency scope."
    )
    arguments: JsonValue = Field(
        description="The call's arguments, inline only when they fit the preview size "
        "(`run_preview_bytes`, default 2 KiB); otherwise null. See arguments_preview."
    )
    arguments_bytes: int = Field(description="Size of the canonical (RFC 8785) arguments.")
    arguments_sha256: str
    arguments_preview: str = Field(
        description="The first `run_preview_bytes` of the canonical arguments JSON."
    )
    result: dict[str, JsonValue] | None = Field(
        description='{"output": <the operation\'s own output>} and, for a deduplicated call, '
        '"deduplicated": true. Inline only when the output fits the preview size; otherwise '
        "null: fetch the receipt from GET /v0.2/tasks/{id}/operations/{operation_id}."
    )
    result_bytes: int | None = Field(
        description="Size of the canonical output; null for a call without a result."
    )
    result_sha256: str | None
    result_preview: str | None = Field(
        description="The first `run_preview_bytes` of the canonical output JSON."
    )


class RunOutputCall(Record):
    operation_id: str
    operation: str
    state: str
    deduplicated: bool


class AgentRunOutput(Record):
    """The published output document of a successful agent run (ADR-027)."""

    text: str = Field(description="The session's final reply.")
    attempts: int = Field(description="Session attempts used.")
    turns: int = Field(description="Model turns of the successful attempt.")
    cost_usd: float | None = Field(
        description="Cost of the successful attempt: reported by the harness, or metered by "
        "Cardea from tokens and the registry price (ADR-033)."
    )
    harness: str | None = Field(
        default=None, description="Harness that ran the session (ADR-033); absent before it."
    )
    model: str | None = Field(
        default=None, description="Model id selected for the session; null for the default."
    )
    usage: dict[str, JsonValue] = Field(
        default_factory=dict, description="Token usage and cost detail the harness reported."
    )
    calls: list[RunOutputCall] = Field(description="Every context and tool call, in order.")


class AgentRunVerification(Record):
    """The verification evidence of a successful agent run."""

    passed: bool
    check: str
    expectations: list[str]
    calls: int
    effects_succeeded: int
    output_digest: str


class RunView(Wire):
    """`GET /v0.2/tasks/{task_id}/run`: what a run did (ADR-029)."""

    task_id: str
    state: str
    failure: KernelFailure | None
    created_at: str
    updated_at: str
    attempt: int | None = Field(description="The agent run's latest attempt number.")
    output: AgentRunOutput | dict[str, JsonValue] | None = Field(
        union_mode="left_to_right",
        description="The published output: an agent run's document, or another profile's "
        "own output. Null until published.",
    )
    verification: AgentRunVerification | dict[str, JsonValue] | None = Field(
        union_mode="left_to_right",
        description="An agent run's evidence, or another profile's. Null until verified.",
    )
    output_truncated: bool = Field(
        description="The output exceeded the run view's 64 KiB document budget: an agent "
        "run's text is trimmed and its calls list dropped, any other output is null. "
        "GET /v0.2/tasks/{id}/output has it whole."
    )
    verification_truncated: bool = Field(
        description="The verification exceeded the document budget and is null here."
    )
    calls: list[RunCall] = Field(
        description="Receipted calls in the order made: one page, bounded by size. "
        "Pass calls_next as `after` for the next page."
    )
    calls_total: int = Field(description="Number of calls of the run, across all pages.")
    calls_next: str | None = Field(description="Cursor of the next page; null on the last.")


class ChildLink(Record):
    slot: int
    child_id: str
    message_id: str


class ChildUsage(Record):
    children: int
    tool_milliseconds: int
    output_bytes: int
    active_operations: int


class ChildrenView(Wire):
    """`GET /v0.2/tasks/{task_id}/children`: a coordinator's delegated children (ADR-017)."""

    children: list[ChildLink]
    usage: ChildUsage


class ReconcileResult(Wire):
    """`POST .../operations/{operation_id}/reconcile`: the operation's state afterwards."""

    state: str


class AgentReceipts(Wire):
    """`GET /v0.2/tasks/{task_id}/agent-receipts`: full receipts of an agent's calls."""

    receipts: list[OperationReceipt] = Field(
        description="One page of receipts, ordered by operation_id and bounded by size: "
        "request with arguments and scope key, state, result or failure, and reconciliation "
        "evidence."
    )
    next: str | None = Field(description="Pass as `after` for the next page; null on the last.")


class OperationView(Wire):
    """`GET /v0.2/tasks/{task_id}/operations/{operation_id}`: one call's full receipt.

    A receipt always fits a response: its arguments and its result are each at most 64 KiB.
    """

    receipt: OperationReceipt


class ContentChunk(Wire):
    """One base64 chunk of a stored document's canonical bytes; follow next_offset."""

    total_bytes: int
    sha256: str = Field(description="SHA-256 of all the bytes, to check the reassembly.")
    offset: int
    next_offset: int | None = Field(description="Offset of the next chunk; null at the end.")
    data_base64: str


class RunOutput(Wire):
    """`GET /v0.2/tasks/{task_id}/output`: the task's latest published output."""

    artifact: ObjectVersion
    content: JsonValue = Field(
        description="The output document; its shape is the profile's. Null when it would "
        "exceed the response cap: read it from GET /v0.2/tasks/{id}/output/content."
    )
    content_omitted: bool = False
    content_bytes: int = Field(description="Size of the stored document.")
    content_sha256: str


class TaskReceipt(Wire):
    """`GET /v0.2/tasks/{task_id}/receipt`: the receipt that stands for the whole task."""

    digest_format: Literal["legacy-v0.1"]
    receipt: OperationReceipt
