"""The JSON examples in docs/getting-started.md stay valid requests and configuration."""

import json
import re
from pathlib import Path
from typing import Any

from cardea.service.harnesses import Catalog, HarnessFile
from cardea.service.models import (
    AgentGroup,
    AgentRunRequest,
    InboundEvent,
    OperationTaskRequest,
    TriggerSpec,
)
from cardea.service.operations import Binding, Registry
from cardea.service.records import CollectionDocument

GUIDE = Path(__file__).parents[4] / "docs" / "getting-started.md"


def examples() -> list[dict[str, Any]]:
    blocks = re.findall(r"```json\n(.*?)```", GUIDE.read_text(), re.S)
    pinned = (b.replace("<from packaging/fetch_opencode.py>", "0" * 64) for b in blocks)
    return [json.loads(b.replace("<same>", "0" * 64)) for b in pinned]


def test_every_example_validates() -> None:
    (
        single,
        run,
        trigger,
        on_event,
        event,
        effect,
        confined,
        research,
        registry,
        group,
        collection,
        create,
        update,
    ) = examples()
    CollectionDocument.model_validate(collection)
    OperationTaskRequest.model_validate(create)
    OperationTaskRequest.model_validate(update)
    for request in (create, update):
        Registry().validate_arguments(request["operation"], request["arguments"])
    OperationTaskRequest.model_validate(single)
    assert AgentGroup.model_validate(group).spec.pools[0].replicas == 2
    AgentRunRequest.model_validate(run)
    assert TriggerSpec.model_validate(trigger).request("daily-recap.20261020T063000Z")
    spec = TriggerSpec.model_validate(on_event)
    assert spec.event is not None
    inbound = InboundEvent.model_validate({k: event[k] for k in ("id", "source", "type", "data")})
    assert spec.event.matches(inbound, {"subject": event["subject"]})
    assert spec.request("issue-triage.e0123456789abcd", event=inbound)
    Binding.model_validate(effect["cardea"])
    Binding.model_validate(confined["cardea"])
    AgentRunRequest.model_validate(research)
    catalog = Catalog.from_file(HarnessFile.model_validate(registry))
    for harness, model in (("claude", None), ("openrouter", "z-ai/glm-4.6"), ("local", "qwen3:8b")):
        selected = AgentRunRequest.model_validate(
            {
                **run,
                "harness": harness,
                "model": model,
                "grants": [*run["grants"], f"harness:{harness}"],
            }
        )
        chosen, _ = catalog.resolve("team", selected.harness, selected.model)
        catalog.check_grant(chosen, selected.grants)
