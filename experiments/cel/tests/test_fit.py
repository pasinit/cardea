"""Observed behaviour of both CEL candidates on ADR-036 approval-rule cases.

Passing means the recorded observations still hold, including the deviations recorded for
cel-python; it is not a claim that either library satisfies every Cardea requirement alone.
"""

from typing import Any

import pytest
from fit import CANDIDATES, Candidate, footprint, probe

RESULTS: dict[str, dict[str, Any]] = {}


@pytest.fixture(scope="module", params=sorted(CANDIDATES))
def result(request: pytest.FixtureRequest) -> tuple[str, dict[str, Any]]:
    name = str(request.param)
    if name not in RESULTS:
        RESULTS[name] = probe(name)
    return name, RESULTS[name]


EXPECTED_MISMATCHES = {
    # Heterogeneous int/double ordering is missing one way round; bool equals int 1; the
    # reserved identifier `namespace` is accepted.
    "cel-python": [
        "int_arg_double_literal",
        "bool_not_int",
        "bool_ne_int",
        "reserved_namespace_identifier",
    ],
    "common-expression-language": [],
}


def test_spec_cases(result: tuple[str, dict[str, Any]]) -> None:
    name, observed = result
    assert sorted(observed["spec_mismatches"]) == sorted(EXPECTED_MISMATCHES[name])


def test_errors_are_surfaced_not_values(result: tuple[str, dict[str, Any]]) -> None:
    """Missing fields, type mismatches and ambient lookups fail, so a caller can fail closed."""
    _, observed = result
    for case in observed["cases"]:
        if case["id"].startswith(("missing_field", "missing_nested", "type_", "no_", "int_ov")):
            assert case["observed"] == "error", case


def test_cost_has_no_built_in_bound(result: tuple[str, dict[str, Any]]) -> None:
    """Neither exposes a cost budget; comprehension cost grows with argument size."""
    _, observed = result
    cost = observed["cost"]
    assert cost["argument_bytes"] <= 65536 + 512
    assert cost["map_filter_2000"]["seconds"] > cost["map_filter_1000"]["seconds"]
    assert cost["nesting"]["deepest_ok"] >= 32


def test_rust_parse_depth_fails_cleanly() -> None:
    nesting = probe_cached("common-expression-language")["cost"]["nesting"]
    assert 64 <= nesting["deepest_ok"] < 100_000
    assert nesting["first_failure"].startswith("depth")


def test_native_footprint_selects_rust_binding() -> None:
    """ADR-036 rule: with both acceptable, prefer fewer native dependencies."""
    native = {name: footprint(name)["native_distributions"] for name in CANDIDATES}
    python_names = {item["distribution"] for item in native["cel-python"]}
    rust_names = {item["distribution"] for item in native["common-expression-language"]}
    assert python_names == {"google-re2", "pendulum", "pyyaml"}
    # wcwidth's optional C accelerator arrives through the package's CLI dependencies.
    assert rust_names <= {"common-expression-language", "wcwidth"}
    assert len(rust_names) < len(python_names)


def test_rust_binding_has_no_ambient_functions() -> None:
    candidate = Candidate("common-expression-language")
    for expr in ("now() > 1", "getenv('HOME') == ''", "open('/etc/passwd') == ''"):
        assert candidate.outcome(expr, {})[0] == "error"


def probe_cached(name: str) -> dict[str, Any]:
    if name not in RESULTS:
        RESULTS[name] = probe(name)
    return RESULTS[name]
