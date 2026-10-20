"""Explicit v0.2 wire/config records; existing v0.1 task and receipt bytes are preserved."""

from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Annotated, ClassVar, Literal, Self
from zoneinfo import ZoneInfo

import rfc8785
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator

from cardea.kernel.models import Identifier, ObjectVersion, OperationName, Task, Timestamp
from cardea.runtime.incidents import IncidentBatch
from cardea.service.schedule import validate_schedule

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
    "models.disclose",
    "tools.incidents",
    "inputs.append",
    "tasks.delegate",
    "tasks.schedule",
]

# A kernel right, or the name of an installed operation (ADR-023). Installed names are
# checked against the live registry at provisioning, admission and dispatch.
Grant = Right | OperationName

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


class TaskRequest(Wire):
    # Canonical (RFC 8785) size bound of one request.
    max_encoded_bytes: ClassVar[int] = 16384

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
        if len(encoded) > self.max_encoded_bytes or len(set(self.grants)) != len(self.grants):
            raise ValueError("Request is too large or repeats a grant")
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


class AgentTaskRequest(TaskRequest):
    inputs: IncidentBatch
    payload: Annotated[dict[str, JsonValue], Field(max_length=0)] = Field(default_factory=dict)
    grants: Annotated[tuple[Right, ...], Field(min_length=1, max_length=13)]
    parent_task_id: None = None
    wait_for_event: Literal[True] = True
    timeout_seconds: Annotated[int, Field(strict=True, ge=120, le=3600)] = 600
    max_output_tokens: Annotated[int, Field(strict=True, ge=256, le=4096)] = 2048

    @model_validator(mode="after")
    def agent_grants(self) -> Self:
        if len(self.inputs.records) >= 20:
            raise ValueError("Initial batch must leave room for one added record")
        if not {
            "models.invoke",
            "models.disclose",
            "tools.incidents",
            "inputs.append",
            "events.read",
        } <= set(self.grants):
            raise ValueError(
                "Agent runtime requires explicit model, disclosure, tool and input grants"
            )
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
        if self.operation not in self.grants:
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


class AgentRunRequest(TaskRequest):
    """One bounded agent session whose only tools are installed operations (ADR-027)."""

    # Room for the full 64 KiB of instructions declared below plus the other fields.
    max_encoded_bytes: ClassVar[int] = 131072

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

    @model_validator(mode="after")
    def run_grants(self) -> Self:
        if len(set(self.operations)) != len(self.operations):
            raise ValueError("Operations must be distinct")
        if any(e.operation not in self.operations for e in self.expect):
            raise ValueError("Expectations must name exposed operations")
        if not {"models.invoke", *self.operations} <= set(self.grants):
            raise ValueError("Agent runs need model invocation and every exposed operation")
        if not {c.operation for c in self.context} <= set(self.grants):
            raise ValueError("Context operations must be granted")
        return self


class AgentInput(Wire):
    request_id: Identifier
    inputs: IncidentBatch


class ModelRuntimeConfig(Record):
    model: Annotated[str, Field(strict=True, min_length=1, max_length=256)]
    api_key_file: Path | None = None
    api_key_env: Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")] | None = None

    @model_validator(mode="after")
    def credential_source(self) -> Self:
        if (self.api_key_file is None) == (self.api_key_env is None):
            raise ValueError("Select exactly one private key file or environment variable")
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


class ServiceConfig(Record):
    data_dir: Path
    spiffe_socket: str
    trust_domain: str
    audience: str = "cardea-kernel"
    runtime_spiffe_id: str
    oidc: OIDCConfig | None = None
    agent_runtime: ModelRuntimeConfig | None = None
    host_execution: Annotated[bool, Field(strict=True)] = False
    # Private directory of installed operation descriptors (ADR-023).
    operations_dir: Path | None = None
    agent_session: AgentSessionConfig | None = None
    rpc_timeout: Annotated[float, Field(gt=0, le=10)] = 2.0
    # Off unless configured; the private Unix socket is always served (ADR-025).
    network: NetworkListener | None = None
    # Request-body cap for every listener. Responses stay capped at 256 KiB.
    max_request_bytes: Annotated[int, Field(strict=True, ge=4096, le=1048576)] = 32768


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
    delegate: Annotated[bool, Field(strict=True)] = False
    expires_at: Timestamp


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


