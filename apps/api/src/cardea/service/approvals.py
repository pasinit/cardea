"""Approval rules and their fail-closed evaluator (ADR-036 slice 1).

A rule says when an operation call needs approval in addition to authority. Rules come from
the operation descriptor (the default), the host (per namespace and operation, replacing the
default) and the submitting supervisor (per grant, only adding requirements). The strictest
outcome wins.

Conditions are CEL expressions (cel-spec) evaluated by ``common-expression-language``, selected
by the slice-1 fit test recorded in ADR-036. They see only ``args`` (the validated arguments),
``operation`` and ``ns`` (the namespace; ``namespace`` is a reserved CEL identifier). Any
evaluation error, or a result that is not a boolean, requires approval.

Nothing here is wired into the runtime yet: no call is held for approval until slice 2.
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
from typing import Annotated, Literal, Self

import cel
import rfc8785
from pydantic import AfterValidator, Field, JsonValue, model_validator

from cardea.kernel.models import Identifier, OperationName
from cardea.service.models import Record

ApprovalLevel = Literal["autonomous", "conditional", "always"]
OnExpiry = Literal["resume", "fail"]
RuleSource = Literal["operation", "host", "grant"]
RuleID = Annotated[str, Field(strict=True, pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")]
# One printable line, like ADR-026 failure summaries; shown to approvers and agents.
Reason = Annotated[str, Field(strict=True, min_length=1, max_length=200, pattern=r"^[ -~]+$")]

MIN_EXPIRY_SECONDS = 60
MAX_EXPIRY_SECONDS = 30 * 24 * 3600
MAX_RULES = 16
# Static cost bound. CEL has no loops, so without comprehension macros evaluation cost is
# bounded by expression size times argument size (64 KiB, ADR-029). The fit test measured the
# worst cases under this bound at well under a second.
MAX_CONDITION_CHARS = 512
VARIABLES = frozenset({"args", "operation", "ns"})
# Operator overload identifiers and functions the Rust binding reports for a program.
FUNCTIONS = frozenset(
    {
        "!_",
        "-_",
        "_!=_",
        "_%_",
        "_&&_",
        "_*_",
        "_+_",
        "_-_",
        "_/_",
        "_<=_",
        "_<_",
        "_==_",
        "_>=_",
        "_>_",
        "_?_:_",
        "_[_]",
        "_||_",
        "@in",
        "contains",
        "double",
        "endsWith",
        "int",
        "matches",
        "size",
        "startsWith",
        "string",
        "uint",
    }
)
# Comprehension macros iterate over arguments, and nested or accumulating ones (map, filter)
# cost more than linear time with no built-in budget. They are refused by name anywhere in the
# source, string literals included: over-refusal is safe, under-refusal is not.
_MACROS = re.compile(
    r"\b(all|exists|exists_one|map|filter|bind|transformList|transformMap|optMap|optFlatMap)\b"
)


class ConditionInvalid(ValueError):
    """A condition that cannot be installed: syntax, size, or an unsupported construct."""


@lru_cache(maxsize=1024)
def compile_condition(source: str) -> cel.Program:
    """Compile and statically check a condition; raise ConditionInvalid when it is refused."""
    if not source.strip():
        raise ConditionInvalid("Condition is empty")
    if len(source) > MAX_CONDITION_CHARS:
        raise ConditionInvalid(f"Condition exceeds {MAX_CONDITION_CHARS} characters")
    macro = _MACROS.search(source)
    if macro:
        raise ConditionInvalid(f"Condition uses unsupported comprehension macro {macro[1]!r}")
    try:
        program = cel.compile(source)
    except Exception as error:
        raise ConditionInvalid(f"Condition is not valid CEL: {_first_line(error)}") from error
    references = program.references()
    unknown = set(references["variables"]) - VARIABLES
    if unknown:
        raise ConditionInvalid(
            f"Condition may only use args, operation and ns, not {', '.join(sorted(unknown))}"
        )
    functions = set(references["functions"]) - FUNCTIONS
    if functions:
        raise ConditionInvalid(f"Condition uses unsupported {', '.join(sorted(functions))}")
    return program


@dataclass(frozen=True)
class ConditionResult:
    """True when approval is required; ``error`` is set when that is because evaluation failed."""

    requires_approval: bool
    error: str | None = None


def evaluate_condition(
    source: str, args: Mapping[str, JsonValue], operation: str, namespace: str
) -> ConditionResult:
    """Evaluate one condition against frozen arguments, failing closed."""
    try:
        program = compile_condition(source)
        value = program.execute({"args": dict(args), "operation": operation, "ns": namespace})
    # The binding raises KeyError, TypeError, ValueError, RuntimeError, OverflowError and, on
    # internal panics, pyo3's PanicException, which derives from BaseException.
    except BaseException as error:  # noqa: B036
        if isinstance(error, KeyboardInterrupt | SystemExit):
            raise
        return ConditionResult(True, f"{type(error).__name__}: {_first_line(error)}")
    if type(value) is not bool:
        return ConditionResult(True, f"Condition returned {type(value).__name__}, not a boolean")
    return ConditionResult(value)


def _first_line(error: BaseException) -> str:
    lines = str(error).strip().splitlines()
    return (lines[0] if lines else type(error).__name__)[:200]


class ApprovalRule(Record):
    rule_id: RuleID
    level: ApprovalLevel
    # CEL condition; required for conditional rules and refused otherwise.
    when: str | None = None
    reason: Reason
    expires_after_seconds: (
        Annotated[int, Field(strict=True, ge=MIN_EXPIRY_SECONDS, le=MAX_EXPIRY_SECONDS)] | None
    ) = None
    # None: resume for agent runs, fail for single-operation tasks (ADR-036 decision 2).
    on_expiry: OnExpiry | None = None
    # Four-eyes separation: the task's own supervisor cannot approve.
    distinct_from_supervisor: bool = False

    @model_validator(mode="after")
    def condition_matches_level(self) -> Self:
        if self.level == "conditional":
            if self.when is None:
                raise ValueError("A conditional rule requires a 'when' condition")
            compile_condition(self.when)
        elif self.when is not None:
            raise ValueError(f"An {self.level} rule takes no 'when' condition")
        return self


def _unique(rules: Iterable[ApprovalRule]) -> None:
    seen: set[str] = set()
    for rule in rules:
        if rule.rule_id in seen:
            raise ValueError(f"Duplicate approval rule_id {rule.rule_id!r}")
        seen.add(rule.rule_id)


RuleList = Annotated[tuple[ApprovalRule, ...], Field(max_length=MAX_RULES)]


class OperationApproval(Record):
    """Descriptor default (`cardea.approval`), set by whoever installs the operation."""

    rules: RuleList = ()

    @model_validator(mode="after")
    def unique(self) -> Self:
        _unique(self.rules)
        return self


class HostApprovalRule(Record):
    """Host rules for one namespace and operation; they replace the descriptor default.

    An empty ``rules`` makes the operation autonomous in that namespace.
    """

    namespace: Identifier
    operation: OperationName
    rules: RuleList = ()

    @model_validator(mode="after")
    def unique(self) -> Self:
        _unique(self.rules)
        return self


class HostApprovals(Record):
    """Host approval configuration, provisioned like policy and versioned by digest."""

    entries: Annotated[tuple[HostApprovalRule, ...], Field(max_length=4096)] = ()

    @model_validator(mode="after")
    def unique(self) -> Self:
        keys = [(entry.namespace, entry.operation) for entry in self.entries]
        if len(keys) != len(set(keys)):
            raise ValueError("Duplicate host approval entry for a namespace and operation")
        return self

    def get(self, namespace: str, operation: str) -> HostApprovalRule | None:
        for entry in self.entries:
            if entry.namespace == namespace and entry.operation == operation:
                return entry
        return None

    @property
    def digest(self) -> str:
        return sha256(rfc8785.dumps(self.model_dump(mode="json"))).hexdigest()


class GrantApprovalRule(ApprovalRule):
    """A supervisor's per-grant rule (`approvals` next to `grants`); it can only add."""

    operation: OperationName


