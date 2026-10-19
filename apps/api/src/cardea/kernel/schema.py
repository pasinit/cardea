"""Emit the implemented kernel record schemas without changing the demo API."""

import json

from pydantic import BaseModel
from pydantic.json_schema import JsonSchemaValue, models_json_schema

from cardea.kernel import CONTRACT_VERSION
from cardea.kernel.models import (
    AgentIdentity,
    BudgetLimits,
    CapabilityGrant,
    Checkpoint,
    KernelEvent,
    KernelFailure,
    ObjectVersion,
    OperationReceipt,
    OperationRequest,
    Task,
    VerificationRecord,
)

CONTRACT_MODELS: tuple[type[BaseModel], ...] = (
    AgentIdentity,
    CapabilityGrant,
    BudgetLimits,
    Task,
    Checkpoint,
    ObjectVersion,
    OperationRequest,
    OperationReceipt,
    KernelEvent,
    VerificationRecord,
    KernelFailure,
)


def contract_schema() -> JsonSchemaValue:
    _, schema = models_json_schema(
        [(model, "validation") for model in CONTRACT_MODELS],
        title=f"Cardea kernel records {CONTRACT_VERSION}",
        description="Portable data contracts only; no transport or execution guarantees.",
    )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        **schema,
        "oneOf": [{"$ref": f"#/$defs/{model.__name__}"} for model in CONTRACT_MODELS],
    }


if __name__ == "__main__":
    print(json.dumps(contract_schema(), indent=2, sort_keys=True))
