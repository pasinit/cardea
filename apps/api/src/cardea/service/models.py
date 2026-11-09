"""Explicit v0.2 wire/config records; existing v0.1 task and receipt bytes are preserved."""

from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Any, Literal, Self
from zoneinfo import ZoneInfo

import rfc8785
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

from cardea.kernel.models import Identifier, ObjectVersion, OperationName, Task, Timestamp
from cardea.service.approvals import HostApprovals
from cardea.service.schedule import validate_schedule

# ADR-036 bounds: approval waits per agent run, and the longest wait (30 days).
DEFAULT_APPROVAL_WAITS = 4
MAX_APPROVAL_WAIT = 30 * 24 * 3600

Right = Literal[
    "objects.read",
    "objects.write",
    "tasks.inspect",
    "tasks.cancel",
    "tasks.resume",
    "events.read",
    "tasks.submit",
    "host.sha256",
    "host.copy",
    "models.invoke",
    "tasks.delegate",
    "tasks.schedule",
    # Supervisor-managed agent groups and collections (ADR-034).
    "groups.manage",
]

# An installed agent harness (ADR-033) and a provider model id it may run.
HarnessID = Annotated[str, Field(strict=True, pattern=r"^[a-z][a-z0-9-]{0,31}$")]
ModelID = Annotated[
    str,
    Field(strict=True, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]*$"),
]
# Right `harness.use` on one harness, written `harness:<id>` (ADR-033).
HarnessGrant = Annotated[str, Field(strict=True, pattern=r"^harness:[a-z][a-z0-9-]{0,31}$")]

# A collection name (ADR-034) and an action on its records: `create`, `read`, `update`,
# and in later slices `claim` and transition names.
COLLECTION_NAME = r"[a-z][a-z0-9_-]{0,63}"
RECORD_ACTION = r"[a-z][a-z0-9_]{0,63}"
CollectionName = Annotated[str, Field(strict=True, pattern=f"^{COLLECTION_NAME}$")]
# One action on the records of one collection, written `record:<collection>/<action>`
# like `harness:<id>` (ADR-034 decision 2.3). It authorizes the built-in `records.*`
# operations; the collection itself may not exist yet when the grant is written.
RecordGrant = Annotated[
    str, Field(strict=True, pattern=f"^record:{COLLECTION_NAME}/{RECORD_ACTION}$")
]
RECORD_GRANT_PREFIX = "record:"
# Built-in, in-process record operations (ADR-034). Their authority is the resource-
# qualified grant for the collection and action each call names, checked under the live
# ceiling at every call; the operation name itself is not a grant.
RECORD_OPERATIONS: frozenset[str] = frozenset(
    {
        "records.create",
        "records.get",
        "records.query",
        "records.update",
        "records.claim",
        "records.release",
        "records.claimed",
    }
)


def record_grant(collection: str, action: str) -> str:
    return f"{RECORD_GRANT_PREFIX}{collection}/{action}"


# The `approve` right on one installed operation, inside a task's own ceiling, written
# `approve:<operation>` (ADR-036 item 2): what lets an agent run, such as a reviewer, decide
# that operation's approval requests through the built-in `approvals.decide`.
APPROVE_GRANT_PREFIX = "approve:"
ApproveGrant = Annotated[
    str,
    Field(
        strict=True,
        max_length=136,
        pattern=r"^approve:[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$",
    ),
]
# Built-in, in-process approval operations (ADR-036 slice 4), authorized per call by an
# `approve:<operation>` grant for the request's operation, never by their own name.
APPROVAL_OPERATIONS: frozenset[str] = frozenset({"approvals.decide"})
# Built-in operations whose name is not itself a grant.
BUILTIN_OPERATIONS: frozenset[str] = RECORD_OPERATIONS | APPROVAL_OPERATIONS


def approve_grant(operation: str) -> str:
    return f"{APPROVE_GRANT_PREFIX}{operation}"


