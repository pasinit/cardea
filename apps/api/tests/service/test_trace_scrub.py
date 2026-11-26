import json

from cardea.service.trace_scrub import REDACTED, TRUNCATED, Limits, scrub_run, scrub_span

SECRET = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"


def test_a_secret_in_an_attribute_never_reaches_the_exporter() -> None:
    span = {
        "name": f"call {SECRET}",
        "attributes": {
            "cardea.operation": f"push token={SECRET}",
            "cardea.error_code": f"Bearer {SECRET}",
            "cardea.member": "/home/alice/.ssh/id_rsa",
            "unlisted": SECRET,
        },
        "events": [{"name": SECRET, "attributes": {"cardea.outcome": f"password: {SECRET}"}}],
    }
    out = scrub_run([span])
    assert SECRET not in json.dumps(out.spans)
    assert "/home/alice" not in json.dumps(out.spans)
    assert "unlisted" not in out.spans[0]["attributes"]
    assert out.spans[0]["dropped_attributes"] == 1


def test_prompt_named_attributes_are_redacted_even_when_allowed() -> None:
    limits = Limits(allowed=frozenset({"llm.prompt"}))
    out = scrub_span({"attributes": {"llm.prompt": "do the thing"}}, limits)
    assert out["attributes"] == {"llm.prompt": REDACTED}


def test_non_scalar_values_are_dropped_and_scalars_kept() -> None:
    out = scrub_span(
        {"attributes": {"cardea.attempt": 2, "cardea.duration_ms": 1.5, "cardea.group": ["x"]}}
    )
    assert out["attributes"] == {"cardea.attempt": 2, "cardea.duration_ms": 1.5}
    assert out["dropped_attributes"] == 1


def test_long_values_are_clipped_with_a_marker() -> None:
    out = scrub_span({"attributes": {"cardea.group": "word " * 200}}, Limits(max_attr_len=40))
    value = out["attributes"]["cardea.group"]
    assert len(value) == 40 and value.endswith(TRUNCATED)


def test_attribute_and_event_caps_count_what_they_drop() -> None:
    limits = Limits(max_attrs=2, max_events=1)
    span = {
        "attributes": {"cardea.group": "g", "cardea.member": "m", "cardea.run_id": "r"},
        "events": [{"name": "a"}, {"name": "b"}, {"name": "c"}],
    }
    out = scrub_span(span, limits)
    assert len(out["attributes"]) == 2 and out["dropped_attributes"] == 1
    assert len(out["events"]) == 1 and out["dropped_events"] == 2


def test_spans_beyond_the_run_cap_are_dropped_and_counted() -> None:
    out = scrub_run([{"name": str(i)} for i in range(5)], Limits(max_spans=3))
    assert [s["name"] for s in out.spans] == ["0", "1", "2"]
    assert out.dropped_spans == 2


def test_the_input_is_not_modified() -> None:
    span = {"attributes": {"cardea.group": SECRET}, "events": [{"name": "e"}]}
    before = json.dumps(span)
    scrub_span(span)
    assert json.dumps(span) == before


def test_malformed_spans_do_not_raise() -> None:
    out = scrub_run([{"attributes": "x", "events": 3, "name": 7}, {}])
    assert len(out.spans) == 2
