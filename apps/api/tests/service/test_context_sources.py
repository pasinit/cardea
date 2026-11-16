"""Context sources and budgets (ADR-034 slice 6; ADR-031 as amended)."""

import asyncio
import json
import re
import threading
from typing import Any

import pytest
import test_agent_runs as runs
from mcp.client.session import ClientSession
from test_records import (
    SUPERVISOR,
    created,
    document,
    items,
    kernel,
)

from cardea.runtime.agent_session import SessionRequest, SessionResult
from cardea.service.agent_run_runtime import AgentRunRuntime, Outcome, context_block
from cardea.service.context_sources import DEFAULT_BYTES, Fetched, fit, trim
from cardea.service.database import shared
from cardea.service.kernel import Kernel, provision
from cardea.service.models import (
    DEFAULT_CONTEXT_BYTES,
    RIGHTS,
    AgentRunRequest,
    RunTemplate,
    now,
)

__all__ = ["items", "kernel"]

READ = "record:items/read"
GRANTS = (*RIGHTS, "models.invoke", "record:items/create", READ, "runs.recent")


# Budgets, without a kernel


def block(name: str, output: Any) -> Fetched:
    outcome = Outcome(output=output)
    return Fetched(name, None, output, lambda o: context_block(name, outcome, o))


def listing(n: int, width: int = 300) -> dict[str, Any]:
    return {"runs": [{"n": i, "text": "x" * width} for i in range(n)], "truncated": 0}


def test_a_list_block_drops_its_oldest_entries_and_says_how_many() -> None:
    output = listing(40)
    trimmed, cut = trim("runs.recent", output, 4096, lambda o: len(json.dumps(o)))
    assert cut and trimmed["truncated"] == 40 - len(trimmed["runs"])
    assert [r["n"] for r in trimmed["runs"]] == list(range(trimmed["truncated"], 40))
    assert len(json.dumps(trimmed)) <= 4096


def test_every_block_respects_its_budget_and_the_total() -> None:
    fetched = [
        block("runs.recent", listing(40)),
        block("records.query", {"records": [{"t": "y" * 400}] * 30}),
        block("listings.search", {"blob": "z" * 30000}),
    ]
    rendered = fit(fetched, DEFAULT_CONTEXT_BYTES)
    sizes = [len(b.encode()) for b in rendered]
    assert sizes[0] <= DEFAULT_BYTES["runs.recent"]
    assert sizes[1] <= DEFAULT_BYTES["records.query"]
    assert sum(sizes) <= DEFAULT_CONTEXT_BYTES
    # An installed operation without a list is replaced by a size note, not cut mid-way.
    assert '"omitted": true' in rendered[2]


def test_claims_and_inbox_come_first_and_are_never_dropped() -> None:
    claims = {"claims": [{"record_id": f"rec-{i}", "pad": "p" * 600} for i in range(20)]}
    inbox = {"messages": [{"m": "q" * 3000}], "truncated": 0}
    fetched = [
        block("records.query", {"records": [{"t": "y" * 400}] * 30}),
        block("records.claimed", claims),
        block("mailbox.inbox", inbox),
    ]
    rendered = fit(fetched, 2048)
    assert [re.search(r'operation="([^"]+)"', b).group(1) for b in rendered] == [  # type: ignore[union-attr]
        "mailbox.inbox",
        "records.claimed",
        "records.query",
    ]
    # The inbox shows exactly the mail its run leased.
    assert "q" * 3000 in rendered[0]
    # Claims keep at least how many there are; the rest gets only a size note.
    shown = json.loads(rendered[1].split(">", 1)[1].rsplit("</context>", 1)[0])
    assert shown["claims"] == [] and shown["truncated"] == 20
    assert '"omitted": true' in rendered[2]


def test_unavailable_sources_keep_their_block() -> None:
    failed = Outcome("authority_denied", reason="denied")
    fetched = [Fetched("records.query", None, None, lambda o: context_block("x", failed, o))]
    expected = '<context operation="x" status="unavailable" reason="denied"></context>'
    assert fit(fetched, 1024) == [expected]


