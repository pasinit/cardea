"""Installed operations declared by host-owner configuration (ADR-023).

A descriptor is a standard MCP ``Tool`` plus Cardea's enforced binding. MCP annotations are
recorded as descriptive hints only; authority, effect class and limits come from the binding.
"""

import json
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from importlib.metadata import entry_points
from pathlib import Path
from typing import Annotated, Any, Literal, Self, cast, get_args

import jsonpointer  # type: ignore[import-untyped]  # ships no type information
import mcp.types
import rfc8785
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import Field, JsonValue, TypeAdapter, ValidationError, model_validator

from cardea.kernel.models import OperationName
from cardea.service.database import Fault, private_file
from cardea.service.models import Record, Right

ENTRY_POINT_GROUP = "cardea.operations"
KERNEL_RIGHTS: frozenset[str] = frozenset(get_args(Right))
# Prefixes owned by kernel rights and fixed installed profiles.
RESERVED_PREFIXES = ("objects.", "tasks.", "events.", "host.", "models.", "tools.", "inputs.")
MAX_OUTPUT_BYTES = 262144
_NAME = TypeAdapter(OperationName)


class OperationRejected(Exception):
    """Raised by a handler that is certain no external effect happened."""


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


@dataclass(frozen=True)
class Reconciliation:
    """A reconciler's observation of destination state for an uncertain effect."""

    applied: bool
    output: dict[str, JsonValue] | None = None


Handler = Callable[[dict[str, JsonValue], OperationContext], Awaitable[dict[str, JsonValue]]]
Reconciler = Callable[[dict[str, JsonValue], OperationContext], Awaitable[Reconciliation]]
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


class Binding(Record):
    version: Annotated[int, Field(strict=True, ge=1)]
    effect: Literal["read", "effect"]
    # In-process entry point; absent for a confined operation, whose code is the worker.
    handler: HandlerName | None = None
    # None runs the handler in the service process (ADR-023).
    executor: ConfinedExecutor | None = None
    timeout_seconds: Annotated[int, Field(strict=True, ge=1, le=600)] = 30
    max_output_bytes: Annotated[int, Field(strict=True, ge=1, le=MAX_OUTPUT_BYTES)] = 65536
    requires: tuple[Right, ...] = ()
    # Argument values that identify one external effect (ADR-026). Within one idempotency
    # context, a second invocation with the same values returns the first receipt's result.
    idempotency_scope: Annotated[tuple[Pointer, ...], Field(max_length=16)] | None = None
    # Entry point that observes destination state to settle an uncertain effect.
    reconciler: HandlerName | None = None

    @model_validator(mode="after")
    def effect_only(self) -> Self:
        if self.effect == "read" and (self.idempotency_scope is not None or self.reconciler):
            raise ValueError("Read operations have no effect scope or reconciler")
        if (self.handler is None) == (self.executor is None):
            raise ValueError("Declare an in-process handler or a confined executor, not both")
        return self


class OperationDescriptor(Record):
    tool: dict[str, JsonValue]
    cardea: Binding

    @model_validator(mode="after")
    def standard_tool(self) -> Self:
        try:
            tool = mcp.types.Tool.model_validate(self.tool)
        except ValidationError as error:
            raise ValueError("Descriptor tool is not a valid MCP Tool") from error
        _NAME.validate_python(tool.name)
        if tool.name in KERNEL_RIGHTS or tool.name.startswith(RESERVED_PREFIXES):
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
        # In-process descriptors keep the digest they had before executors existed.
        exclude = {"cardea": {"executor"}} if self.cardea.executor is None else None
        return sha256(rfc8785.dumps(self.model_dump(mode="json", exclude=exclude))).hexdigest()


class Registry:
    def __init__(
        self,
        descriptors: Iterable[OperationDescriptor] = (),
        handlers: Mapping[str, Handler | Reconciler] | None = None,
    ) -> None:
        self.descriptors: dict[str, OperationDescriptor] = {}
        for descriptor in descriptors:
            if descriptor.name in self.descriptors:
                raise Fault("operation_duplicate", "Operation is installed twice", 500)
            self.descriptors[descriptor.name] = descriptor
        self.handlers: dict[str, Any] = dict(handlers or {})
        if handlers is not None:
            missing = self.entry_points() - set(self.handlers)
            if missing:
                raise Fault("operation_handler_missing", "Operation handler is not installed", 500)

    def entry_points(self) -> set[str]:
        names = set()
        for descriptor in self.descriptors.values():
            if descriptor.cardea.handler:
                names.add(descriptor.cardea.handler)
            if descriptor.cardea.reconciler:
                names.add(descriptor.cardea.reconciler)
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
                raise Fault(
                    "operation_invalid", f"Invalid operation descriptor {path.name}", 500
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
            if grant not in KERNEL_RIGHTS:
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
        if not Draft202012Validator(descriptor.output_schema).is_valid(output):
            return None
        return content
