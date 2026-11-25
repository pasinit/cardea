"""Named response bodies of the v0.2 API, so every route documents its output.

Task submissions, inspection and control return `TaskView` and events return `KernelEvent`s;
these classes cover every other JSON route. Each mirrors exactly what its handler returned
before it was typed: top-level bodies carry `schema_version`, nested parts do not. Payloads
that belong to an operation or a client stay `JsonValue`, described where they appear.
"""

from typing import Literal

from pydantic import ConfigDict, Field, JsonValue

from cardea.kernel.models import KernelFailure, ObjectVersion, OperationReceipt
from cardea.service.models import GroupSpec, Record, Wire

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


class Liveness(Wire):
    """`GET /v0.2/livez`: the kernel process answers. It reads no state."""

    status: Literal["ok"]


class ReadinessCheck(Record):
    """One readiness check: its name, whether it passed and a short reason."""

    name: str = Field(description="For example `database`, `repository:<alias>`.")
    ok: bool
    detail: str


class Readiness(Wire):
    """`GET /v0.2/readyz`: whether every check passed (HTTP 200) or not (HTTP 503)."""

    ready: bool
    checks: list[ReadinessCheck]
    as_of: str = Field(description="When the checks ran (UTC, ISO 8601).")


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


class TriggerDelivery(Record):
    """One decided delivery of an inbound event to an event trigger (ADR-035)."""

    delivery_id: int
    source: str
    event_id: str = Field(description="The CloudEvents `id`.")
    event_type: str
    outcome: Literal["admitted", "refused"]
    task_id: str | None = Field(description="The task the event started, if admitted.")
    code: str | None = Field(description="Refusal code; a refused event can be redelivered.")
    at: str


class TriggerEntry(Record):
    trigger_id: str
    kind: Literal["time", "event"] = "time"
    namespace: str
    definition: dict[str, JsonValue] = Field(description="The installed TriggerSpec.")
    definition_sha256: str
    enabled: bool
    removed: bool
    last_slot: str | None
    events: list[TriggerEvent] = Field(description="Most recent history first.")
    deliveries: list[TriggerDelivery] = Field(
        default_factory=list, description="Event triggers: most recent deliveries first."
    )


class EventDelivered(Wire):
    """`POST /v0.2/events/{trigger_id}`: what the kernel did with one signed event."""

    trigger_id: str
    outcome: Literal["admitted", "duplicate", "ignored"] = Field(
        description="admitted: a new task; duplicate: the task this event already started; "
        "ignored: the event does not match the trigger, and nothing was started."
    )
    task_id: str | None


class TriggerList(Wire):
    """`GET /v0.2/triggers`: the caller's own triggers and their recent slot history."""

    triggers: list[TriggerEntry]
    next: str | None = Field(description="Pass as `after` for the next page; null on the last.")


# Agent groups (ADR-034)


class GroupMember(Record):
    principal_id: str = Field(description="The member principal `<group>.<pool>.<n>`.")
    pool: str
    ordinal: int
    state: Literal["active", "draining", "removed"]
    disabled: bool = Field(description="The principal is disabled; its runs are refused.")
    active_task_id: str | None = Field(description="The member's unfinished run, if any.")
    triggers: list[str] = Field(description="The member's compiled ADR-029 triggers.")


class GroupStatus(Record):
    observedGeneration: int = Field(  # The Kubernetes field name.
        description="The newest generation the reconciler converged; below `generation` "
        "while members drain."
    )
    state: Literal["active", "deleting", "deleted"] = Field(
        description="`deleting` while a deleted group's members drain."
    )
    members: list[GroupMember]


class GroupMetadataView(Record):
    name: str
    namespace: str
    generation: int = Field(description="Incremented by every apply that changes `spec`.")
    created_at: str
    updated_at: str


class GroupView(Wire):
    """`GET /v0.2/groups/{name}`: the applied manifest and its status."""

    apiVersion: Literal["cardea/v1alpha1"]  # The Kubernetes field name.
    kind: Literal["AgentGroup"]
    metadata: GroupMetadataView
    spec: GroupSpec
    status: GroupStatus


class OverviewRun(Record):
    task_id: str
    outcome: str
    failure_code: str | None
    finished_at: str | None


class OverviewMember(Record):
    principal_id: str
    pool: str
    ordinal: int
    state: str = Field(description="active, draining or removed.")
    disabled: bool
    active_task_id: str | None = Field(description="The member's unfinished run, if any.")
    last_run: OverviewRun | None = Field(description="Its most recently finished run.")
    claims: list[str] = Field(description="Live claims held, as `<collection>/<record_id>`.")
    claims_truncated: int


