"""Installed operations declared by host-owner configuration (ADR-023).

A descriptor is a standard MCP ``Tool`` plus Cardea's enforced binding. MCP annotations are
recorded as descriptive hints only; authority, effect class and limits come from the binding.
"""

import json
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from hashlib import sha256
from importlib.metadata import entry_points
from pathlib import Path
from typing import Annotated, Any, Literal, Self, cast, get_args

import jsonpointer  # type: ignore[import-untyped]  # ships no type information
import mcp.types
import rfc8785
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import (
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
    ValidationInfo,
    model_validator,
)

from cardea.kernel.models import MAX_DOCUMENT_BYTES, OperationName, canonical_json
from cardea.service.approvals import OperationApproval
from cardea.service.database import Fault, Snapshot, private_file
from cardea.service.models import (
    APPROVE_GRANT_PREFIX,
    BUILTIN_OPERATIONS,
    RECORD_GRANT_PREFIX,
    Record,
    Right,
)

ENTRY_POINT_GROUP = "cardea.operations"
KERNEL_RIGHTS: frozenset[str] = frozenset(get_args(Right))
# Prefixes owned by kernel rights and fixed installed profiles.
RESERVED_PREFIXES = (
    "objects.",
    "tasks.",
    "events.",
    "host.",
    "models.",
    "tools.",
    "inputs.",
    "groups.",
    # Built-in record operations (ADR-034); installed only by the kernel itself.
    "records.",
    # Built-in mailbox operations (ADR-034); they run inside their settle transaction.
    "mailbox.",
    # Built-in context sources (ADR-034 decision 5).
    "runs.",
    # Built-in approval operations (ADR-036 slice 4).
    "approvals.",
)
BUILTIN_PREFIXES = ("mailbox.", "runs.")
# Room in a receipt's 64 KiB result document for the output and its wrapper keys.
MAX_RECORDED_OUTPUT_BYTES = MAX_DOCUMENT_BYTES - 64
_NAME = TypeAdapter(OperationName)
MAX_IN_PROCESS_TIMEOUT_SECONDS = 600
# The hard maximum for a confined operation (ADR-023 amendment, lead decision 2026-11-16).
MAX_CONFINED_TIMEOUT_SECONDS = 7200
# Artifacts travel in the worker's output document, which the helper caps (launcher).
MAX_ARTIFACT_BYTES = 11 * 1024 * 1024
DEFAULT_ARTIFACT_BYTES = 10 * 1024 * 1024


class OperationRejected(Exception):
    """Raised by a handler that is certain no external effect happened.

    ``code`` becomes the receipt's failure code; built-in operations use specific codes
    such as ``version_conflict`` (ADR-034), installed ones normally keep the default.
    """

    def __init__(self, message: str = "", code: str = "operation_rejected") -> None:
        super().__init__(message)
        self.code = code


class ContentRefused(OperationRejected):
    """The content gate refused a call (ADR-037 decision 4).

    `refused` holds the refused inputs and the lines that matched. They stay in memory
    only: the kernel uses them to redact the call's receipt and request, and never stores,
    logs or shows them.
    """

    def __init__(self, message: str, refused: tuple[str, ...]) -> None:
        super().__init__(message, "content_refused")
        self.refused = refused


def http_status(error: BaseException) -> int | None:
    """The HTTP status an exception carries (httpx, requests, urllib style), if any."""
    for candidate in (getattr(error, "response", None), error):
        for name in ("status_code", "status", "code"):
            value = getattr(candidate, name, None)
            if type(value) is int and 100 <= value <= 599:
                return value
    return None


