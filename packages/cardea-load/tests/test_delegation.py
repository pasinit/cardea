from copy import deepcopy
from typing import Any

import pytest

from cardea_load.delegation import PROFILE, delegation_baseline, delegation_compare


def run(kind: str) -> dict[str, Any]:
    return {
        "schema_version": "m4-load-1",
        "kind": kind,
        "complete": True,
        "contract": {"profile": PROFILE, "harness": "same"},
        "points": [
            {
                "tasks": [{"passed": True}] * 4,
                "passed": True,
                "admission_p95_ms": 1,
                "completion_p95_ms": 10,
                "completion_rps": 2,
            }
        ]
        * (2 if kind == "reference" else 1),
    }


def test_comparison_reuses_metrics_without_accepting_other_profiles() -> None:
    baseline = delegation_baseline(run("reference"))
    assert delegation_compare(baseline, run("comparison"))["correctness_passed"]
    wrong = deepcopy(run("comparison"))
    wrong["contract"]["profile"]["children"] = 3
    with pytest.raises(ValueError, match="M4"):
        delegation_compare(baseline, wrong)


def test_incomplete_and_failed_reference_rejected() -> None:
    failed = run("reference")
    failed["points"][0]["passed"] = False
    with pytest.raises(ValueError):
        delegation_baseline(failed)
    incomplete = run("reference")
    incomplete["complete"] = False
    with pytest.raises(ValueError):
        delegation_baseline(incomplete)


def test_m3_and_environment_mismatch_rejected() -> None:
    baseline = delegation_baseline(run("reference"))
    other = run("comparison")
    other["schema_version"] = "m3-load-1"
    with pytest.raises(ValueError):
        delegation_compare(baseline, other)
    other = run("comparison")
    other["contract"]["harness"] = "different"
    with pytest.raises(ValueError):
        delegation_compare(baseline, other)