class OverviewPool(Record):
    name: str
    replicas: int = Field(description="Replicas in the spec; 0 for a removed pool.")
    members: list[OverviewMember]


class OverviewClaim(Record):
    record_id: str
    title: str | None
    state: str | None
    holder: str
    claim_epoch: int
    claimed_at: str | None
    age_seconds: int | None
    lease_expires_at: str | None


class OverviewCollection(Record):
    name: str
    status: Literal["ok", "unavailable"]
    by_state: dict[str, int] = Field(description="Record counts by state; `null` without states.")
    total: int
    claimed: list[OverviewClaim]
    truncated: int


class OverviewAddress(Record):
    address: str
    pending: int
    in_flight: int
    dead: int
    consumed: int


class OverviewMailbox(Record):
    addresses: list[OverviewAddress]
    truncated: int


class OverviewApproval(Record):
    request_id: str
    task_id: str
    operation: str
    resource: str | None
    state: str
    created_at: str
    expires_at: str | None


class OverviewApprovals(Record):
    items: list[OverviewApproval]
    truncated: int


class OverviewAccount(Record):
    account: str = Field(description="`group` or `pool:<name>`.")
    usd: float | None = Field(description="The USD ceiling; null for a run-only account.")
    window: str
    window_ends_at: str
    used_usd: float = Field(description="Settled costs plus open and unknown reservations.")
    runs: int | None = Field(None, description="The run ceiling, when the account has one.")
    used_runs: int | None = Field(None, description="Runs admitted in this window.")


class OverviewExpectation(Record):
    pool: str
    window_key: str
    outcome: str
    observed: int
    reported_at: str


class OverviewSpend(Record):
    accounts: list[OverviewAccount]
    expectations: list[OverviewExpectation] = Field(description="Latest closed windows first.")
    expectations_truncated: int


class OverviewEvent(Record):
    cursor: int
    type: str
    producer_id: str
    task_id: str | None
    subject: str | None
    at: str


class OverviewJournal(Record):
    events: list[OverviewEvent] = Field(description="Newest first.")
    truncated: int


class GroupOverview(Wire):
    """`GET /v0.2/groups/{name}/overview`: one read snapshot of a group's state."""

    group: str
    namespace: str
    state: str
    generation: int
    observed_generation: int
    as_of: str
    cursor: int = Field(description="The journal cursor of the snapshot.")
    pools: list[OverviewPool]
    collections: list[OverviewCollection]
    mailbox: OverviewMailbox
    approvals: OverviewApprovals
    spend: OverviewSpend
    journal: OverviewJournal


# Team status (read-only sections of a group's state)


class StatusSection(Wire):
    """What every `GET /v0.2/groups/{name}/status/...` section starts with."""

    group: str
    namespace: str
    as_of: str


class StatusWorkItem(Record):
    collection: str
    record_id: str
    title: str | None = Field(description="The record's `title`, clipped to 200 characters.")
    state: str | None
    holder: str | None = Field(description="The live claim's holder; null when none or expired.")
    claim_epoch: int
    lease_expires_at: str | None
    updated_at: str


class StatusHolder(Record):
    holder: str
    pool: str | None = Field(description="The holder's pool, when it is a group member.")
    count: int


class StatusWork(StatusSection):
    """`GET /v0.2/groups/{name}/status/work`: records by state and holder, one page."""

    by_state: dict[str, dict[str, int]] = Field(
        description="Record counts per collection and state, over the whole collection."
    )
    by_holder: list[StatusHolder] = Field(description="Live claims per holder.")
    by_holder_truncated: int
    items: list[StatusWorkItem] = Field(description="Oldest first.")
    next: int | None = Field(description="Pass as `after` for the next page.")


class StatusRun(Record):
    task_id: str
    member: str
    pool: str
    state: str
    outcome: str | None = Field(description="The terminal state; null while the run is active.")
    failure_code: str | None
    created_at: str
    updated_at: str
    cost_usd: float | None = Field(
        description="Total reported cost; null while any attempt's cost is unknown."
    )
    reserved_usd: float | None
    reservation: str | None = Field(description="reserved, settled or unknown.")