@dataclass(frozen=True)
class OperationContext:
    task_id: str
    namespace: str
    receipt_id: str
    deadline: datetime
    # Stable identity of this external effect: the idempotency scope key when the
    # operation declares one, else the receipt. Use it as the provider idempotency key.
    effect_key: str
    # Dispatch lease of the invoking attempt; a confined worker stops when it is lost.
    lease_id: str = ""
    # Agent principal of the agent run that made the call; None outside agent runs.
    agent_id: str | None = None
    # The request's idempotency context, e.g. `<trigger_id>.<slot>` for a scheduled task.
    idempotency_context: str | None = None
    # ISO 8601 scheduled slot time with its trigger's UTC offset; None outside triggers.
    slot_time: str | None = None
    # The run's context read snapshot, for built-in context sources only (ADR-034 5.1).
    snapshot: Snapshot | None = None
    # The calling agent's group, from the kernel's member table (ADR-034); None outside one.
    group: str | None = None
    # The calling agent's pool in that group; None outside one (ADR-039 role limits).
    pool: str | None = None
    # Declared object inputs, staged by the kernel under the caller's ceiling: argument
    # pointer -> the object's verified bytes (ADR-037). Never recorded in the receipt.
    inputs: Mapping[str, bytes] = field(default_factory=dict)
    # Receipts the descriptor's `evidence` queries found, by query name (ADR-037
    # amendment 2026-11-17): the kernel's own record of earlier calls, never the caller's
    # claim. Each entry is `{receipt_id, agent_id, group, at, fields}`, newest first.
    evidence: Mapping[str, tuple[Mapping[str, JsonValue], ...]] = field(default_factory=dict)
    # The kernel's content gate (ADR-037 decision 4), bound for in-process installed
    # operations: call `check_content(operation=..., diff=..., texts=...)` before
    # sending anything off the host; it raises `OperationRejected` on a finding.
    check_content: Callable[..., None] | None = None
    # Bound for in-process installed operations (ADR-039): how many calls of this operation
    # the caller's role made within the window, this call included. The role is the
    # caller's group pool, or the agent itself outside a group. Running, succeeded and
    # uncertain calls count; failed ones do not. A handler uses it for a rate limit.
    count_calls: Callable[[timedelta], int] | None = None


@dataclass(frozen=True)
class WithArtifacts:
    """A confined handler's output plus named artifacts (ADR-037).

    Only a worker (`operation_worker`) can return it; the kernel stores the artifacts as
    task-owned objects when the operation declares `max_artifact_bytes`.
    """

    output: dict[str, JsonValue]
    artifacts: dict[str, bytes]


@dataclass(frozen=True)
class Reconciliation:
    """A reconciler's observation of destination state for an uncertain effect."""

    applied: bool
    output: dict[str, JsonValue] | None = None


Handler = Callable[[dict[str, JsonValue], OperationContext], Awaitable[dict[str, JsonValue]]]
Reconciler = Callable[[dict[str, JsonValue], OperationContext], Awaitable[Reconciliation]]
# A reply poller (ADR-039): one call's thread in, `replies.Polled` out.
Poller = Callable[[dict[str, JsonValue]], Awaitable[Any]]
HandlerName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")]
# RFC 6901 pointer into the operation arguments.
Pointer = Annotated[str, Field(pattern=r"^(/([^~/]|~[01])*)+$", max_length=256)]


WorkerName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")]


class ConfinedExecutor(Record):
    """Run the handler as a separate confined process (ADR-028).

    Both names refer to host-owner configuration read by the root helper; the descriptor
    selects among installed profiles and workers but cannot define either.
    """

    kind: Literal["confined"]
    profile: WorkerName
    worker: WorkerName


class ResourceGrant(Record):
    """The kind of a resource-qualified grant and the argument that names the resource."""

    grant: Literal["repo"]
    argument: Pointer


# A value recorded in a receipt: `arguments/<key>/...` (the call's arguments) or
# `output/<key>/...` (its result's output). Keys are plain names, so a path maps to SQLite
# JSON without quoting issues.
EvidencePath = Annotated[
    str, Field(strict=True, pattern=r"^(arguments|output)(/[A-Za-z0-9_]{1,64}){1,4}$")
]
MAX_EVIDENCE = 20


class EvidenceQuery(Record):
    """Earlier receipts a call needs as evidence (ADR-037 amendment 2026-11-17).

    The kernel finds the successful calls of `operation` in the caller's namespace whose
    recorded `match` paths equal this call's arguments at the given pointers, and whose
    `equals` paths hold the given values. The handler receives, for the newest
    `MAX_EVIDENCE`, the receipt ID, the recorded caller (`agent_id`, `group`), the time and
    the values at `fields`. Only the kernel writes receipts, so a caller cannot forge one.
    """

    name: Annotated[str, Field(strict=True, pattern=r"^[a-z][a-z0-9_]{0,31}$")]
    operation: OperationName
    match: Annotated[dict[EvidencePath, Pointer], Field(min_length=1, max_length=8)]
    equals: Annotated[dict[EvidencePath, str | int | bool], Field(max_length=8)] = Field(
        default_factory=dict
    )
    fields: Annotated[tuple[EvidencePath, ...], Field(max_length=8)] = ()
    # Which end of the history: the newest receipts, or the oldest (the first such call).
    order: Literal["newest", "oldest"] = "newest"


