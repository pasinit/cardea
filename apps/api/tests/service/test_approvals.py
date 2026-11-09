"""Approval rule schema and fail-closed evaluator (ADR-036 slice 1); no runtime wiring."""

import time
from typing import Any

import pytest
from pydantic import JsonValue, TypeAdapter, ValidationError

from cardea.service.approvals import (
    MAX_CONDITION_CHARS,
    ApprovalRule,
    ConditionInvalid,
    GrantApprovalRule,
    GrantApprovals,
    HostApprovalRule,
    HostApprovals,
    OperationApproval,
    compile_condition,
    evaluate_condition,
    requirement,
)

OP = "payments.create"
NS = "acme"


def rule(rule_id: str = "large", **fields: Any) -> ApprovalRule:
    fields.setdefault("level", "conditional")
    if fields["level"] == "conditional":
        fields.setdefault("when", "args.amount > 500")
    return ApprovalRule.model_validate({"rule_id": rule_id, "reason": "Large payment", **fields})


def grant(rule_id: str = "sup", operation: str = OP, **fields: Any) -> GrantApprovalRule:
    fields.setdefault("level", "conditional")
    if fields["level"] == "conditional":
        fields.setdefault("when", "args.amount > 100")
    return GrantApprovalRule.model_validate(
        {"rule_id": rule_id, "reason": "Supervisor limit", "operation": operation, **fields}
    )


def needs(args: dict[str, JsonValue], **sources: Any) -> bool:
    return requirement(
        operation=OP,
        namespace=NS,
        args=args,
        descriptor=sources.get("descriptor"),
        host=sources.get("host"),
        grant=sources.get("grant", ()),
    ).required


@pytest.mark.parametrize(
    ("when", "args", "expected"),
    [
        ("args.amount > 500", {"amount": 600}, True),
        ("args.amount > 500", {"amount": 500}, False),
        ("args.amount > 500", {"amount": 500.5}, True),
        ("args.amount > 500.0", {"amount": 600}, True),
        ("args.amount > 500 || args.currency != 'EUR'", {"amount": 1, "currency": "USD"}, True),
        ("args.amount > 500 || args.currency != 'EUR'", {"amount": 1, "currency": "EUR"}, False),
        ("args.order.total >= 1000", {"order": {"total": 999}}, False),
        ("'prod' in args.tags", {"tags": ["dev", "prod"]}, True),
        ("has(args.override) && args.override", {}, False),
        ("operation == 'payments.create' && ns == 'acme'", {}, True),
        ("args.flag != 1", {"flag": True}, True),
    ],
)
def test_condition_values(when: str, args: dict[str, JsonValue], expected: bool) -> None:
    result = evaluate_condition(when, args, OP, NS)
    assert result.requires_approval is expected
    assert result.error is None


@pytest.mark.parametrize(
    ("when", "args"),
    [
        ("args.amount > 500", {}),  # missing field
        ("args.order.total > 5", {"order": {}}),  # missing nested field
        ("args.amount > 500", {"amount": "600"}),  # type mismatch
        ("args.amount > 'x'", {"amount": 1}),
        ("args.amount * 9223372036854775807 > 0", {"amount": 2}),  # overflow
        ("args.amount / 0 > 1", {"amount": 1}),
        ("args.amount", {"amount": 1}),  # not a boolean
        ("args.missing > 1 || args.currency != 'EUR'", {"currency": "EUR"}),
    ],
)
def test_evaluation_errors_require_approval(when: str, args: dict[str, JsonValue]) -> None:
    result = evaluate_condition(when, args, OP, NS)
    assert result.requires_approval is True
    assert result.error


def test_invalid_source_at_evaluation_fails_closed() -> None:
    assert evaluate_condition("args.amount >", {}, OP, NS).requires_approval is True


@pytest.mark.parametrize(
    "when",
    [
        "args.amount >",  # syntax
        "namespace == 'acme'",  # reserved CEL identifier
        "now() > 1",
        "request.time > 1",
        "args.items.exists(i, i.amount > 500)",
        "args.items.all(args, args > 1)",
        "args.items.map(x, x).size() > 1",
        "args.items . filter (x, x > 1) == []",
        "cel.bind(x, args.amount, x > 1)",
        "timestamp(args.at) > timestamp('2026-01-01T00:00:00Z')",
        "type(args.amount) == int",
        " ",
        "args.a == 1 || " * 40 + "true",  # too long
    ],
)
def test_refused_conditions(when: str) -> None:
    with pytest.raises(ConditionInvalid):
        compile_condition(when)
    with pytest.raises(ValidationError):
        rule(when=when)


def test_rule_shape() -> None:
    with pytest.raises(ValidationError):
        rule(level="conditional", when=None)
    with pytest.raises(ValidationError):
        rule(level="always", when="args.amount > 1")
    with pytest.raises(ValidationError):
        rule(expires_after_seconds=59)
    with pytest.raises(ValidationError):
        rule(expires_after_seconds=30 * 24 * 3600 + 1)
    with pytest.raises(ValidationError):
        rule(reason="two\nlines")
    with pytest.raises(ValidationError):
        rule(reason="x" * 201)
    with pytest.raises(ValidationError):
        ApprovalRule.model_validate({"rule_id": "r", "level": "never", "reason": "x"})
    with pytest.raises(ValidationError):
        OperationApproval(rules=(rule("a"), rule("a")))
    with pytest.raises(ValidationError):
        TypeAdapter(GrantApprovals).validate_python([grant("a"), grant("a")])
    TypeAdapter(GrantApprovals).validate_python([grant("a"), grant("a", "payments.refund")])
    with pytest.raises(ValidationError):
        HostApprovals(
            entries=(
                HostApprovalRule(namespace=NS, operation=OP),
                HostApprovalRule(namespace=NS, operation=OP),
            )
        )


