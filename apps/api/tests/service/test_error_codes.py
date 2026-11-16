"""The error-code reference matches the source it is generated from."""

from cardea.service.error_codes import DOCUMENT, collect, render


def test_committed_error_codes_are_current() -> None:
    assert DOCUMENT.read_text() == render(), "docs/error-codes.md is stale: run `make error-codes`"


def test_the_reference_covers_request_errors_and_recorded_failures() -> None:
    api, failures = collect()
    # A sample from each mechanism the generator reads.
    for code in ("member_busy", "authority_denied", "request_too_large", "revision_conflict"):
        assert code in api, code
    for code in ("content_refused", "content_gate_unavailable", "credential_in_change"):
        assert code in failures, code
    assert all(entry.modules for entry in (*api.values(), *failures.values()))
