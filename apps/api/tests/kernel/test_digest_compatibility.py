"""R03: JCS fit and persisted v0.1 compatibility, without changing production hashes."""

import json
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest
import rfc8785

from cardea.kernel.lifecycle import ContractError, match_operation_retry
from cardea.kernel.models import OperationReceipt, OperationRequest, ResourceRef, canonical_json

FIXTURE = Path(__file__).parent / "fixtures" / "v01_receipt.json"


def test_persisted_v01_receipt_keeps_its_literal_digest() -> None:
    data = FIXTURE.read_text()
    receipt = OperationReceipt.model_validate_json(data)
    assert receipt.request.digest() == json.loads(data)["request_digest"]
    assert match_operation_retry(receipt, receipt.request) == receipt
    as_float = receipt.request.model_copy(update={"arguments": {"amount": 1.0, "label": "é"}})
    with pytest.raises(ContractError):
        match_operation_retry(receipt, as_float)


def test_jcs_changes_legacy_numeric_and_unicode_representation() -> None:
    receipt = OperationReceipt.model_validate_json(FIXTURE.read_text())
    request = receipt.request.model_dump(mode="json")
    assert sha256(rfc8785.dumps(request)).hexdigest() != receipt.request_digest
    assert canonical_json({"amount": 1}) != canonical_json({"amount": 1.0})
    assert rfc8785.dumps({"amount": 1}) == rfc8785.dumps({"amount": 1.0})
    assert rfc8785.dumps({"label": "é"}) == b'{"label":"\xc3\xa9"}'


def test_rfc8785_numeric_vector() -> None:
    # RFC 8785 section 3.2.2, independent expected wire bytes.
    value = [333333333.33333329, 1e30, 4.50, 2e-3, 0.000000000000000000000000001]
    assert rfc8785.dumps(value) == b"[333333333.3333333,1e+30,4.5,0.002,1e-27]"
    assert rfc8785.dumps([-0.0, 0.0]) == b"[0,0]"


def test_jcs_sorts_keys_by_utf16_and_does_not_normalize_unicode() -> None:
    value = {"\ue000": 1, "\U0001f600": 2}
    assert rfc8785.dumps(value) == '{"😀":2,"\ue000":1}'.encode()
    assert rfc8785.dumps({"s": "é"}) != rfc8785.dumps({"s": "e\u0301"})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 2**53, "\ud800"])
def test_jcs_rejects_values_outside_its_supported_domain(value: float | int | str) -> None:
    with pytest.raises(rfc8785.CanonicalizationError):
        rfc8785.dumps(value)


def test_legacy_allows_large_integer_that_requires_migration_policy() -> None:
    request = OperationRequest(
        operation_id="large-integer",
        task_id="task",
        subject_id="agent",
        name="objects.write",
        resource=ResourceRef(namespace="output", resource_id="index"),
        arguments={"amount": 2**53},
        deadline=datetime(2030, 1, 1, tzinfo=UTC),
    )
    assert request.digest()
    with pytest.raises(rfc8785.IntegerDomainError):
        rfc8785.dumps(request.model_dump(mode="json"))
