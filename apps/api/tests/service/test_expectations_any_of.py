"""Per-run expectation alternatives and argument match (ADR-029 amendment, 2026-11-16)."""

from typing import Any

import pytest
import test_agent_runs as runs
from mcp.client.session import ClientSession
from pydantic import ValidationError
from test_agent_runs import FakeSession, Tools, call, run_request, submit

from cardea.runtime.agent_session import SessionRequest, SessionResult
from cardea.service.agent_run_runtime import AgentRunRuntime
from cardea.service.database import borrow
from cardea.service.kernel import Kernel
from cardea.service.models import AgentRunRequest, WindowExpectation

kernel, session, tools = runs.kernel, runs.session, runs.tools
LEAD = "lead@example.invalid"
# "A pull request, or a message to the team lead": here a search or a mail to the lead.
EITHER = {
    "any_of": [
        {"operation": "listings.search", "min_calls": 2},
        {"operation": "mail.send", "match": {"/to": LEAD}},
    ]
}


def sends_to(address: str, searches: int = 0) -> Any:
    async def script(client: ClientSession, request: SessionRequest) -> SessionResult:
        for _ in range(searches):
            assert not (await call(client, "listings-search")).is_error
        sent = await call(client, "mail-send", to=address, body="status")
        assert not sent.is_error, sent.content
        return SessionResult(text="Reported.", is_error=False)

    return script


def failure_code(kernel: Kernel, task_id: str) -> str | None:
    with kernel.database.read() as conn, borrow(conn) as tx:
        failure = tx.get_task(task_id).failure
    return failure.code if failure else None


def test_any_of_is_met_by_one_alternative(
    kernel: Kernel, tools: Tools, session: FakeSession
) -> None:
    session.scripts = [sends_to(LEAD)]
    task_id = submit(kernel, expect=[EITHER], max_attempts=1)
    assert AgentRunRuntime(kernel).execute(task_id) == "succeeded"
    (prompt,) = [r.prompt for r in session.requests]
    assert "one of: at least 2 successful call(s) of listings-search; or" in prompt
    assert '/to = "lead@example.invalid"' in prompt


def test_any_of_unmet_when_no_alternative_is_met(
    kernel: Kernel, tools: Tools, session: FakeSession
) -> None:
    # One search and a mail to someone else: neither alternative holds.
    session.scripts = [sends_to("other@example.invalid", searches=1)]
    task_id = submit(kernel, expect=[EITHER], max_attempts=1)
    assert AgentRunRuntime(kernel).execute(task_id) == "failed"
    assert failure_code(kernel, task_id) == "expectation_unmet"


def test_any_of_exceeded_only_when_every_alternative_is(
    kernel: Kernel, tools: Tools, session: FakeSession
) -> None:
    capped = {
        "any_of": [
            {"operation": "mail.send", "min_calls": 0, "max_calls": 0},
            {"operation": "listings.search", "min_calls": 0, "max_calls": 0},
        ]
    }
    session.scripts = [sends_to(LEAD)]
    task_id = submit(kernel, request_id="one-exceeded", expect=[capped], max_attempts=1)
    # mail.send is exceeded but zero searches still meets the other alternative.
    assert AgentRunRuntime(kernel).execute(task_id) == "succeeded"
    session.scripts = [sends_to(LEAD, searches=1)]
    task_id = submit(kernel, request_id="all-exceeded", expect=[capped], max_attempts=1)
    assert AgentRunRuntime(kernel).execute(task_id) == "failed"
    assert failure_code(kernel, task_id) == "expectation_exceeded"


def test_match_alone_counts_only_matching_calls(
    kernel: Kernel, tools: Tools, session: FakeSession
) -> None:
    to_lead = {"operation": "mail.send", "match": {"/to": LEAD}}
    session.scripts = [sends_to("other@example.invalid")]
    task_id = submit(kernel, expect=[to_lead], max_attempts=1)
    assert AgentRunRuntime(kernel).execute(task_id) == "failed"
    assert failure_code(kernel, task_id) == "expectation_unmet"


def test_requests_without_the_amendment_keep_their_digests() -> None:
    plain = run_request(expect=[{"operation": "mail.send", "min_calls": 1}])
    assert plain.model_dump(mode="json")["expect"] == [
        {"operation": "mail.send", "min_calls": 1, "max_calls": None}
    ]
    window = WindowExpectation.model_validate({"operation": "mail.send", "min": 1, "window": "day"})
    assert "any_of" not in window.model_dump(mode="json")
    assert "match" not in window.model_dump(mode="json")


@pytest.mark.parametrize(
    "entry",
    [
        {"any_of": [{"operation": "mail.send"}]},  # one alternative is not a choice
        {"any_of": [{"operation": "mail.send"}, {"operation": "other.op"}]},  # not exposed
        {"operation": "mail.send", "match": {"to": LEAD}},  # not a pointer
    ],
)
def test_invalid_alternatives_are_refused(entry: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        AgentRunRequest.model_validate({**run_request().model_dump(mode="json"), "expect": [entry]})


@pytest.mark.parametrize(
    "entry",
    [
        {"operation": "mail.send", "any_of": [], "window": "day"},
        {
            "operation": "mail.send",
            "min": 1,
            "any_of": [
                {"operation": "mail.send", "min": 1},
                {"operation": "listings.search", "min": 1},
            ],
            "window": "day",
        },
        {"any_of": [{"operation": "mail.send"}, {"operation": "x.y", "min": 1}], "window": "day"},
    ],
)
def test_invalid_window_alternatives_are_refused(entry: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        WindowExpectation.model_validate(entry)
