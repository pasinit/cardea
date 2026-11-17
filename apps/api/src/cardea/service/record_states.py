"""Collection state machines (ADR-034 decision 3, slice 3): the flat table and its checks.

A collection may declare `states`, an `initial` state, `terminal` states and `transitions`.
The table is configuration; the kernel only enforces it in `records.transition`. Each
transition names its permission (`record:<collection>/<name>`), an optional CEL guard
(the ADR-036 evaluator and limits), a claim effect, optional receipt evidence and an
optional ADR-036 approval gate, which compiles into an approval rule on
`records.transition` decided under the resource-qualified `approve` right.
"""

from collections.abc import Iterable, Mapping
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, ConfigDict, Field, JsonValue, model_validator

from cardea.kernel.models import OperationName
from cardea.service.approvals import (
    MAX_EXPIRY_SECONDS,
    MIN_EXPIRY_SECONDS,
    ApprovalRule,
    GrantApprovalRule,
    OnExpiry,
    Reason,
    compile_condition,
)
from cardea.service.models import RECORD_ACTION, Record, record_grant

GUARD_VARIABLES = frozenset({"record", "args", "caller"})
# A receipt-result guard reads the cited call's output, and nothing else (ADR-038 Q3).
RESULT_VARIABLES = frozenset({"result"})
MAX_STATES = 64
MAX_TRANSITIONS = 64
# Actions of the built-in record operations; a transition cannot take one of their names.
RESERVED_ACTIONS = frozenset({"create", "read", "update", "claim", "release"})
TRANSITION_OPERATION = "records.transition"

StateName = Annotated[str, Field(strict=True, pattern=r"^[a-z][a-z0-9_]{0,63}$")]
TransitionName = Annotated[str, Field(strict=True, pattern=f"^{RECORD_ACTION}$")]


def _guard(source: str) -> str:
    compile_condition(source, GUARD_VARIABLES)
    return source


GuardSource = Annotated[str, Field(strict=True), AfterValidator(_guard)]


def _result_guard(source: str) -> str:
    compile_condition(source, RESULT_VARIABLES)
    return source


ResultGuardSource = Annotated[str, Field(strict=True), AfterValidator(_result_guard)]


class _Aliased(Record):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, populate_by_name=True
    )


class ReceiptRequirement(_Aliased):
    """Evidence a transition needs: a successful call of `operation` by the caller.

    `same_task` accepts a call of the caller's own task (its run). `same_caller` accepts one
    of any task of the same agent principal within `window_seconds` before the transition.
    The cited call must name this record: its `record_id` argument, or an idempotency
    context `<record_id>.<epoch>`.

    `result` (ADR-034 amendment, 2026-11-16) is a CEL guard over the cited call's output,
    as `result`: for example `result.passed == true` accepts only a passing `env.try`
    run. It has the record guards' limits (no comprehension macros), so an operation
    that wants such a guard reports a summary field. False, an error, or a receipt with
    no output refuses the transition (`receipt_result_refused`).
    """

    operation: OperationName
    within: Literal["same_task", "same_caller"] = Field("same_task", alias="in")
    window_seconds: Annotated[int, Field(strict=True, ge=60, le=30 * 24 * 3600)] | None = None
    result: ResultGuardSource | None = None

    @model_validator(mode="after")
    def window(self) -> Self:
        if self.within == "same_caller" and self.window_seconds is None:
            raise ValueError("A same_caller receipt requirement needs window_seconds")
        return self


class TransitionApproval(Record):
    """An ADR-036 approval gate. `when` (CEL) sees the `records.transition` call arguments as
    `args`; it is combined with the collection and transition the rule applies to."""

    level: Literal["always", "conditional"]
    when: str | None = None
    reason: Reason
    expires_after_seconds: (
        Annotated[int, Field(strict=True, ge=MIN_EXPIRY_SECONDS, le=MAX_EXPIRY_SECONDS)] | None
    ) = None
    on_expiry: OnExpiry | None = None
    distinct_from_supervisor: bool = False

    @model_validator(mode="after")
    def condition(self) -> Self:
        if (self.level == "conditional") != (self.when is not None):
            raise ValueError("A conditional approval needs 'when'; an 'always' one takes none")
        return self


