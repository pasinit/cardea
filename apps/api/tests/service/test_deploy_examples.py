"""The small-host deployment examples (ADR-030) stay valid against the current models."""

import json
from pathlib import Path

from cardea.service.models import Provisioning, ServiceConfig

KIT = Path(__file__).resolve().parents[4] / "deploy" / "small-host"


def test_service_config_example_is_valid() -> None:
    config = json.loads((KIT / "cardea" / "config.json").read_text())
    config["network"]["host"] = "10.0.0.2"  # placeholder in the example
    parsed = ServiceConfig.model_validate(config)
    assert parsed.network is not None and parsed.agent_session is not None


def test_provisioning_example_is_valid_and_grants_schedules() -> None:
    document = Provisioning.model_validate_json(
        (KIT / "cardea" / "provision.telos.json").read_text()
    )
    rights = {p.operation for p in document.permissions if p.principal_id == "telos"}
    assert {"tasks.schedule", "models.invoke", "mail.send"} <= rights
    assert {p.principal_id for p in document.platform} == {"platform"}
