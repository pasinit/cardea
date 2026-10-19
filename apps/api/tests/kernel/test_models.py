from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from cardea.kernel import CONTRACT_VERSION
from cardea.kernel.models import (
    AgentIdentity,
    BudgetLimits,
    CapabilityGrant,
    Checkpoint,
    KernelEvent,
    KernelModel,
    OperationReceipt,
    OperationRequest,
    ResourceRef,
    Task,
    VerificationRecord,
)
from cardea.kernel.schema import CONTRACT_MODELS, contract_schema


def test_records_round_trip_and_reject_unknown_versions_and_fields(
    task: Task,
    grant: CapabilityGrant,
    checkpoint: Checkpoint,
    receipt: OperationReceipt,
    verification: VerificationRecord,
    now: datetime,
) -> None:
    records: tuple[KernelModel, ...] = (
        task,
        grant,
        checkpoint,
        receipt,
        verification,
        AgentIdentity(
            agent_id=task.agent_id,
            owner_id="supervisor",
            display_name="Indexer",
            created_at=now,
        ),
        KernelEvent(
            event_id="event-1",
            namespace="inputs",
            cursor=1,
            event_type="input.changed",
            producer_id="input-adapter",
            correlation_id=task.task_id,
            occurred_at=now,
            payload={"source_version": 2},
        ),
    )
    for record in records:
        model = type(record)
        assert model.model_validate_json(record.model_dump_json()) == record
        assert record.model_dump()["schema_version"] == CONTRACT_VERSION
        for invalid in ({"schema_version": "999"}, {"unrecognized_field": True}):
            with pytest.raises(ValidationError):
                model.model_validate(record.model_dump() | invalid)


@pytest.mark.parametrize("invalid", [-1, True, "10", 1.5])
def test_budgets_reject_negative_and_coerced_limits(limits: BudgetLimits, invalid: object) -> None:
    with pytest.raises(ValidationError):
        BudgetLimits.model_validate(limits.model_dump() | {"max_model_tokens": invalid})


def test_zero_budget_can_disable_model_calls(limits: BudgetLimits) -> None:
    disabled = BudgetLimits.model_validate(limits.model_dump() | {"max_model_calls": 0})
    assert disabled.max_model_calls == 0
    with pytest.raises(ValidationError):
        BudgetLimits.model_validate(limits.model_dump() | {"max_concurrent_operations": 0})


@pytest.mark.parametrize("identifier", ["", "../secret", "/etc/passwd", "*", "with space"])
def test_resource_identifiers_are_opaque_handles(identifier: str) -> None:
    with pytest.raises(ValidationError):
        ResourceRef(namespace="outputs", resource_id=identifier)


@pytest.mark.parametrize(
    "change",
    [
        {"operations": []},
        {"operations": ["*"]},
        {"operations": ["objects.write", "objects.write"]},
        {"resources": []},
        {"parent_grant_id": "grant-output"},
    ],
)
def test_grants_reject_ambiguous_scopes(grant: CapabilityGrant, change: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        CapabilityGrant.model_validate(grant.model_dump() | change)


def test_grant_validity_and_budget_must_agree(grant: CapabilityGrant, now: datetime) -> None:
    for change in (
        {"expires_at": now},
        {"revoked_at": now - timedelta(seconds=1)},
        {"expires_at": grant.limits.deadline - timedelta(seconds=1)},
    ):
        with pytest.raises(ValidationError):
            CapabilityGrant.model_validate(grant.model_dump() | change)


def test_times_require_timezone_and_normalize_to_utc(task: Task, now: datetime) -> None:
    same_instant = now.astimezone(timezone(timedelta(hours=3)))
    decoded = Task.model_validate(task.model_dump() | {"created_at": same_instant})
    assert decoded.created_at.isoformat() == now.isoformat()
    with pytest.raises(ValidationError):
        Task.model_validate(task.model_dump() | {"created_at": now.replace(tzinfo=None)})


@pytest.mark.parametrize(
    "arguments",
    [
        {"bad": float("nan")},
        {"nested": [float("inf")]},
        {"large": "a" * 65537},
    ],
)
def test_arguments_are_finite_and_bounded(
    operation_request: OperationRequest,
    arguments: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        OperationRequest.model_validate(operation_request.model_dump() | {"arguments": arguments})


def test_request_digest_ignores_json_key_order_and_normalizes_timezone(
    operation_request: OperationRequest,
) -> None:
    reordered = OperationRequest.model_validate(
        operation_request.model_dump()
        | {
            "arguments": {"content": {"count": 1, "items": ["input-1"]}, "expected_version": 0},
            "deadline": operation_request.deadline.astimezone(timezone(timedelta(hours=-4))),
        }
    )
    assert reordered.digest() == operation_request.digest()
    assert reordered.model_dump_json() != operation_request.model_dump_json()


def test_receipt_rejects_changed_payload_even_after_nested_mutation(
    receipt: OperationReceipt,
) -> None:
    receipt.request.arguments["expected_version"] = 99
    with pytest.raises(ValidationError, match="digest"):
        OperationReceipt.model_validate(receipt)


def test_verification_requires_artifact_versions_and_observed_evidence(
    verification: VerificationRecord,
) -> None:
    changes: tuple[dict[str, object], ...] = (
        {"artifacts": []},
        {"evidence": []},
        {"passed": "true"},
    )
    for change in changes:
        with pytest.raises(ValidationError):
            VerificationRecord.model_validate(verification.model_dump() | change)


def test_schema_catalog_has_versioned_closed_records_and_resolvable_references() -> None:
    schema = contract_schema()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    definitions = schema["$defs"]
    for model in CONTRACT_MODELS:
        record = definitions[model.__name__]
        assert record["additionalProperties"] is False
        assert record["properties"]["schema_version"]["const"] == CONTRACT_VERSION

    def check_refs(value: object) -> None:
        if isinstance(value, dict):
            if "$ref" in value:
                assert value["$ref"].removeprefix("#/$defs/") in definitions
            for item in value.values():
                check_refs(item)
        elif isinstance(value, list):
            for item in value:
                check_refs(item)

    check_refs(schema)