class ReplyPolling(Record):
    """Replies to an effect, polled from its destination (ADR-039).

    The kernel calls `poller` (an entry point) for each succeeded call of the operation in
    the last `window_days`, every `interval_seconds`. The poller returns the replies it
    accepts and the ones it drops; the kernel delivers accepted replies into the calling
    agent's mailbox as untrusted data and counts dropped ones without their content.

    `inbound` (amendment 2026-11-21) names a second entry point, called once per interval
    with the operation's recent threads. It reads new messages at the destination that are
    on none of them, as host configuration allows, and returns a `replies.Inbox`: the ones
    it accepts, delivered to a configured pool, and the ones it rejects (`replies.py`).
    """

    poller: HandlerName
    interval_seconds: Annotated[int, Field(strict=True, ge=60, le=86400)] = 300
    window_days: Annotated[int, Field(strict=True, ge=1, le=90)] = 14
    inbound: HandlerName | None = None


class RunWindow(Record):
    """A read operation's run window (ADR-038 decisions 4 and 5).

    A call whose run key (the namespace, the operation, the `key` arguments and the
    environment digest) settled successfully within `seconds` returns that result, marked
    `cached`, and nothing runs. An identical call while one runs is refused
    (`env_run_in_progress`). `rerun` names a boolean argument that skips the window; forced
    reruns are limited to `reruns_per_day` per `rerun_subject` (for example a repository and
    commit). `environment` names the argument whose repository-registry `environment`
    catalog joins the digest, with the confined worker's profile digest.
    """

    key: Annotated[tuple[Pointer, ...], Field(min_length=1, max_length=8)]
    seconds: Annotated[int, Field(strict=True, ge=60, le=7 * 24 * 3600)] = 24 * 3600
    rerun: Pointer | None = None
    rerun_subject: Annotated[tuple[Pointer, ...], Field(min_length=1, max_length=8)] | None = None
    reruns_per_day: Annotated[int, Field(strict=True, ge=0, le=100)] = 3
    environment: Pointer | None = None

    @model_validator(mode="after")
    def subject(self) -> Self:
        if self.rerun is not None and self.rerun_subject is None:
            raise ValueError("A run window with rerun needs rerun_subject")
        return self