class ModelBudget(Record):
    principal_id: Identifier
    namespace: Identifier
    calls: Annotated[int, Field(strict=True, ge=0, le=1000000)]
    tokens: Annotated[int, Field(strict=True, ge=0, le=10**12)]


# The slot request ID `<trigger_id>.<YYYYMMDDTHHMMSSZ>` must still be an Identifier (ADR-024).
TriggerID = Annotated[str, Field(strict=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,110}$")]
TemplateKind = Literal[
    "task", "host", "host-object", "operation", "delegation", "agent", "agent-run"
]
TEMPLATES: dict[str, type[TaskRequest]] = {
    "task": TaskRequest,
    "host": HostTaskRequest,
    "host-object": HostObjectTaskRequest,
    "operation": OperationTaskRequest,
    "delegation": DelegationTaskRequest,
    "agent": AgentTaskRequest,
    "agent-run": AgentRunRequest,
}


class TriggerSpec(Record):
    """A host-owner-installed time trigger; it admits one task per scheduled slot."""

    trigger_id: TriggerID
    supervisor_id: Identifier
    # Five-field POSIX/Vixie cron, evaluated on the wall clock of `timezone`.
    schedule: Annotated[str, Field(strict=True, min_length=9, max_length=128)]
    timezone: Annotated[str, Field(strict=True, min_length=1, max_length=64)]
    misfire: Literal["skip", "run_once_late", "grace"]
    grace_seconds: Annotated[int, Field(strict=True, ge=60, le=86400)] | None = None
    enabled: Annotated[bool, Field(strict=True)] = True
    template_kind: TemplateKind
    # A complete request of `template_kind` without `request_id`; validated at install and
    # again at every admission, because installed operations and policy can change.
    template: dict[str, JsonValue]

    @model_validator(mode="after")
    def coherent(self) -> Self:
        validate_schedule(self.schedule, self.timezone)
        if (self.misfire == "grace") != (self.grace_seconds is not None):
            raise ValueError("grace_seconds is required for, and only for, the grace policy")
        # A null slot_time is accepted so a dumped request model can serve as a template.
        if {"request_id", "idempotency_context"} & set(self.template) or self.template.get(
            "slot_time"
        ):
            raise ValueError("The trigger assigns request and idempotency IDs per slot")
        if self.template.get("parent_task_id") is not None:
            raise ValueError("Triggers admit root tasks only")
        self.request("validation.20000101T000000Z")
        return self

    def request(self, request_id: str, slot_time: datetime | None = None) -> TaskRequest:
        """The slot's request; `request_id` is the slot identity `<trigger_id>.<slot>`.

        Operation effects in one slot share that identity as their idempotency
        context (ADR-026), so a retried slot cannot repeat a committed effect.
        `slot_time` is the scheduled instant, recorded on the wall clock of `timezone`.
        """
        slot: dict[str, JsonValue] = {"request_id": request_id}
        if self.template_kind in {"operation", "agent-run"}:
            slot["idempotency_context"] = request_id
            if slot_time is not None:
                slot["slot_time"] = slot_time.astimezone(ZoneInfo(self.timezone)).isoformat()
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
    model_budgets: Annotated[tuple[ModelBudget, ...], Field(max_length=1000)] = ()
    # The complete trigger set; a trigger absent from a later document is removed (ADR-024).
    triggers: Annotated[tuple[TriggerSpec, ...], Field(max_length=1000)] = ()