# Right to send mail to one address, written `mail:<address>` (ADR-034 decision 4.2): an
# instance (a principal ID), a pool (`<group>.<pool>`) or a group (`<group>`).
MailGrant = Annotated[str, Field(strict=True, pattern=r"^mail:[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]

# A kernel right, the name of an installed operation (ADR-023), a harness grant, a record
# grant, a mail grant or an approve grant. Installed names are checked against the live
# registry at provisioning, admission and dispatch; harness grants against the installed
# harnesses.
Grant = Right | OperationName | HarnessGrant | RecordGrant | MailGrant | ApproveGrant

RIGHTS: tuple[Right, ...] = (
    "objects.read",
    "objects.write",
    "tasks.inspect",
    "tasks.cancel",
    "tasks.resume",
    "events.read",
    "tasks.submit",
)


def now() -> datetime:
    return datetime.now(UTC)


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class Wire(Record):
    schema_version: Literal["0.2"] = "0.2"


# Canonical (RFC 8785) size of one task request: the host default and the largest value a
# host owner may configure (`max_task_request_bytes`). Admission and trigger installation
# enforce the configured value; models enforce only the absolute ceiling.
DEFAULT_MAX_TASK_REQUEST_BYTES = 16384
MAX_TASK_REQUEST_BYTES = 1048576


def request_size(request: "TaskRequest") -> int:
    return len(rfc8785.dumps(request.model_dump(mode="json")))


class TaskRequest(Wire):
    request_id: Identifier
    agent_id: Identifier
    namespace: Identifier
    objective: Annotated[str, Field(strict=True, min_length=1, max_length=1024)]
    payload: dict[str, JsonValue]
    grants: Annotated[tuple[Grant, ...], Field(min_length=1, max_length=7)] = RIGHTS
    timeout_seconds: Annotated[int, Field(strict=True, ge=5, le=3600)] = 300
    wait_for_event: Annotated[bool, Field(strict=True)] = False
    parent_task_id: Identifier | None = None

    @model_validator(mode="after")
    def bounded(self) -> Self:
        try:
            encoded = rfc8785.dumps(self.model_dump(mode="json"))
        except (ValueError, TypeError) as error:
            raise ValueError("Payload is outside the supported RFC 8785 domain") from error
        if len(encoded) > MAX_TASK_REQUEST_BYTES:
            raise ValueError("Request is too large")
        if len(set(self.grants)) != len(self.grants):
            raise ValueError("Request repeats a grant")
        if not {"objects.read", "objects.write"} <= set(self.grants):
            raise ValueError("The installed deterministic runtime requires object read and write")
        return self

    def digest(self, principal: str) -> str:
        return sha256(
            rfc8785.dumps({"principal": principal, "request": self.model_dump(mode="json")})
        ).hexdigest()


class BaseHostTaskRequest(TaskRequest):
    operation: Literal["host.sha256", "host.copy"]
    grants: Annotated[tuple[Right, ...], Field(min_length=1, max_length=9)]
    wait_for_event: Literal[False] = False
    timeout_seconds: Annotated[int, Field(strict=True, ge=30, le=3600)] = 300

    @model_validator(mode="after")
    def host_grant(self) -> Self:
        if self.operation not in self.grants:
            raise ValueError("Installed host operation requires an explicit grant")
        return self


class HostTaskRequest(BaseHostTaskRequest):
    parent_task_id: None = None


class HostObjectTaskRequest(HostTaskRequest):
    input_object: ObjectVersion
    # Inline and stored input cannot be combined or silently override each other.
    payload: Annotated[dict[str, JsonValue], Field(max_length=0)] = Field(default_factory=dict)


class ChildWork(Record):
    agent_id: Identifier
    operation: Literal["host.sha256", "host.copy"]
    payload: dict[str, JsonValue]


class DelegationTaskRequest(TaskRequest):
    children: Annotated[tuple[ChildWork, ...], Field(min_length=1, max_length=4)]
    payload: Annotated[dict[str, JsonValue], Field(max_length=0)] = Field(default_factory=dict)
    grants: Annotated[tuple[Right, ...], Field(min_length=1, max_length=10)]
    parent_task_id: None = None
    wait_for_event: Literal[False] = False
    timeout_seconds: Annotated[int, Field(strict=True, ge=120, le=3600)] = 600
    tool_milliseconds: Annotated[int, Field(strict=True, ge=0, le=40000)] = 20000
    output_bytes: Annotated[int, Field(strict=True, ge=0, le=294912)] = 163840
    concurrent_children: Annotated[int, Field(strict=True, ge=1, le=4)] = 2
    minimum_success: Annotated[int, Field(strict=True, ge=1, le=4)] = 1

    @model_validator(mode="after")
    def delegation_grants(self) -> Self:
        required = {
            "tasks.delegate",
            "tasks.submit",
            "tasks.inspect",
            "tasks.cancel",
            "events.read",
            *(child.operation for child in self.children),
        }
        if not required <= set(self.grants) or self.minimum_success > len(self.children):
            raise ValueError("Delegation needs explicit grants and an attainable objective")
        if any(child.agent_id == self.agent_id for child in self.children):
            raise ValueError("Coordinator and peer must be distinct logical agents")
        return self


class OperationTaskRequest(TaskRequest):
    """One invocation of an installed operation; arguments are checked at admission."""

    operation: OperationName
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    # Shared by every invocation that may repeat the same effect, e.g. one scheduled slot.
    # Defaults to the task itself.
    idempotency_context: Identifier | None = None
    # The scheduled slot this task serves, in its trigger's timezone; set by the trigger.
    slot_time: AwareDatetime | None = None
    payload: Annotated[dict[str, JsonValue], Field(max_length=0)] = Field(default_factory=dict)
    grants: Annotated[tuple[Grant, ...], Field(min_length=1, max_length=16)]
    parent_task_id: None = None
    wait_for_event: Literal[False] = False

    @model_validator(mode="after")
    def operation_grant(self) -> Self:
        if self.operation in APPROVAL_OPERATIONS:
            raise ValueError("approvals.decide runs only as an agent-run tool call")
        if self.operation in RECORD_OPERATIONS:
            if not any(g.startswith(RECORD_GRANT_PREFIX) for g in self.grants):
                raise ValueError("Record operations require a record:<collection>/<action> grant")
        elif self.operation not in self.grants:
            raise ValueError("Installed operation requires an explicit grant")
        return self


class Expectation(Record):
    """A completion condition on a run's successful calls of one operation (ADR-029)."""

    operation: OperationName
    min_calls: Annotated[int, Field(strict=True, ge=0, le=100)] = 1
    max_calls: Annotated[int, Field(strict=True, ge=0, le=100)] | None = None

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.max_calls is not None and self.max_calls < self.min_calls:
            raise ValueError("max_calls must not be below min_calls")
        return self

    def describe(self) -> str:
        bound = (
            f"exactly {self.min_calls}"
            if self.max_calls == self.min_calls
            else f"at least {self.min_calls}"
            + (f" and at most {self.max_calls}" if self.max_calls is not None else "")
        )
        return f"{bound} successful call(s) of {self.operation.replace('.', '-')}"


class ContextCall(Record):
    """A read-only operation whose output an agent run receives before its session."""

    operation: OperationName
    arguments: dict[str, JsonValue] = Field(default_factory=dict)


class InboundEvent(Record):
    """The CloudEvents 1.0 event that started an event-triggered run (ADR-035).

    Set by the trigger, never by its template. The run receives it as untrusted data.
    """

    id: Annotated[str, Field(strict=True, min_length=1, max_length=256)]
    source: Annotated[str, Field(strict=True, min_length=1, max_length=512)]
    type: Annotated[str, Field(strict=True, min_length=1, max_length=256)]
    subject: Annotated[str, Field(strict=True, max_length=512)] | None = None
    time: Annotated[str, Field(strict=True, max_length=64)] | None = None
    data: JsonValue = None


class AgentRunRequest(TaskRequest):
    """One bounded agent session whose only tools are installed operations (ADR-027)."""

    instructions: Annotated[str, Field(strict=True, min_length=1, max_length=65536)]
    operations: Annotated[tuple[OperationName, ...], Field(min_length=1, max_length=32)]
    payload: Annotated[dict[str, JsonValue], Field(max_length=0)] = Field(default_factory=dict)
    grants: Annotated[tuple[Grant, ...], Field(min_length=1, max_length=48)]
    parent_task_id: None = None
    wait_for_event: Literal[False] = False
    timeout_seconds: Annotated[int, Field(strict=True, ge=60, le=3600)] = 1800
    # Effects in this run share one idempotency context, e.g. a scheduled slot.
    idempotency_context: Identifier | None = None
    # The scheduled slot this run serves, in its trigger's timezone; set by the trigger.
    slot_time: AwareDatetime | None = None
    max_attempts: Annotated[int, Field(strict=True, ge=1, le=3)] = 2
    attempt_timeout_seconds: Annotated[int, Field(strict=True, ge=30, le=3600)] = 900
    max_turns: Annotated[int, Field(strict=True, ge=1, le=200)] = 40
    max_tool_calls: Annotated[int, Field(strict=True, ge=1, le=500)] = 100
    max_cost_usd: Annotated[float, Field(strict=True, gt=0, le=50)] = 2.0
    # The run succeeds only when every expectation holds over its successful calls.
    expect: Annotated[tuple[Expectation, ...], Field(max_length=16)] = ()
    # Read-only operations invoked before each attempt's session; outputs go into the prompt.
    context: Annotated[tuple[ContextCall, ...], Field(max_length=4)] = ()
    # The harness and model to run (ADR-033); defaults come from the host's registry.
    harness: HarnessID | None = None
    model: ModelID | None = None
    # The inbound event this run serves; set by an event trigger (ADR-035).
    trigger_event: InboundEvent | None = None
    # Approval waits (ADR-036 slice 4): how many approval requests the run may make (4 when
    # unset), and seconds added to the deadline at admission so a wait fits (0 when unset).
    # Unset fields are left out of the request's dump, so its digest is unchanged.
    max_approval_waits: Annotated[int, Field(strict=True, ge=1, le=16)] | None = None
    approval_wait_seconds: Annotated[int, Field(strict=True, ge=0, le=MAX_APPROVAL_WAIT)] | None = (
        None
    )

    @model_serializer(mode="wrap")
    def _omit_unset_approval_fields(self, handler: SerializerFunctionWrapHandler) -> Any:
        document = handler(self)
        if isinstance(document, dict):
            for key in ("max_approval_waits", "approval_wait_seconds"):
                if document.get(key, 0) is None:
                    del document[key]
        return document

    @property
    def approval_waits(self) -> int:
        return self.max_approval_waits or DEFAULT_APPROVAL_WAITS

    @model_validator(mode="after")
    def run_grants(self) -> Self:
        if len(set(self.operations)) != len(self.operations):
            raise ValueError("Operations must be distinct")
        if any(e.operation not in self.operations for e in self.expect):
            raise ValueError("Expectations must name exposed operations")
        # Built-in record operations are authorized per call by record grants (ADR-034),
        # `approvals.decide` by approve grants (ADR-036).
        named = {o for o in self.operations if o not in BUILTIN_OPERATIONS}
        if not {"models.invoke", *named} <= set(self.grants):
            raise ValueError("Agent runs need model invocation and every exposed operation")
        if any(c.operation in APPROVAL_OPERATIONS for c in self.context):
            raise ValueError("approvals.decide cannot be a context operation")
        context = {c.operation for c in self.context if c.operation not in RECORD_OPERATIONS}
        if not context <= set(self.grants):
            raise ValueError("Context operations must be granted")
        return self


class TaskView(Wire):
    task: Task
    supervisor_id: Identifier
    agent_id: Identifier
    runtime_principal_id: Identifier
    namespace: Identifier
    grants: tuple[Grant, ...]
    request_digest: str
    digest_format: Literal["sha256-rfc8785-v1"] = "sha256-rfc8785-v1"
    # What a waiting task waits for, from the service tables: {"approval": request_id}.
    waiting_for: dict[str, str] | None = None


class Signal(Wire):
    request_id: Identifier


class ErrorBody(Wire):
    code: str
    message: str


class OIDCConfig(Record):
    issuer: str
    audience: str = "cardea-kernel"
    client_id: str
    client_secret_file: Path
    # Explicit loopback-only exception for a disposable development provider.
    allow_loopback_http: bool = False


class AgentSessionConfig(Record):
    """Host-owner selection of the agent runtime for agent runs (ADR-027)."""

    runtime: Literal["claude-agent-sdk"] = "claude-agent-sdk"
    model: Annotated[str, Field(strict=True, min_length=1, max_length=128)] | None = None
    # Exactly one private credential source: an API key or a subscription OAuth token.
    api_key_file: Path | None = None
    oauth_token_file: Path | None = None
    concurrent_sessions: Annotated[int, Field(strict=True, ge=1, le=8)] = 1
    # Run each session in a confined worker under this host-owner profile (ADR-028). The
    # profile holds the credential, so the service is given none.
    confined_profile: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")] | None = None
    confined_worker: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")] | None = None

    @model_validator(mode="after")
    def credential_source(self) -> Self:
        if (self.confined_profile is None) != (self.confined_worker is None):
            raise ValueError("A confined session names both a profile and a worker")
        if self.confined_profile is not None:
            if self.api_key_file is not None or self.oauth_token_file is not None:
                raise ValueError("A confined session's credential belongs to its profile")
            return self
        if (self.api_key_file is None) == (self.oauth_token_file is None):
            raise ValueError("Select exactly one private API key or OAuth token file")
        return self


class NetworkListener(Record):
    """Optional TLS 1.3 listener for remote clients (ADR-025); there is no plaintext option.

    Transport only: the bearer credential remains the sole identity proof. A client
    certificate (when `client_ca_file` is set) and the peer address confer nothing.
    """

    host: Annotated[str, Field(min_length=1, max_length=253, pattern=r"^[A-Za-z0-9.:-]+$")]
    port: Annotated[int, Field(strict=True, ge=1, le=65535)]
    certificate_file: Path
    key_file: Path
    # Optional mutual TLS: refuse connections without a certificate chaining to this CA.
    client_ca_file: Path | None = None


class AttemptLogging(Record):
    """Observability of agent-run attempts: heartbeat, harness output bounds, log files."""

    # Seconds between "attempt still running" lines per active attempt; 0 turns them off.
    heartbeat_seconds: Annotated[int, Field(strict=True, ge=0, le=3600)] = 60
    # Harness stderr (and SDK diagnostics): bytes kept per line, and logged per attempt.
    output_line_bytes: Annotated[int, Field(strict=True, ge=256, le=65536)] = 2048
    output_max_bytes: Annotated[int, Field(strict=True, ge=0, le=16777216)] = 262144
    # Also write each attempt's records to <data_dir>/attempt-logs/<task_id>-<attempt>.log,
    # keeping the newest `files_retained` files.
    files: Annotated[bool, Field(strict=True)] = False
    files_retained: Annotated[int, Field(strict=True, ge=1, le=100000)] = 200


class AgentTools(Record):
    """How an agent run's harness may use its per-attempt tool endpoint (ADR-027)."""

    # Seconds the harness's MCP client waits for one tool call before giving up. None
    # derives it per attempt: the slowest exposed operation's worst case (its timeout plus
    # any worker-slot queue allowance, within the lease) plus a settle margin, capped at
    # the attempt's remaining time. Never the client's own default (about a minute).
    tool_timeout_seconds: Annotated[int, Field(strict=True, ge=1, le=86400)] | None = None
    # Tool calls of one attempt that may be in flight (admitted, waiting or running) at
    # once; another call is refused before admission with the retryable `tool_busy`.
    max_concurrent_tool_calls: Annotated[int, Field(strict=True, ge=1, le=64)] = 4


class EventTriggers(Record):
    """Inbound event intake for event triggers (ADR-035)."""

    # Largest accepted event body: the whole signed CloudEvent, data included. The
    # listener's `max_request_bytes` and `max_task_request_bytes` still apply on top.
    max_event_bytes: Annotated[int, Field(strict=True, ge=1024, le=262144)] = 8192
    # Largest accepted distance between a signature's timestamp and the kernel clock.
    timestamp_tolerance_seconds: Annotated[int, Field(strict=True, ge=1, le=300)] = 300


class ServiceConfig(Record):
    data_dir: Path
    spiffe_socket: str
    trust_domain: str
    audience: str = "cardea-kernel"
    runtime_spiffe_id: str
    oidc: OIDCConfig | None = None
    host_execution: Annotated[bool, Field(strict=True)] = False
    # Private directory of installed operation descriptors (ADR-023).
    operations_dir: Path | None = None
    agent_session: AgentSessionConfig | None = None
    # Root-owned registry of installed agent harnesses (ADR-033); replaces `agent_session`.
    harnesses_file: Path | None = None
    rpc_timeout: Annotated[float, Field(gt=0, le=10)] = 2.0
    # Off unless configured; the private Unix socket is always served (ADR-025).
    network: NetworkListener | None = None
    # Request-body cap for every listener. Responses stay capped at 256 KiB.
    max_request_bytes: Annotated[int, Field(strict=True, ge=4096, le=1048576)] = 32768
    # Bytes of each call's arguments and result the run view shows inline (as a preview, and
    # in full when they fit); larger values are paged from their own routes (ADR-029).
    run_preview_bytes: Annotated[int, Field(strict=True, ge=0, le=16384)] = 2048
    # Canonical size cap of one task request, checked at admission and trigger installation.
    max_task_request_bytes: Annotated[
        int, Field(strict=True, ge=4096, le=MAX_TASK_REQUEST_BYTES)
    ] = DEFAULT_MAX_TASK_REQUEST_BYTES
    # Service log level on stderr (journald under systemd); CARDEA_LOG_LEVEL overrides it.
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    attempt_logging: AttemptLogging = AttemptLogging()
    agent_tools: AgentTools = AgentTools()
    event_triggers: EventTriggers = EventTriggers()

    @model_validator(mode="after")
    def one_harness_source(self) -> Self:
        if self.agent_session is not None and self.harnesses_file is not None:
            raise ValueError("Configure agent_session or harnesses_file, not both")
        return self


class Principal(Record):
    principal_id: Identifier
    kind: Literal["human", "agent", "service"]
    disabled: Annotated[bool, Field(strict=True)] = False
    auth_kind: Literal["spiffe", "oidc"] | None = None
    issuer: str = ""
    subject: str = ""
    runtime_id: Identifier | None = None

    @model_validator(mode="after")
    def external(self) -> Self:
        if self.principal_id == "platform":
            raise ValueError("The platform identifier is reserved for policy")
        if self.auth_kind and (not self.issuer or not self.subject):
            raise ValueError("External identity requires exact issuer and subject")
        if self.kind == "agent" and not self.runtime_id:
            raise ValueError("Logical agents require an administratively assigned runtime")
        return self


class Permission(Record):
    principal_id: Identifier
    namespace: Identifier
    operation: Grant
    # Casbin modes this row installs: `allow` (may use), `delegate` (may grant to a task;
    # needs `allow`) and `approve` (may decide approval requests, ADR-036).
    allow: Annotated[bool, Field(strict=True)] = True
    delegate: Annotated[bool, Field(strict=True)] = False
    approve: Annotated[bool, Field(strict=True)] = False
    expires_at: Timestamp

    @model_validator(mode="after")
    def some_mode(self) -> Self:
        if self.delegate and not self.allow:
            raise ValueError("delegate requires allow")
        if not (self.allow or self.approve):
            raise ValueError("A permission needs allow or approve")
        return self

    @property
    def modes(self) -> tuple[str, ...]:
        return tuple(
            mode
            for mode, wanted in (
                ("allow", self.allow),
                ("delegate", self.delegate),
                ("approve", self.approve),
            )
            if wanted
        )


class ObjectPermission(Record):
    principal_id: Identifier
    object: ObjectVersion
    operation: Literal["objects.read"] = "objects.read"
    delegate: Annotated[bool, Field(strict=True)] = False
    expires_at: Timestamp


class HostBudget(Record):
    principal_id: Identifier
    namespace: Identifier
    tool_milliseconds: Annotated[int, Field(strict=True, ge=0, le=10**12)]
    output_bytes: Annotated[int, Field(strict=True, ge=0, le=10**12)]
    concurrent_operations: Annotated[int, Field(strict=True, ge=1, le=100)] = 4


# The slot request ID `<trigger_id>.<YYYYMMDDTHHMMSSZ>` must still be an Identifier (ADR-024).
TriggerID = Annotated[str, Field(strict=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,110}$")]
TemplateKind = Literal["task", "host", "host-object", "operation", "delegation", "agent-run"]
TEMPLATES: dict[str, type[TaskRequest]] = {
    "task": TaskRequest,
    "host": HostTaskRequest,
    "host-object": HostObjectTaskRequest,
    "operation": OperationTaskRequest,
    "delegation": DelegationTaskRequest,
    "agent-run": AgentRunRequest,
}


# A CloudEvents context attribute name: lower-case letters and digits (CloudEvents 1.0).
EventAttribute = Annotated[str, Field(strict=True, pattern=r"^[a-z0-9]{1,20}$")]
EventText = Annotated[str, Field(strict=True, min_length=1, max_length=512)]
EventType = Annotated[str, Field(strict=True, min_length=1, max_length=256)]


class EventFilter(Record):
    """One CloudEvents Subscriptions API filter: `exact`, `prefix` or `suffix` on one
    context attribute. An event without that attribute does not match."""

    exact: dict[EventAttribute, EventText] | None = None
    prefix: dict[EventAttribute, EventText] | None = None
    suffix: dict[EventAttribute, EventText] | None = None

    @model_validator(mode="after")
    def one_dialect(self) -> Self:
        dialects = [d for d in (self.exact, self.prefix, self.suffix) if d is not None]
        if len(dialects) != 1 or len(dialects[0]) != 1:
            raise ValueError("A filter names one dialect with exactly one attribute")
        return self

    def matches(self, attributes: dict[str, str]) -> bool:
        for dialect, test in (
            (self.exact, str.__eq__),
            (self.prefix, str.startswith),
            (self.suffix, str.endswith),
        ):
            if dialect is not None:
                ((name, expected),) = dialect.items()
                value = attributes.get(name)
                return value is not None and bool(test(value, expected))
        return False


class EventMatch(Record):
    """Which signed inbound events start an event trigger's template (ADR-035)."""

    # The exact CloudEvents `source`, and the accepted `type` values.
    source: EventText
    types: Annotated[tuple[EventType, ...], Field(min_length=1, max_length=16)]
    # Every filter must match.
    filters: Annotated[tuple[EventFilter, ...], Field(max_length=8)] = ()
    signing: Literal["standard-webhooks"] = "standard-webhooks"
    # Host-owner private file of Standard Webhooks secrets (`whsec_...`), one per line.
    # Any of them verifies, so a secret is rotated without dropping deliveries.
    secret_file: Path

    @model_validator(mode="after")
    def absolute(self) -> Self:
        if not self.secret_file.is_absolute():
            raise ValueError("secret_file must be an absolute path")
        return self

    def matches(self, event: InboundEvent, attributes: dict[str, str]) -> bool:
        return (
            event.source == self.source
            and event.type in self.types
            and all(f.matches(attributes) for f in self.filters)
        )


class TriggerSpec(Record):
    """An installed trigger. A time trigger admits one task per scheduled slot (ADR-024);
    an event trigger admits one task per matching inbound event (ADR-035)."""

    trigger_id: TriggerID
    supervisor_id: Identifier
    # Time triggers: five-field POSIX/Vixie cron, evaluated on the wall clock of `timezone`.
    schedule: Annotated[str, Field(strict=True, min_length=9, max_length=128)] | None = None
    timezone: Annotated[str, Field(strict=True, min_length=1, max_length=64)] | None = None
    misfire: Literal["skip", "run_once_late", "grace"] | None = None
    grace_seconds: Annotated[int, Field(strict=True, ge=60, le=86400)] | None = None
    # Event triggers: the inbound events that start the template, instead of a schedule.
    event: EventMatch | None = None
    enabled: Annotated[bool, Field(strict=True)] = True
    template_kind: TemplateKind
    # A complete request of `template_kind` without `request_id`; validated at install and
    # again at every admission, because installed operations and policy can change.
    template: dict[str, JsonValue]

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.event is None:
            if self.schedule is None or self.timezone is None or self.misfire is None:
                raise ValueError("A time trigger needs schedule, timezone and misfire")
            validate_schedule(self.schedule, self.timezone)
            if (self.misfire == "grace") != (self.grace_seconds is not None):
                raise ValueError("grace_seconds is required for, and only for, the grace policy")
        else:
            if {self.schedule, self.timezone, self.misfire, self.grace_seconds} != {None}:
                raise ValueError("An event trigger has no schedule")
            if self.template_kind != "agent-run":
                raise ValueError("Event triggers admit agent runs")
        # A null slot_time is accepted so a dumped request model can serve as a template.
        if {"request_id", "idempotency_context"} & set(self.template) or self.template.get(
            "slot_time"
        ):
            raise ValueError("The trigger assigns request and idempotency IDs per slot")
        if self.template.get("trigger_event") is not None:
            raise ValueError("The trigger assigns the inbound event")
        if self.template.get("parent_task_id") is not None:
            raise ValueError("Triggers admit root tasks only")
        self.request("validation.20000101T000000Z")
        return self

    @property
    def kind(self) -> Literal["time", "event"]:
        return "time" if self.event is None else "event"

    def request(
        self,
        request_id: str,
        slot_time: datetime | None = None,
        event: InboundEvent | None = None,
    ) -> TaskRequest:
        """The request for one slot or event; `request_id` is its identity,
        `<trigger_id>.<slot>` or `<trigger_id>.e<event digest>`.

        Operation effects in one slot or event share that identity as their idempotency
        context (ADR-026), so a retry cannot repeat a committed effect. `slot_time` is the
        scheduled instant, recorded on the wall clock of `timezone`; `event` is the inbound
        event an event trigger matched.
        """
        slot: dict[str, JsonValue] = {"request_id": request_id}
        if self.template_kind in {"operation", "agent-run"}:
            slot["idempotency_context"] = request_id
            if slot_time is not None and self.timezone is not None:
                slot["slot_time"] = slot_time.astimezone(ZoneInfo(self.timezone)).isoformat()
            if event is not None:
                slot["trigger_event"] = event.model_dump(mode="json")
        return TEMPLATES[self.template_kind].model_validate({**self.template, **slot})

    @property
    def namespace(self) -> str:
        return self.request("validation.20000101T000000Z").namespace


class Provisioning(Record):
    principals: Annotated[tuple[Principal, ...], Field(max_length=1000)]
    permissions: Annotated[tuple[Permission, ...], Field(max_length=10000)]
    platform: Annotated[tuple[Permission, ...], Field(max_length=10000)]
    object_permissions: Annotated[tuple[ObjectPermission, ...], Field(max_length=10000)] = ()
    object_platform: Annotated[tuple[ObjectPermission, ...], Field(max_length=10000)] = ()
    host_budgets: Annotated[tuple[HostBudget, ...], Field(max_length=1000)] = ()
    # The complete trigger set; a trigger absent from a later document is removed (ADR-024).
    triggers: Annotated[tuple[TriggerSpec, ...], Field(max_length=1000)] = ()
    # Host approval rules per namespace and operation (ADR-036); replaced as a whole.
    approvals: HostApprovals = HostApprovals()


# Agent groups (ADR-034 decision 1). A Kubernetes-shaped manifest: the kernel reuses the
# apiVersion/kind/metadata/spec/status shape and generation semantics, not an API server.
GroupName = Annotated[str, Field(strict=True, pattern=r"^[a-z][a-z0-9-]{0,39}$")]
PoolName = Annotated[str, Field(strict=True, pattern=r"^[a-z][a-z0-9-]{0,31}$")]
# Largest replicas per pool, pools per group, members per group and triggers per pool.
MAX_REPLICAS, MAX_POOLS, MAX_MEMBERS, MAX_POOL_TRIGGERS = 32, 16, 64, 8


class GroupMetadata(Record):
    name: GroupName
    namespace: Identifier
    # Server-assigned; accepted and ignored so a `GET` body can be applied again.
    generation: Annotated[int, Field(strict=True, ge=0)] | None = None
    created_at: Annotated[str, Field(max_length=64)] | None = None
    updated_at: Annotated[str, Field(max_length=64)] | None = None


class GroupSchedule(Record):
    """A time trigger of every member of a pool (ADR-024 semantics)."""

    cron: Annotated[str, Field(strict=True, min_length=9, max_length=128)]
    timezone: Annotated[str, Field(strict=True, min_length=1, max_length=64)]
    misfire: Literal["skip", "run_once_late", "grace"]
    grace_seconds: Annotated[int, Field(strict=True, ge=60, le=86400)] | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        validate_schedule(self.cron, self.timezone)
        if (self.misfire == "grace") != (self.grace_seconds is not None):
            raise ValueError("grace_seconds is required for, and only for, the grace policy")
        return self


class GroupEventMatch(Record):
    """Kernel-journal events that wake a member (ADR-034 decision 6; ADR-034 slice 7).

    Parsed so a manifest is complete, and refused at apply until slice 7 lands."""

    types: Annotated[tuple[EventType, ...], Field(min_length=1, max_length=16)]
    source: EventText | None = None
    filters: Annotated[tuple[EventFilter, ...], Field(max_length=8)] = ()


class GroupTrigger(Record):
    schedule: GroupSchedule | None = None
    event: GroupEventMatch | None = None

    @model_validator(mode="after")
    def one_kind(self) -> Self:
        if (self.schedule is None) == (self.event is None):
            raise ValueError("A group trigger has exactly one of schedule or event")
        return self


class RunBounds(Record):
    """A run template's bounds; `timeout_seconds` is per attempt, as in ADR-034's
    apply-time lease bound."""

    max_turns: Annotated[int, Field(strict=True, ge=1, le=200)] = 40
    max_tool_calls: Annotated[int, Field(strict=True, ge=1, le=500)] = 100
    max_cost_usd: Annotated[float, Field(strict=True, gt=0, le=50)] = 2.0
    attempts: Annotated[int, Field(strict=True, ge=1, le=3)] = 2
    timeout_seconds: Annotated[int, Field(strict=True, ge=30, le=3600)] = 900
    run_timeout_seconds: Annotated[int, Field(strict=True, ge=60, le=3600)] = 1800


class RunTemplate(Record):
    """A pool's ADR-027 agent-run template; the kernel fills in the member, namespace,
    grants and per-slot identity."""

    objective: Annotated[str, Field(strict=True, min_length=1, max_length=1024)] | None = None
    instructions: Annotated[str, Field(strict=True, min_length=1, max_length=65536)]
    operations: Annotated[tuple[OperationName, ...], Field(min_length=1, max_length=32)]
    context: Annotated[tuple[ContextCall, ...], Field(max_length=4)] = ()
    harness: HarnessID | None = None
    model: ModelID | None = None
    bounds: RunBounds = RunBounds()
    expect: Annotated[tuple[Expectation, ...], Field(max_length=16)] = ()


class GroupSpend(Record):
    """A spend account (ADR-034 decision 5.3; slice 9). Refused at apply until then."""

    usd: Annotated[float, Field(strict=True, gt=0, le=100000)]
    window: Literal["day", "week"]
    timezone: Annotated[str, Field(strict=True, min_length=1, max_length=64)] = "UTC"


class WindowExpectation(Record):
    """A windowed pool expectation (ADR-034 decision 5.4; slice 9). Refused until then."""

    operation: OperationName
    transition: Identifier | None = None
    min: Annotated[int, Field(strict=True, ge=0, le=100000)] | None = None
    max: Annotated[int, Field(strict=True, ge=0, le=100000)] | None = None
    window: Literal["day", "week"]


class GroupPool(Record):
    name: PoolName
    replicas: Annotated[int, Field(strict=True, ge=1, le=MAX_REPLICAS)]
    # Delegated from the supervisor: the grants of every member run.
    grants: Annotated[tuple[Grant, ...], Field(min_length=1, max_length=48)]
    run: RunTemplate
    triggers: Annotated[tuple[GroupTrigger, ...], Field(max_length=MAX_POOL_TRIGGERS)] = ()
    spend: GroupSpend | None = None
    expect_window: Annotated[tuple[WindowExpectation, ...], Field(max_length=16)] = ()
    drain_timeout_seconds: Annotated[int, Field(strict=True, ge=0, le=86400)] = 1800

    @model_validator(mode="after")
    def distinct_grants(self) -> Self:
        if len(set(self.grants)) != len(self.grants):
            raise ValueError("A pool repeats a grant")
        return self


class GroupSpec(Record):
    supervisor: Identifier
    # Collection documents this group uses (ADR-034 decision 2.1).
    collections: Annotated[tuple[Identifier, ...], Field(max_length=32)] = ()
    spend: GroupSpend | None = None
    pools: Annotated[tuple[GroupPool, ...], Field(min_length=1, max_length=MAX_POOLS)]

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if len({p.name for p in self.pools}) != len(self.pools):
            raise ValueError("Pool names must be distinct")
        if len(set(self.collections)) != len(self.collections):
            raise ValueError("Collections must be distinct")
        if sum(p.replicas for p in self.pools) > MAX_MEMBERS:
            raise ValueError(f"A group has at most {MAX_MEMBERS} members")
        return self


class AgentGroup(Wire):
    """`PUT /v0.2/groups/{name}`: a declarative set of agent principals (ADR-034 P1)."""

    apiVersion: Literal["cardea/v1alpha1"]  # The Kubernetes field name.
    kind: Literal["AgentGroup"]
    metadata: GroupMetadata
    spec: GroupSpec
    # Server-owned; accepted and ignored so a `GET` body can be applied again.
    status: dict[str, JsonValue] | None = None