class Binding(Record):
    version: Annotated[int, Field(strict=True, ge=1)]
    effect: Literal["read", "effect"]
    # In-process entry point; absent for a confined operation, whose code is the worker.
    handler: HandlerName | None = None
    # None runs the handler in the service process (ADR-023).
    executor: ConfinedExecutor | None = None
    # In-process operations: at most 600 s. Confined operations: at most their profile's
    # `wall_seconds`, checked against the installed profile at load and admission, and never
    # more than two hours (ADR-023 amendment, 2026-11-16).
    timeout_seconds: Annotated[int, Field(strict=True, ge=1, le=MAX_CONFINED_TIMEOUT_SECONDS)] = 30
    # At most what a receipt can record (MAX_DOCUMENT_BYTES); checked below with a clear error.
    max_output_bytes: Annotated[int, Field(strict=True, ge=1)] = 65536
    requires: tuple[Right, ...] = ()
    # Argument values that identify one external effect (ADR-026). Within one idempotency
    # context, a second invocation with the same values returns the first receipt's result.
    idempotency_scope: Annotated[tuple[Pointer, ...], Field(max_length=16)] | None = None
    # Entry point that observes destination state to settle an uncertain effect.
    reconciler: HandlerName | None = None
    # Default approval rules (ADR-036); a host rule for a namespace replaces them. None, like
    # an empty rule list, is autonomous: authority alone decides.
    approval: OperationApproval | None = None
    # A resource-qualified grant every call needs in the caller's live ceiling, named by an
    # argument (ADR-037): `{"grant": "repo", "argument": "/repository"}` needs `repo:<value>`.
    resource: ResourceGrant | None = None
    # Arguments that name kernel objects (`{namespace, resource_id, version, content_digest}`)
    # the handler receives as verified bytes in `OperationContext.inputs` (ADR-037).
    inputs: Annotated[tuple[Pointer, ...], Field(min_length=1, max_length=4)] | None = None
    # Earlier receipts the handler receives in `OperationContext.evidence` (ADR-037).
    evidence: Annotated[tuple[EvidenceQuery, ...], Field(min_length=1, max_length=4)] | None = None
    # Confined operations only: bytes of named artifacts the worker may return beside its
    # output, stored as task-owned kernel objects in parts of at most 1 MiB (ADR-037). 0
    # means none. The output then carries the objects' references, not the bytes.
    max_artifact_bytes: Annotated[int, Field(strict=True, ge=0, le=MAX_ARTIFACT_BYTES)] = 0
    # Replies to this effect that come back from its destination (ADR-039).
    replies: ReplyPolling | None = None
    # Read operations only: reuse a settled run of the same inputs (ADR-038).
    run_window: RunWindow | None = None
    # Names the argument holding a record ID: the kernel links the result to that record,
    # needing `record:<collection>/attach` live (`evidence_links`).
    attach_record: Pointer | None = None

    @model_validator(mode="after")
    def effect_only(self) -> Self:
        if self.effect == "read" and (
            self.idempotency_scope is not None or self.reconciler or self.replies
        ):
            raise ValueError("Read operations have no effect scope, reconciler or replies")
        if self.replies is not None and self.executor is not None:
            raise ValueError("Only in-process operations declare a reply poller")
        if (self.handler is None) == (self.executor is None):
            raise ValueError("Declare an in-process handler or a confined executor, not both")
        if self.executor is None and self.timeout_seconds > MAX_IN_PROCESS_TIMEOUT_SECONDS:
            raise ValueError(
                f"timeout_seconds {self.timeout_seconds} exceeds "
                f"{MAX_IN_PROCESS_TIMEOUT_SECONDS}; only confined operations may run longer"
            )
        if self.run_window is not None and self.effect != "read":
            raise ValueError("Only read operations have a run window")
        if self.executor is None and self.max_artifact_bytes:
            raise ValueError("Only confined operations return artifacts")
        if self.max_output_bytes > MAX_DOCUMENT_BYTES:
            # A larger output could never be recorded in its receipt (ADR-023, ADR-029).
            raise ValueError(
                f"max_output_bytes {self.max_output_bytes} exceeds {MAX_DOCUMENT_BYTES}, the "
                "largest output a receipt can record (64 KiB kernel document)"
            )
        return self


class OperationDescriptor(Record):
    tool: dict[str, JsonValue]
    cardea: Binding

    @model_validator(mode="after")
    def standard_tool(self, info: ValidationInfo) -> Self:
        try:
            tool = mcp.types.Tool.model_validate(self.tool)
        except ValidationError as error:
            raise ValueError("Descriptor tool is not a valid MCP Tool") from error
        _NAME.validate_python(tool.name)
        # Only the kernel's own built-in descriptors may use a reserved name.
        builtin = bool(info.context and info.context.get("builtin"))
        if not builtin and (tool.name in KERNEL_RIGHTS or tool.name.startswith(RESERVED_PREFIXES)):
            raise ValueError("Operation name is reserved for kernel rights")
        if tool.output_schema is None:
            raise ValueError("Installed operations require an output schema for verification")
        for schema in (tool.input_schema, tool.output_schema):
            try:
                Draft202012Validator.check_schema(schema)
            except SchemaError as error:
                raise ValueError("Operation schema is not valid JSON Schema 2020-12") from error
            if schema.get("type") != "object":
                raise ValueError("Operation input and output schemas must describe objects")
        return self

    @property
    def name(self) -> str:
        return str(self.tool["name"])

    @property
    def input_schema(self) -> dict[str, Any]:
        return mcp.types.Tool.model_validate(self.tool).input_schema

    @property
    def output_schema(self) -> dict[str, Any]:
        schema = mcp.types.Tool.model_validate(self.tool).output_schema
        assert schema is not None
        return schema

    @property
    def digest(self) -> str:
        # Descriptors keep the digest they had before executors and approval rules existed.
        unset = {
            name
            for name, value in (
                ("executor", self.cardea.executor),
                ("approval", self.cardea.approval),
                ("resource", self.cardea.resource),
                ("inputs", self.cardea.inputs),
                ("evidence", self.cardea.evidence),
                ("replies", self.cardea.replies),
                ("run_window", self.cardea.run_window),
                ("attach_record", self.cardea.attach_record),
            )
            if value is None
        }
        if not self.cardea.max_artifact_bytes:
            unset.add("max_artifact_bytes")
        excluded: dict[str, Any] = dict.fromkeys(unset, True)
        if self.cardea.replies is not None and self.cardea.replies.inbound is None:
            excluded["replies"] = {"inbound"}  # added 2026-11-21
        exclude = {"cardea": excluded} if excluded else None
        return sha256(rfc8785.dumps(self.model_dump(mode="json", exclude=exclude))).hexdigest()