class Transition(_Aliased):
    name: TransitionName
    from_: Annotated[tuple[StateName, ...], Field(alias="from", min_length=1, max_length=64)]
    to: StateName
    guard: GuardSource | None = None
    claim: Literal["acquire", "holder", "release", "none"] = "none"
    requires_receipt: ReceiptRequirement | None = None
    approval: TransitionApproval | None = None
    # Where an expired claim returns the record; only for `claim: acquire`.
    on_claim_expiry: StateName | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.name in RESERVED_ACTIONS:
            raise ValueError(f"Transition name {self.name!r} is reserved for record operations")
        if len(set(self.from_)) != len(self.from_):
            raise ValueError(f"Transition {self.name} repeats a from state")
        if self.on_claim_expiry is not None and self.claim != "acquire":
            raise ValueError(f"Transition {self.name}: on_claim_expiry needs claim: acquire")
        return self


class StateMigration(_Aliased):
    """Where records in a removed state go (decision 3.3)."""

    from_: StateName = Field(alias="from")
    to: StateName


def check_table(
    states: tuple[str, ...] | None,
    initial: str | None,
    terminal: tuple[str, ...],
    transitions: tuple[Transition, ...],
    migrate: tuple[StateMigration, ...],
    has_claims: bool,
) -> None:
    """Static checks of one collection's table; raises ValueError for the spec validator."""
    if states is None:
        if initial is not None or terminal or transitions or migrate:
            raise ValueError("initial, terminal, transitions and migrate need states")
        return
    known = set(states)
    if len(known) != len(states):
        raise ValueError("States must be distinct")
    if initial is None or initial not in known:
        raise ValueError("A state machine needs an initial state from its states")
    if not set(terminal) <= known:
        raise ValueError("Terminal states must be states")
    names = [t.name for t in transitions]
    if len(set(names)) != len(names):
        raise ValueError("Transition names must be distinct")
    for transition in transitions:
        used = {*transition.from_, transition.to}
        if transition.on_claim_expiry is not None:
            used.add(transition.on_claim_expiry)
        if not used <= known:
            raise ValueError(f"Transition {transition.name} names an unknown state")
        if set(transition.from_) & set(terminal):
            raise ValueError(f"Transition {transition.name} leaves a terminal state")
        if transition.claim != "none" and not has_claims:
            raise ValueError(f"Transition {transition.name} has a claim effect; set claims")
    sources = [m.from_ for m in migrate]
    if len(set(sources)) != len(sources):
        raise ValueError("Each removed state migrates once")
    for move in migrate:
        if move.from_ in known:
            raise ValueError(f"State {move.from_} is migrated but still declared")
        if move.to not in known:
            raise ValueError(f"State {move.from_} migrates to an unknown state")


def approval_rule(collection: str, transition: Transition) -> GrantApprovalRule | None:
    """The ADR-036 rule a transition's `approval` compiles to (decision 3.2)."""
    gate = transition.approval
    if gate is None:
        return None
    match = f"args.collection == '{collection}' && args.transition == '{transition.name}'"
    when = match if gate.when is None else f"({match}) && ({gate.when})"
    return GrantApprovalRule(
        operation=TRANSITION_OPERATION,
        rule_id=f"transition.{transition.name}"[:64],
        level="conditional",
        when=when,
        reason=gate.reason,
        expires_after_seconds=gate.expires_after_seconds,
        on_expiry=gate.on_expiry,
        distinct_from_supervisor=gate.distinct_from_supervisor,
    )


def approval_resource(arguments: Mapping[str, JsonValue]) -> str:
    """The right whose `approve` mode decides a transition (ADR-036 amendment, 2026-11-09)."""
    return record_grant(str(arguments["collection"]), str(arguments["transition"]))


# The action each built-in record operation needs a `record:<collection>/<action>` grant for.
_ACTIONS = {
    "records.create": "create",
    "records.get": "read",
    "records.query": "read",
    "records.claimed": "read",
    "records.update": "update",
    "records.claim": "claim",
    "records.release": "claim",
}


def record_right(operation: str, arguments: Mapping[str, JsonValue]) -> str:
    """The resource-qualified grant one call of a built-in record operation needs."""
    action = (
        str(arguments["transition"]) if operation == TRANSITION_OPERATION else _ACTIONS[operation]
    )
    return record_grant(str(arguments["collection"]), action)


def claiming_grants(collection: str, transitions: Iterable[Transition]) -> set[str]:
    """Grants that let a pool hold a record: standalone claims and acquiring transitions."""
    grants = {record_grant(collection, "claim")}
    grants |= {record_grant(collection, t.name) for t in transitions if t.claim == "acquire"}
    return grants


__all__ = [
    "GUARD_VARIABLES",
    "TRANSITION_OPERATION",
    "ApprovalRule",
    "GuardSource",
    "ReceiptRequirement",
    "StateMigration",
    "Transition",
    "TransitionApproval",
    "approval_resource",
    "approval_rule",
    "check_table",
    "claiming_grants",
]