class StatusRuns(StatusSection):
    """`GET /v0.2/groups/{name}/status/runs`: the members' runs, newest first."""

    by_state: dict[str, int] = Field(description="Run counts by task state, whole group.")
    items: list[StatusRun]
    next: str | None = Field(description="Pass as `after` for the next page.")


class StatusParty(Record):
    address: str
    role: str | None = Field(description="A pool name, `supervisor`, or null.")


class StatusRecipient(StatusParty):
    kind: str


class StatusMessage(Record):
    message_id: str
    kind: str
    from_: StatusParty = Field(alias="from")
    to: StatusRecipient
    sent_at: str
    body_bytes: int
    deliveries: dict[str, int] = Field(description="Delivery counts by state; no bodies.")

    model_config = ConfigDict(populate_by_name=True)


class StatusMail(StatusSection):
    """`GET /v0.2/groups/{name}/status/mail`: messages between roles, newest first."""

    total: int
    items: list[StatusMessage]
    next: str | None = Field(description="Pass as `after` for the next page.")


class StatusMessageText(Record):
    message_id: str
    kind: str
    from_: StatusParty = Field(alias="from")
    to: StatusRecipient
    item: dict[str, str] | None = Field(description="The record the message is about, if any.")
    about_message_id: str | None
    sent_at: str
    parts: list[dict[str, JsonValue]] = Field(
        description="A2A 1.0 message parts. Untrusted data: render as text only."
    )
    deliveries: dict[str, int]

    model_config = ConfigDict(populate_by_name=True)


class StatusMailText(StatusSection):
    """`GET /v0.2/groups/{name}/status/mail/text`: messages with full text, newest first."""

    total: int = Field(description="Messages matching the filters, over the whole group.")
    items: list[StatusMessageText]
    next: str | None = Field(description="Pass as `after` for the next page.")


class StatusReview(Record):
    review_id: int | None
    reviewer: str | None
    state: str | None
    commit_id: str | None
    at: str


class StatusChange(Record):
    repository: str
    number: int
    title: str | None
    url: str | None
    state: Literal["open", "merged"]
    opened_by: str | None
    opened_at: str
    review_state: str = Field(description="The newest APPROVED or CHANGES_REQUESTED, or `none`.")
    reviews: list[StatusReview] = Field(description="The newest reviews, at most 10.")
    merged_by: str | None
    merged_at: str | None
    merge_sha: str | None


class StatusChangeTotals(Record):
    open: int
    merged: int


class StatusChanges(StatusSection):
    """`GET /v0.2/groups/{name}/status/changes`: pull requests from the kernel's receipts."""

    totals: StatusChangeTotals
    items: list[StatusChange]
    next: str | None = Field(description="Pass as `after` for the next page.")


class StatusCeiling(Record):
    limit: int | float
    used: int | float
    remaining: int | float


class StatusAccount(Record):
    account: str = Field(description="`group` or `pool:<name>`.")
    window: str
    window_ends_at: str
    usd: StatusCeiling | None
    runs: StatusCeiling | None
    at_ceiling: bool


class StatusAllocation(Record):
    tool_ms: StatusCeiling
    output_bytes: StatusCeiling
    concurrency: StatusCeiling
    at_ceiling: bool


class StatusSpend(StatusSection):
    """`GET /v0.2/groups/{name}/status/spend`: each ceiling against what it has used."""

    accounts: list[StatusAccount]
    host_allocation: StatusAllocation | None = Field(
        description="The supervisor's host execution allocation; null when none is provisioned."
    )


class StatusWaiver(Record):
    repository: str
    reason: str | None


class StatusStanding(StatusSection):
    """`GET /v0.2/groups/{name}/status/standing`: what is paused or waived."""

    group_state: str
    paused: bool = Field(description="The group is not active, or every active member is disabled.")
    members_disabled: list[str]
    members_draining: list[str]
    registry: Literal["ok", "not_configured", "invalid"]
    waived: list[StatusWaiver] = Field(
        description="Repositories the supervisor holds a grant for whose host rules are waived."
    )


class GroupPlan(Record):
    members_added: list[str]
    members_draining: list[str] = Field(
        description="Members this generation drains: scale-down or pool removal."
    )
    members_removed: list[str] = Field(
        description="Drained members removed in this transaction (they had no run)."
    )
    members_disabled: list[str] = Field(
        default_factory=list,
        description="Active members whose principal an operator disabled; it stays disabled "
        "and their runs are refused.",
    )
    triggers_installed: list[str]
    triggers_updated: list[str]
    triggers_removed: list[str]