class Registry:
    def __init__(
        self,
        descriptors: Iterable[OperationDescriptor] = (),
        handlers: Mapping[str, Handler | Reconciler | Poller] | None = None,
    ) -> None:
        from cardea.service import mailbox
        from cardea.service.approval_requests import DECIDE_DESCRIPTOR
        from cardea.service.records import DESCRIPTORS

        # Built-in record (ADR-034) and approval (ADR-036) operations are always installed;
        # the kernel binds their handlers, so they are not entry points.
        self.descriptors: dict[str, OperationDescriptor] = {
            d.name: d for d in (*DESCRIPTORS, DECIDE_DESCRIPTOR)
        }
        for descriptor in descriptors:
            if descriptor.name in self.descriptors:
                raise Fault("operation_duplicate", "Operation is installed twice", 500)
            self.descriptors[descriptor.name] = descriptor
        from cardea.service import context_sources

        for builtin in (*mailbox.descriptors(), *context_sources.DESCRIPTORS):
            # Always present; a copy of another registry's descriptors may carry them.
            self.descriptors.setdefault(builtin.name, builtin)
        self.handlers: dict[str, Any] = dict(handlers or {})
        if handlers is not None:
            missing = self.entry_points() - set(self.handlers)
            if missing:
                raise Fault("operation_handler_missing", "Operation handler is not installed", 500)

    def entry_points(self) -> set[str]:
        names = set()
        for descriptor in self.descriptors.values():
            if descriptor.name in BUILTIN_OPERATIONS or descriptor.name.startswith(
                BUILTIN_PREFIXES
            ):
                continue
            if descriptor.cardea.handler:
                names.add(descriptor.cardea.handler)
            # A confined operation's reconciler runs in its worker (ADR-037), not here.
            if descriptor.cardea.reconciler and descriptor.cardea.executor is None:
                names.add(descriptor.cardea.reconciler)
            if descriptor.cardea.replies is not None:
                names.add(descriptor.cardea.replies.poller)
                if descriptor.cardea.replies.inbound is not None:
                    names.add(descriptor.cardea.replies.inbound)
        return names

    @classmethod
    def load(cls, directory: Path | None, *, resolve: bool = True) -> "Registry":
        """Read private descriptor files; resolve handlers only from installed entry points."""
        if directory is None:
            return cls((), {} if resolve else None)
        descriptors = []
        for path in sorted(directory.glob("*.json")):
            try:
                descriptors.append(OperationDescriptor.model_validate_json(private_file(path)))
            except ValidationError as error:
                reason = str(error.errors()[0].get("msg", ""))[:300]
                raise Fault(
                    "operation_invalid", f"Invalid operation descriptor {path.name}: {reason}", 500
                ) from error
        if not resolve:
            return cls(descriptors)
        wanted = cls(descriptors).entry_points()
        handlers = {
            point.name: point.load()
            for point in entry_points(group=ENTRY_POINT_GROUP)
            if point.name in wanted
        }
        return cls(descriptors, handlers)

    def __contains__(self, name: object) -> bool:
        return name in self.descriptors

    def get(self, name: str) -> OperationDescriptor:
        try:
            return self.descriptors[name]
        except KeyError:
            raise Fault("operation_not_installed", "Operation is not installed", 409) from None

    def handler(self, name: str) -> Handler:
        if name.startswith(BUILTIN_PREFIXES) and name in self.descriptors:
            return _builtin_handler
        try:
            return cast(Handler, self.handlers[self.get(name).cardea.handler or ""])
        except KeyError:
            raise Fault(
                "operation_handler_missing", "Operation handler is not loaded", 503
            ) from None

    def reconciler(self, name: str) -> Reconciler | None:
        entry = self.get(name).cardea.reconciler
        if entry is None:
            return None
        try:
            return cast(Reconciler, self.handlers[entry])
        except KeyError:
            raise Fault("operation_handler_missing", "Reconciler is not loaded", 503) from None

    def poller(self, name: str) -> Poller:
        """The reply poller of an operation that declares one (ADR-039)."""
        replies = self.get(name).cardea.replies
        if replies is None:
            raise Fault("operation_invalid", "Operation declares no reply poller", 500)
        try:
            return cast(Poller, self.handlers[replies.poller])
        except KeyError:
            raise Fault("operation_handler_missing", "Reply poller is not loaded", 503) from None

    def inbound_poller(self, name: str) -> Poller | None:
        """The inbound poller of an operation that declares one (amendment 2026-11-21)."""
        replies = self.get(name).cardea.replies
        if replies is None or replies.inbound is None:
            return None
        try:
            return cast(Poller, self.handlers[replies.inbound])
        except KeyError:
            raise Fault("operation_handler_missing", "Inbound poller is not loaded", 503) from None

    def scope_key(
        self, name: str, namespace: str, context: str, arguments: Mapping[str, JsonValue]
    ) -> str | None:
        """Digest identifying one external effect, or None when no scope is declared."""
        scope = self.get(name).cardea.idempotency_scope
        if scope is None:
            return None
        values = []
        for pointer in scope:
            try:
                values.append(jsonpointer.resolve_pointer(dict(arguments), pointer))
            except jsonpointer.JsonPointerException:
                raise Fault(
                    "invalid_arguments", "Arguments lack a declared idempotency field", 422
                ) from None
        return sha256(
            rfc8785.dumps(
                {"namespace": namespace, "operation": name, "context": context, "values": values}
            )
        ).hexdigest()

    def check_grants(self, grants: Iterable[str]) -> None:
        for grant in grants:
            # Harness grants (ADR-033) are checked against the installed harnesses. Record
            # grants (ADR-034) name collections, which are documents checked at each call;
            # mail grants name addresses, resolved when mail is sent. An approve grant
            # (ADR-036) names an installed operation.
            if grant.startswith(APPROVE_GRANT_PREFIX):
                resource = grant.removeprefix(APPROVE_GRANT_PREFIX)
                if not resource.startswith(RECORD_GRANT_PREFIX):
                    self.get(resource)
            elif grant not in KERNEL_RIGHTS and not grant.startswith(
                ("harness:", "mail:", "repo:", RECORD_GRANT_PREFIX)
            ):
                self.get(grant)

    def validate_arguments(self, name: str, arguments: Mapping[str, JsonValue]) -> None:
        if not Draft202012Validator(self.get(name).input_schema).is_valid(dict(arguments)):
            raise Fault("invalid_arguments", "Arguments do not match the operation schema", 422)

    def check_output(self, name: str, output: object) -> bytes | None:
        """Canonical output bytes when the result satisfies schema and size, else None."""
        descriptor = self.get(name)
        try:
            content = rfc8785.dumps(json.loads(json.dumps(output)))
        except (TypeError, ValueError):
            return None
        if len(content) > descriptor.cardea.max_output_bytes:
            return None
        # The receipt records {"output": ..., "deduplicated": ...} as a kernel document, so
        # the effective cap is that document's 64 KiB, whatever the descriptor declares.
        if len(canonical_json(json.loads(content))) > MAX_RECORDED_OUTPUT_BYTES:
            return None
        if not Draft202012Validator(descriptor.output_schema).is_valid(output):
            return None
        return content


def check_context(registry: Registry, name: str) -> None:
    """A context operation (ADR-031) must be installed and declare no external effect."""
    if registry.get(name).cardea.effect != "read":
        raise Fault("context_not_read_only", "Context operations must be read-only", 422)


async def _builtin_handler(
    arguments: dict[str, JsonValue], context: OperationContext
) -> dict[str, JsonValue]:
    """Built-in operations run inside their settle transaction, never as plain handlers."""
    raise OperationRejected("This built-in operation runs only inside an agent run")
