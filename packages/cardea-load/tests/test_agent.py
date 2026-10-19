from copy import deepcopy
from typing import Any

import pytest

from cardea_load.agent import METRICS, PROFILE, agent_baseline, agent_compare


def sample(kind: str) -> dict[str, Any]:
    point = {
        "tasks": [{"passed": True} for _ in range(4)],
        "passed": True,
        "admission_p95_ms": 100,
        "completion_p95_ms": 1000,
        "completion_rps": 2,
    }
    return {
        "schema_version": "m3-load-1",
        "kind": kind,
        "complete": True,
        "contract": {"profile": PROFILE, "host": "debian-test"},
        "points": [deepcopy(point) for _ in range(2 if kind == "reference" else 1)],
    }


def test_matched_reference_and_performance_warnings() -> None:
    reference = agent_baseline(sample("reference"))
    candidate = sample("comparison")
    result = agent_compare(reference, candidate)
    assert result["correctness_passed"] and not result["warnings"]
    candidate["points"][0].update(admission_p95_ms=200, completion_rps=1)
    result = agent_compare(reference, candidate)
    assert set(result["warnings"]) == {"admission_p95_ms", "completion_rps"}
    assert set(result["changes"]) == set(METRICS)


@pytest.mark.parametrize("damage", ["incomplete", "missing_task", "failed"])
def test_bad_reference_cannot_approve(damage: str) -> None:
    run = sample("reference")
    if damage == "incomplete":
        run["complete"] = False
    elif damage == "missing_task":
        run["points"][0]["tasks"].pop()
    else:
        run["points"][0]["passed"] = False
    with pytest.raises(ValueError):
        agent_baseline(run)


def test_different_environment_is_incomparable_and_failure_is_not_warning() -> None:
    reference = agent_baseline(sample("reference"))
    candidate = sample("comparison")
    candidate["contract"]["host"] = "different-host"
    with pytest.raises(ValueError):
        agent_compare(reference, candidate)
    candidate = sample("comparison")
    candidate["points"][0]["passed"] = False
    assert not agent_compare(reference, candidate)["correctness_passed"]