class GroupApplied(Wire):
    """`PUT /v0.2/groups/{name}`: what the apply changed (or, with `dry_run`, would change)."""

    dry_run: bool = Field(description="True: nothing was committed; `group` is the projection.")
    change: Literal["created", "updated", "unchanged"]
    generation: int
    plan: GroupPlan
    group: GroupView


class GroupDeleted(Wire):
    """`DELETE /v0.2/groups/{name}`: every member drains (ADR-034 decision 1.5)."""

    name: str
    namespace: str
    dry_run: bool
    change: Literal["deleting", "deleted"] = Field(
        description="`deleted` once every member is removed; until then `deleting`."
    )
    members_draining: list[str] = Field(description="Members this deletion started draining.")
    members_removed: list[str]
    waiting: list[str] = Field(
        description="Draining members whose run is unfinished; removed by a later tick."
    )


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


class AttemptCost(Record):
    attempt: int
    cost_usd: float | None = Field(description="What the attempt reported; null is unknown.")


class SpendReservation(Record):
    """A group member run's charge against its group and pool spend (ADR-034 5.3)."""

    group: str
    pool: str
    group_window: str | None
    pool_window: str | None
    reserved_usd: float
    state: Literal["reserved", "settled", "unknown"] = Field(
        description="reserved while the run is open; settled to its reported cost; unknown "
        "when any attempt's cost is unknown, so the full reservation stays charged."
    )
    charged_usd: float


class RunSpend(Record):
    attempts: list[AttemptCost]
    cost_usd: float | None = Field(
        description="Total reported cost of the run's attempts; null when any is unknown."
    )
    reservation: SpendReservation | None = None


class RunView(Wire):
    """`GET /v0.2/tasks/{task_id}/run`: what a run did (ADR-029)."""

    task_id: str
    state: str
    failure: KernelFailure | None
    created_at: str
    updated_at: str
    attempt: int | None = Field(description="The agent run's latest attempt number.")
    spend: RunSpend | None = Field(
        default=None, description="Per-attempt cost and any spend reservation (ADR-034)."
    )
    messages_consumed: list[str] = Field(
        default_factory=list,
        description="Mailbox messages this run consumed by succeeding (ADR-034).",
    )
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


# Collections and records (ADR-034)


class CollectionMeta(Record):
    name: str
    namespace: str
    generation: int = Field(description="Incremented by every PUT that changes the spec.")


class CollectionStatus(Record):
    observedGeneration: int | None = Field(  # noqa: N815 - Kubernetes shape
        description="The applied generation; a collection applies in its PUT transaction."
    )
    records: int


