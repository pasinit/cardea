from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from cardea_load.metrics import baseline, classify, compare, fingerprint, oracle, save


def campaign() -> dict[str, Any]:
    return {
        "schema_version": "1",
        "kind": "reference",
        "complete": True,
        "contract": {"profile": "fixed", "cpu": 2},
        "source": {"app.py": "old"},
        "profile": {
            "rates": [1],
            "reference_repetitions": 3,
            "comparison_repetitions": 1,
            "performance_warnings": {
                "p95_increase_fraction": 0.25,
                "throughput_decrease_fraction": 0.2,
            },
        },
        "points": [
            {
                "rate": 1,
                "p95_ms": n,
                "p99_ms": n * 2,
                "completion_rps": 1,
                "accepted": 20,
                "capacity_rejected": 0,
                "passed": True,
            }
            for n in (10, 20, 30)
        ],
    }


def test_reference_is_measured_and_median_based() -> None:
    reference = baseline(campaign())
    assert reference["points"]["1"]["p95_ms"] == {"median": 20, "min": 10, "max": 30}


def test_exclusive_reference_creation(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    save(path, {"reference": 1})
    with pytest.raises(FileExistsError):
        save(path, {"reference": 2})
    assert '"reference": 1' in path.read_text()


def test_comparison_allows_app_change_but_rejects_different_environment() -> None:
    original = campaign()
    reference = baseline(original)
    current = deepcopy(original)
    current.update(kind="comparison", source={"app.py": "new"}, points=[original["points"][-1]])
    assert compare(reference, current)["warnings"] == ["1 rps: p95_ms"]
    current["contract"]["cpu"] = 4
    with pytest.raises(ValueError, match="Incomparable"):
        compare(reference, current)


def test_incomplete_or_failed_evidence_is_not_approved() -> None:
    original = campaign()
    original["points"].pop()
    with pytest.raises(ValueError, match="Incomplete"):
        baseline(original)
    original = campaign()
    original["points"][0]["passed"] = False
    reference = baseline(original)
    original["kind"] = "comparison"
    with pytest.raises(ValueError, match="correctness failures"):
        compare(reference, original)


@pytest.mark.parametrize(
    ("code", "denial", "fault", "expected"),
    [
        (201, False, False, "accepted"),
        (429, False, False, "capacity_rejected"),
        (403, False, False, "unexpected_error"),
        (403, True, False, "expected_denial"),
        (0, False, True, "fault_unavailable"),
        (500, True, True, "unexpected_error"),
    ],
)
def test_errors_are_not_hidden(code: int, denial: bool, fault: bool, expected: str) -> None:
    assert classify(code, denial, fault) == expected


def test_oracle_detects_lost_acks_duplicate_submission_and_false_success() -> None:
    row = {
        "task_id": "one",
        "supervisor": "s",
        "request_id": "r",
        "outputs": 0,
        "state": "succeeded",
        "verification": False,
    }
    errors = oracle(
        {"violations": [], "tasks": [row, {**row, "task_id": "two", "outputs": 2}]}, {"missing"}
    )
    assert "missing_acknowledged:missing" in errors
    assert "duplicate_submission" in errors
    assert "one:false_success" in errors
    assert "two:duplicate_artifact" in errors


def test_fingerprint_is_canonical() -> None:
    assert fingerprint({"a": 1, "b": 2}) == fingerprint({"b": 2, "a": 1})


def test_empty_comparison_is_not_a_success() -> None:
    original = campaign()
    reference = baseline(original)
    original.update(kind="comparison", points=[])
    with pytest.raises(ValueError, match="Incomplete comparison"):
        compare(reference, original)