def test_no_rule_is_autonomous() -> None:
    assert not needs({"amount": 10**6})
    assert not needs({"amount": 10**6}, descriptor=OperationApproval())


def test_levels() -> None:
    always = OperationApproval(rules=(rule(level="always"),))
    autonomous = OperationApproval(rules=(rule(level="autonomous"),))
    assert needs({"amount": 1}, descriptor=always)
    assert not needs({"amount": 10**6}, descriptor=autonomous)


def test_host_rule_replaces_descriptor_default_for_its_namespace() -> None:
    descriptor = OperationApproval(rules=(rule(level="always"),))
    looser = HostApprovals(entries=(HostApprovalRule(namespace=NS, operation=OP),))
    other_ns = HostApprovals(entries=(HostApprovalRule(namespace="other", operation=OP),))
    assert not needs({"amount": 1}, descriptor=descriptor, host=looser)
    assert needs({"amount": 1}, descriptor=descriptor, host=other_ns)
    tighter = HostApprovals(
        entries=(HostApprovalRule(namespace=NS, operation=OP, rules=(rule("host-any"),)),)
    )
    assert needs({"amount": 600}, host=tighter)
    assert not needs({"amount": 6}, host=tighter)


def test_grant_rule_only_tightens() -> None:
    descriptor = OperationApproval(rules=(rule(),))
    # A supervisor's autonomous rule, even with the default's rule_id, removes nothing.
    loosen = (grant("large", level="autonomous"),)
    assert needs({"amount": 600}, descriptor=descriptor, grant=loosen)
    host = HostApprovals(
        entries=(HostApprovalRule(namespace=NS, operation=OP, rules=(rule(level="always"),)),)
    )
    assert needs({"amount": 1}, host=host, grant=loosen)
    # It adds a lower threshold.
    assert needs({"amount": 200}, descriptor=descriptor, grant=(grant(),))
    assert not needs({"amount": 50}, descriptor=descriptor, grant=(grant(),))
    # Rules for other operations do not apply.
    assert not needs({"amount": 200}, grant=(grant(operation="payments.refund"),))


def test_combined_requirement_takes_strictest_terms() -> None:
    descriptor = OperationApproval(
        rules=(rule("a", expires_after_seconds=7200, on_expiry="resume"),)
    )
    host = None
    grants = (
        grant("b", expires_after_seconds=600, on_expiry="fail", distinct_from_supervisor=True),
        grant("c", when="args.amount > 100000"),
    )
    result = requirement(
        operation=OP,
        namespace=NS,
        args={"amount": 1000},
        descriptor=descriptor,
        host=host,
        grant=grants,
    )
    assert result.required
    assert [(r.source, r.rule_id) for r in result.rules] == [("operation", "a"), ("grant", "b")]
    assert result.expires_after_seconds == 600
    assert result.on_expiry == "fail"
    assert result.distinct_from_supervisor
    assert result.resolved_on_expiry(agent_run=True) == "fail"


def test_on_expiry_default_depends_on_task_kind() -> None:
    result = requirement(
        operation=OP,
        namespace=NS,
        args={},
        descriptor=OperationApproval(rules=(rule(level="always"),)),
        host=None,
    )
    assert result.on_expiry is None
    assert result.resolved_on_expiry(agent_run=True) == "resume"
    assert result.resolved_on_expiry(agent_run=False) == "fail"


def test_error_is_reported_on_the_requiring_rule() -> None:
    result = requirement(
        operation=OP,
        namespace=NS,
        args={},
        descriptor=OperationApproval(rules=(rule(),)),
        host=None,
    )
    assert result.required
    assert result.rules[0].error and "amount" in result.rules[0].error


def test_rules_digest_tracks_applicable_rules() -> None:
    def digest(**sources: Any) -> str:
        return requirement(
            operation=OP,
            namespace=NS,
            args={"amount": 1},
            descriptor=sources.get("descriptor"),
            host=None,
            grant=sources.get("grant", ()),
        ).rules_digest

    base = OperationApproval(rules=(rule(),))
    assert digest(descriptor=base) == digest(descriptor=base)
    assert digest(descriptor=base) != digest(descriptor=base, grant=(grant(),))
    assert digest(descriptor=base) != digest(
        descriptor=OperationApproval(rules=(rule(when="args.amount > 501"),))
    )
    assert (
        HostApprovals().digest
        != HostApprovals(entries=(HostApprovalRule(namespace=NS, operation=OP),)).digest
    )


def test_bounded_cost_at_argument_limit() -> None:
    """The near-worst refused-macro-free condition stays fast over 64 KiB of arguments."""
    terms: list[str] = []
    while len(" + ".join([*terms, "args.l"]) + " == []") <= MAX_CONDITION_CHARS:
        terms.append("args.l")
    when = " + ".join(terms) + " == []"
    compile_condition(when)
    args: dict[str, JsonValue] = {"l": list(range(11_000))}
    start = time.perf_counter()
    assert evaluate_condition(when, args, OP, NS).requires_approval is False
    assert time.perf_counter() - start < 5