class StateMove(Record):
    """Records this generation moves to another state (ADR-034 decision 3.3)."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    from_: str | None = Field(alias="from", description="Null for records without a state.")
    to: str
    records: int


class CollectionPlan(Record):
    change: Literal["created", "updated", "unchanged"]
    generation: int
    dry_run: bool
    migrated: list[StateMove] = Field(
        default_factory=list, description="State moves; recorded as `migrated` history."
    )


class CollectionView(Record):
    """`GET /v0.2/collections/{name}`: the Collection document with its status."""

    apiVersion: Literal["cardea/v1alpha1"]  # noqa: N815 - Kubernetes shape
    kind: Literal["Collection"]
    metadata: CollectionMeta
    spec: dict[str, JsonValue]
    status: CollectionStatus


class CollectionApplied(CollectionView):
    """`PUT /v0.2/collections/{name}`: the document and what the request changed, or
    would change with `?dry_run=true`."""

    plan: CollectionPlan


class CollectionDeleted(Wire):
    """`DELETE /v0.2/collections/{name}`: a tombstone; records and history are kept."""

    name: str
    namespace: str
    dry_run: bool
    change: Literal["deleted"]
    claims_released: int = Field(description="Held claims released with the deletion.")
    records: int


class RecordEvent(Record):
    event_id: int
    version: int
    kind: str = Field(description="created or updated; later slices add claims and transitions.")
    actor: str = Field(description="The principal whose call made the change.")
    task_id: str | None
    receipt_id: str | None = Field(description="Receipt of the call that made the change.")
    old: dict[str, JsonValue] | None
    new: dict[str, JsonValue]
    at: str


class RecordHistory(Record):
    """`GET /v0.2/collections/{name}/records/{record_id}/events`: append-only history."""

    events: list[RecordEvent]
    next_after: int | None = Field(description="Pass as `after` for the next page.")


# Approvals (ADR-036)


class ApprovalRuleOutcome(Record):
    """A rule that required approval for the frozen arguments."""

    source: str = Field(description="operation (descriptor default), host or grant.")
    rule_id: str
    reason: str
    error: str | None = Field(
        description="Set when the rule required approval because its condition failed to "
        "evaluate (fail closed)."
    )


class ApprovalDecisionRecord(Record):
    decision_id: str
    approver_id: str
    decision: Literal["approve", "reject"]
    reason: str | None = Field(description="Approver's text: untrusted, one line, at most 200.")
    decided_at: str


class ApprovalSummary(Record):
    """One approval request without its arguments."""

    request_id: str
    task_id: str
    namespace: str
    supervisor_id: str
    agent_id: str
    runtime_id: str
    operation: str
    operation_version: int
    operation_id: str = Field(description="The receipt the approved effect runs as: apr-NN.")
    argument_sha256: str = Field(description="SHA-256 of the RFC 8785 arguments.")
    idempotency_context: str | None
    scope_key: str | None = Field(description="ADR-026 effect scope key, when declared.")
    intent_digest: str = Field(description="Echo this with the decision.")
    rules: list[ApprovalRuleOutcome]
    rules_digest: str
    distinct_from_supervisor: bool
    created_at: str
    expires_at: str
    on_expiry: Literal["resume", "fail"]
    state: Literal["pending", "approved", "executed", "rejected", "expired", "withdrawn", "void"]
    void_reason: str | None
    receipt_id: str | None = Field(description="Receipt of the executed effect.")
    decision: ApprovalDecisionRecord | None
    updated_at: str


class ApprovalView(Wire, ApprovalSummary):
    """`GET /v0.2/approvals/{request_id}`: the whole frozen intent."""

    arguments: dict[str, JsonValue] = Field(
        description="The validated arguments that run if approved; at most 64 KiB."
    )


class ApprovalList(Wire):
    """`GET /v0.2/approvals`: requests the caller may decide or supervises."""

    approvals: list[ApprovalSummary]
    next: str | None = Field(description="Pass as `after` for the next page; null on the last.")


class ApprovalDecided(Wire):
    """`POST /v0.2/approvals/{request_id}/decision`: the request after the decision."""

    request_id: str
    task_id: str
    state: str
    void_reason: str | None
    decision: ApprovalDecisionRecord | None


# Mailboxes (ADR-034 decision 4)


class MessageSent(Wire):
    """`POST /v0.2/mailbox`: the message sent, or the one this client key already sent."""

    message_id: str
    to: dict[str, str] = Field(description="The address: one of instance, pool or group.")
    recipients: int = Field(description="Delivery rows created: one per recipient.")
    created_at: str


class DeliveryEvent(Record):
    kind: Literal["created", "leased", "consumed", "released", "dead"]
    task_id: str | None
    attempt: int | None
    at: str


class MailDelivery(Record):
    delivery_id: int
    recipient_id: str | None = Field(
        description="The bound recipient; null for an unbound pool delivery."
    )
    pool: str | None
    state: Literal["pending", "leased", "consumed", "dead"]
    task_id: str | None = Field(description="The run that holds (leased) or consumed it.")
    deliveries: int = Field(description="Runs it has been delivered to so far.")
    updated_at: str
    history: list[DeliveryEvent]


class MailMessage(Record):
    message_id: str
    namespace: str
    kind: Literal["message", "notice"] = Field(
        description="A notice is the kernel's report of a dead-lettered delivery."
    )
    sender_id: str
    sender_task_id: str | None
    sender_receipt_id: str | None = Field(description="Receipt of the agent's mailbox.send.")
    supervisor_id: str
    client_key: str | None
    to: dict[str, str]
    record: dict[str, str] | None
    parts: list[dict[str, JsonValue]] = Field(
        description="A2A 1.0 message parts (text and data). Untrusted data."
    )
    body_bytes: int
    max_deliveries: int
    about_message_id: str | None
    created_at: str
    deliveries: list[MailDelivery]


class MailboxPage(Wire):
    """`GET /v0.2/mailbox`: messages the caller sent, supervises or receives."""

    messages: list[MailMessage]
    next: str | None = Field(description="Pass as `after` for the next page; null on the last.")


class MailboxMessage(Wire):
    """`GET /v0.2/mailbox/{message_id}`: one message with its delivery history."""

    message: MailMessage