def _unique_per_operation(rules: tuple[GrantApprovalRule, ...]) -> tuple[GrantApprovalRule, ...]:
    keys = [(rule.operation, rule.rule_id) for rule in rules]
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate per-grant approval rule_id for an operation")
    return rules


# The `approvals` field of an agent-run request or trigger template (wired in slice 5).
GrantApprovals = Annotated[
    tuple[GrantApprovalRule, ...],
    Field(max_length=MAX_RULES),
    AfterValidator(_unique_per_operation),
]


@dataclass(frozen=True)
class RequiringRule:
    source: RuleSource
    rule_id: str
    reason: str
    # Set when the rule requires approval because its condition failed to evaluate.
    error: str | None = None


@dataclass(frozen=True)
class ApprovalRequirement:
    """The combined outcome of every applicable rule for one call."""

    required: bool
    rules: tuple[RequiringRule, ...]
    # Digest of every applicable rule with its source, recorded with a request.
    rules_digest: str
    # Earliest expiry over requiring rules; None when none sets one.
    expires_after_seconds: int | None
    # fail over resume; None when no requiring rule sets it.
    on_expiry: OnExpiry | None
    distinct_from_supervisor: bool

    def resolved_on_expiry(self, *, agent_run: bool) -> OnExpiry:
        if self.on_expiry is not None:
            return self.on_expiry
        return "resume" if agent_run else "fail"