def test_requests_take_eight_entries_and_a_total_budget() -> None:
    base = {
        "request_id": "r",
        "agent_id": "a1",
        "namespace": "test",
        "objective": "o",
        "instructions": "i",
        "operations": ["records.get"],
        "grants": list(GRANTS),
    }
    eight = [{"operation": "records.query", "arguments": {"collection": "items"}}] * 8
    request = AgentRunRequest.model_validate({**base, "context": eight})
    assert request.context_max_bytes == DEFAULT_CONTEXT_BYTES
    with pytest.raises(ValueError):
        AgentRunRequest.model_validate({**base, "context": [*eight, eight[0]]})
    with pytest.raises(ValueError):
        AgentRunRequest.model_validate({**base, "context_max_bytes": 65537})
    # Left at their defaults, the new fields do not change a request's digest.
    plain = AgentRunRequest.model_validate({**base, "context": eight[:1]})
    document_ = plain.model_dump(mode="json")
    assert "context_max_bytes" in document_
    explicit = AgentRunRequest.model_validate(
        {**base, "context": [{**eight[0], "max_bytes": 1024}], "context_max_bytes": 4096}
    )
    assert plain.digest("s") != explicit.digest("s")
    template = RunTemplate.model_validate(
        {
            "instructions": "i",
            "operations": ["records.get"],
            "context": eight,
            "context_max_bytes": 4096,
        }
    )
    assert len(template.context) == 8 and template.context_max_bytes == 4096


# Sources in a run


def blocks(request: SessionRequest) -> dict[str, tuple[dict[str, str], Any]]:
    """Each context block by operation: its tag attributes and its JSON."""
    found: dict[str, tuple[dict[str, str], Any]] = {}
    for tag, body in re.findall(r"<context ([^>]*)>(.*?)</context>", request.prompt, re.S):
        attributes = dict(re.findall(r'(\w+)="([^"]*)"', tag))
        found[attributes["operation"]] = (attributes, json.loads(body) if body else None)
    return found


async def done(client: ClientSession, request: SessionRequest) -> SessionResult:
    return SessionResult(text="Looked at my context. All quiet today.", is_error=False)


def agent_run(
    kernel: Kernel, request_id: str, context: list[dict[str, Any]], **changes: Any
) -> str:
    request = AgentRunRequest.model_validate(
        {
            "request_id": request_id,
            "agent_id": "a1",
            "namespace": "test",
            "objective": "Review",
            "instructions": "Review your context.",
            "operations": ["records.get"],
            "grants": list(GRANTS),
            "context": context,
            "attempt_timeout_seconds": 30,
            "max_attempts": 1,
            **changes,
        }
    )
    return kernel.submit(kernel.auth.validate("supervisor"), request).task.task_id


@pytest.fixture
def team(items: Kernel) -> tuple[Kernel, runs.FakeSession]:
    provision(items.database, document((*SUPERVISOR, "runs.recent")), items.operations)
    session = runs.FakeSession()
    items.session = session
    return items, session


def insert(kernel: Kernel, title: str) -> None:
    at = now().isoformat()
    with kernel.database.engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO records(namespace,collection,record_id,data_json,version,created_by,"
            "created_at,updated_at) VALUES ('test','items',?,?,1,'a2',?,?)",
            ("rec-" + title.ljust(32, "0")[:32], json.dumps({"title": title}), at, at),
        )


SUMMARY = {"operation": "records.summary", "arguments": {"collection": "items"}}
QUERY = {"operation": "records.query", "arguments": {"collection": "items"}}


def test_built_in_blocks_share_one_snapshot(team: tuple[Kernel, runs.FakeSession]) -> None:
    kernel, session = team
    created(kernel)
    written: list[str] = []

    def write_during_fetch(stage: str) -> None:
        # A record committed after the snapshot was taken, between the two context calls.
        if stage == "agent_run.call_admitted" and not written:
            written.append(stage)
            insert(kernel, "late")

    session.scripts = [done]
    task_id = agent_run(kernel, "snap", [SUMMARY, QUERY])
    kernel.fault = write_during_fetch
    assert AgentRunRuntime(kernel).execute(task_id) == "succeeded"
    kernel.fault = lambda stage: None
    found = blocks(session.requests[0])
    (summary_tag, summary), (query_tag, query) = found["records.summary"], found["records.query"]
    assert written and summary["total"] == 1 and len(query["records"]) == 1
    assert summary_tag["as_of"] == query_tag["as_of"]
    assert summary_tag["cursor"] == query_tag["cursor"]
    assert [a["title"] for a in summary["available"]] == ["first"]
    assert summary["by_state"] == {"null": 1}


