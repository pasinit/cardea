# ADR-027: Agent Runs with Cardea-Owned Tools

- **Date / owner / milestones:** 2026-10-15; runtime and broker maintainers; ADR-022 gaps #4 and #5 (C3).
- **Status:** selected and implemented for portable execution. A live Claude session and Debian confinement of the session process are not yet run. Confinement is ADR-022 gap #6.
- **Requirement:** an agent session runs as one bounded step of a Cardea task.
  - Every tool it can use is an installed operation served by Cardea, so each call has a durable intent, a live authority check, a receipt and an idempotency scope.
  - A session that outlives its attempt cannot act.
  - A retried session continues from the recorded calls instead of repeating effects.

## Candidates and primary evidence

Checked 2026-10-15.

| Need | Candidate | Fit |
|---|---|---|
| Agent runtime | [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python), MIT. Pinned `claude-agent-sdk==0.2.163`, released 2026-09-30; newer releases are under two weeks old. | Selected as the first session adapter, at the project owner's direction to treat a session as one unit. Its options can remove every built-in tool (`tools=[]`), ignore host settings and MCP configuration (`setting_sources=[]`, `strict_mcp_config`), and cap turns and cost (`max_turns`, `max_budget_usd`). Its loop runs inside the Claude Code CLI and cannot be checkpointed per model call, which is why the kernel treats the session as opaque and owns only the tools. |
| Tool transport | MCP Streamable HTTP via the pinned `mcp==2.2.0` low-level `Server` | Selected. Claude Code connects to HTTP MCP servers natively. One loopback server runs per attempt, with a random bearer token, in stateless JSON-response mode. No custom RPC. |
| Kernel-owned agent loop (Anthropic Messages API inside the M3 broker; Pydantic AI with DBOS) | Documented in ADR-022 | Not chosen now. It would checkpoint every model call, but needs Cardea to own the agent loop, which the owner decided against. It stays the fallback if the SDK cannot be confined. |

## Decision

1. **Request.** `AgentRunRequest` (`POST /v0.2/agent-runs`, profile `agent-run-v1`, its own DBOS queue sized by `concurrent_sessions`) carries:
   - instructions;
   - the installed operations it exposes, each of which must be granted together with `models.invoke`;
   - an optional `idempotency_context`, such as a trigger slot;
   - bounds: attempts (at most 3), attempt timeout, turns, tool calls and cost.
2. **Attempts.** Each attempt first records its number in a checkpoint, then takes a fresh lease covering the session budget plus settlement time. The checkpoint is written through RUNNING → SUSPENDED → QUEUED → RUNNING. This means a crash cannot reuse an attempt number, and kernel recovery always finds a checkpoint.
   - A failed session with every call settled is suspended and retried while attempts remain.
   - Any `running` or `outcome_unknown` call fences the run. It waits for the lease to expire, and kernel recovery then fails it with `reconciliation_required`, after which ADR-026 reconciliation applies.
3. **Tool endpoint.** It is served on 127.0.0.1 with a per-attempt bearer token and lists only the exposed operations. Operation names map to tool names by turning `.` into `-`, because model APIs reject dots and operation names cannot contain `-`. Before each call, Cardea checks that:
   - the task is RUNNING under *this attempt's* lease, which is unexpired;
   - the full live ceiling holds;
   - the tool-call budget is not exhausted.

   The call then goes through the shared invocation core (ADR-026) as operation `call-NNNN`:
   - a deduplicated call returns the earlier result, marked as already done;
   - an uncertain effect is reported to the agent as `outcome_unknown`, with an instruction not to retry.
4. **Retry journal.** A later attempt's prompt lists the earlier calls with their arguments and final states, taken from the receipts.
5. **Completion.** A run succeeds only when the session reports success, it has non-empty text, and every call is settled. Its output records the text, attempts, turns, cost and calls; the evidence records that all calls are settled. `GET /v0.2/tasks/{id}/agent-receipts` returns the full receipt of every call. Destination checks specific to a workload come with workload definitions (ADR-022 C5).
6. **Session adapter.** `ClaudeAgentSession` runs with:
   - no built-in tools;
   - only `mcp__cardea__*` tools allowed;
   - strict MCP configuration and no host settings;
   - a private temporary HOME and working directory;
   - an environment holding only one credential, from a private file chosen by the host owner (an API key or a subscription OAuth token).
7. **Kernel change.** `recover_task` now settles an interrupted `READ_ONLY` operation as FAILED (`worker_lost`) rather than `outcome_unknown`. A read-only operation declares that it has no external effect, so it needs no reconciliation.

## Limits and open items

- **Not confined yet.** The session process is not confined beyond its tool configuration, environment and private directory. Network egress limits (model API only) and process confinement need the configurable worker profiles of ADR-022 gap #6. [ADR-028](028-configurable-worker-profiles.md) adds an optional confined session (`confined_profile`/`confined_worker`), not yet verified on Debian. Unconfined, the Agent SDK merges the service's environment into the CLI's. The adapter therefore blanks every inherited variable except `PATH`, locale, timezone and certificate paths, so item 6 also holds unconfined. This is tested in `test_claude_session_has_no_builtin_tools_or_host_settings`.
- **Not exercised live.** No live Claude session has run against the endpoint. The portable tests use a scripted MCP client over the real HTTP endpoint.
- **Cost is reported, not reserved.** Cost comes from the SDK's result; there is no Cardea budget account for it yet.
- **Context before the session.** Read-only context operations that run before each session (`ctx-NN`) are described in [ADR-031](031-agent-run-context-operations.md).

## Verification

14 tests in `apps/api/tests/service/test_agent_runs.py` run a scripted session against the real per-attempt MCP endpoint. They cover:
- admission grants, the session requirement and installed operations;
- receipted calls;
- the bearer token;
- the tool budget;
- a retry with a journal and no resend;
- bounded attempts;
- an uncertain effect fencing the run;
- a crash after an effect resuming without resend;
- orphan refusal;
- cancellation;
- revocation;
- the adapter's option set.

`make lint`, `make typecheck` and `make test` pass (359 API tests).