def applicable_rules(
    *,
    operation: str,
    namespace: str,
    descriptor: OperationApproval | None,
    host: HostApprovals | None,
    grant: Sequence[GrantApprovalRule] = (),
) -> tuple[tuple[RuleSource, ApprovalRule], ...]:
    """The host entry replaces the descriptor default; per-grant rules are always added."""
    entry = host.get(namespace, operation) if host is not None else None
    base: tuple[tuple[RuleSource, ApprovalRule], ...]
    if entry is not None:
        base = tuple(("host", rule) for rule in entry.rules)
    else:
        base = tuple(("operation", rule) for rule in (descriptor.rules if descriptor else ()))
    added: tuple[tuple[RuleSource, ApprovalRule], ...] = tuple(
        ("grant", rule) for rule in grant if rule.operation == operation
    )
    return base + added


def requirement(
    *,
    operation: str,
    namespace: str,
    args: Mapping[str, JsonValue],
    descriptor: OperationApproval | None,
    host: HostApprovals | None,
    grant: Sequence[GrantApprovalRule] = (),
) -> ApprovalRequirement:
    """Whether this call needs approval, with every requiring rule; strictest wins."""
    rules = applicable_rules(
        operation=operation, namespace=namespace, descriptor=descriptor, host=host, grant=grant
    )
    requiring: list[tuple[RuleSource, ApprovalRule, str | None]] = []
    for source, rule in rules:
        if rule.level == "always":
            requiring.append((source, rule, None))
        elif rule.level == "conditional":
            assert rule.when is not None
            result = evaluate_condition(rule.when, args, operation, namespace)
            if result.requires_approval:
                requiring.append((source, rule, result.error))
    expiries = [r.expires_after_seconds for _, r, _ in requiring if r.expires_after_seconds]
    on_expiry: OnExpiry | None = None
    if any(r.on_expiry == "fail" for _, r, _ in requiring):
        on_expiry = "fail"
    elif any(r.on_expiry == "resume" for _, r, _ in requiring):
        on_expiry = "resume"
    digest_input = [{"source": source, **rule.model_dump(mode="json")} for source, rule in rules]
    return ApprovalRequirement(
        required=bool(requiring),
        rules=tuple(
            RequiringRule(source, rule.rule_id, rule.reason, error)
            for source, rule, error in requiring
        ),
        rules_digest=sha256(rfc8785.dumps(digest_input)).hexdigest(),
        expires_after_seconds=min(expiries) if expiries else None,
        on_expiry=on_expiry,
        distinct_from_supervisor=any(r.distinct_from_supervisor for _, r, _ in requiring),
    )