def test_a_source_shows_only_what_the_caller_may_read(
    team: tuple[Kernel, runs.FakeSession],
) -> None:
    kernel, session = team
    created(kernel)
    session.scripts = [done]
    grants = [g for g in GRANTS if g != READ]
    task_id = agent_run(kernel, "no-read", [SUMMARY, QUERY], grants=grants)
    assert AgentRunRuntime(kernel).execute(task_id) == "succeeded"
    found = blocks(session.requests[0])
    assert found["records.summary"][0]["status"] == "unavailable"
    assert found["records.query"][0]["status"] == "unavailable"


def test_runs_recent_is_scoped_to_the_caller(team: tuple[Kernel, runs.FakeSession]) -> None:
    kernel, session = team
    created(kernel)  # an operation task of a1 that touched a record
    session.scripts = [done, done, done]
    assert AgentRunRuntime(kernel).execute(agent_run(kernel, "a1-first", [])) == "succeeded"
    other = AgentRunRequest.model_validate(
        {
            "request_id": "a2-run",
            "agent_id": "a2",
            "namespace": "test",
            "objective": "o",
            "instructions": "i",
            "operations": ["records.get"],
            "grants": list(GRANTS),
            "attempt_timeout_seconds": 30,
        }
    )
    a2 = kernel.submit(kernel.auth.validate("supervisor"), other).task.task_id
    assert AgentRunRuntime(kernel).execute(a2) == "succeeded"
    recent = [{"operation": "runs.recent", "arguments": {"limit": 5, "tail_bytes": 12}}]
    task_id = agent_run(kernel, "a1-second", recent)
    assert AgentRunRuntime(kernel).execute(task_id) == "succeeded"
    tag, shown = blocks(session.requests[-1])["runs.recent"]
    assert tag["status"] == "ok" and "as_of" in tag
    listed = shown["runs"]
    assert {r["task_id"] for r in listed}.isdisjoint({a2, task_id})
    assert [r["request_id"] for r in listed][-1] == "a1-first"
    first = listed[-1]
    assert first["outcome"] == "succeeded" and first["result_tail"] == "…quiet today."
    touched = [r for r in listed if r["records_touched"]]
    assert touched and touched[0]["records_touched"][0].startswith("items/rec-")


def test_runs_recent_needs_its_grant(team: tuple[Kernel, runs.FakeSession]) -> None:
    kernel, _ = team
    with pytest.raises(ValueError, match="Context operations must be granted"):
        agent_run(
            kernel,
            "no-grant",
            [{"operation": "runs.recent"}],
            grants=[g for g in GRANTS if g != "runs.recent"],
        )


def test_priority_sources_render_first(team: tuple[Kernel, runs.FakeSession]) -> None:
    kernel, session = team
    created(kernel)
    claimed = {"operation": "records.claimed", "arguments": {"collection": "items"}}
    session.scripts = [done]
    task_id = agent_run(kernel, "order", [QUERY, claimed], context_max_bytes=1024)
    assert AgentRunRuntime(kernel).execute(task_id) == "succeeded"
    prompt = session.requests[0].prompt
    assert prompt.index('operation="records.claimed"') < prompt.index('operation="records.query"')


def test_the_snapshot_is_explicit_and_bound_to_its_thread(items: Kernel) -> None:
    created(items)
    count = "SELECT COUNT(*) FROM records"
    with items.database.snapshot() as snapshot:
        connection = snapshot.connection()
        assert connection is not None
        assert connection.exec_driver_sql(count).scalar_one() == 1
        insert(items, "after")
        # Ordinary reads stay fresh while a snapshot is open; only `shared` uses it.
        with items.database.read() as fresh:
            assert fresh.exec_driver_sql(count).scalar_one() == 2
            assert shared(snapshot, fresh).exec_driver_sql(count).scalar_one() == 1
        # Another thread never gets the connection, so it falls back to a fresh read.
        elsewhere: list[object] = []
        worker = threading.Thread(target=lambda: elsewhere.append(snapshot.connection()))
        worker.start()
        worker.join()
        assert elsewhere == [None]
        assert asyncio.run(asyncio.to_thread(snapshot.connection)) is None
    # Closed, it is unusable everywhere.
    assert snapshot.connection() is None
