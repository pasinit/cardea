# ADR-032: Remove the Incident Agent-Task Path

- **Date / owner / milestones:** 2026-10-20; runtime maintainers; supersedes the agent-task parts of [ADR-016](016-m3-openrouter-and-native-mcp.md).
- **Status:** selected and implemented; portable tests pass.
- **Requirement:** one way to run an agent. An agent is an [ADR-027](027-agent-runs.md) agent run whose tools are installed operations ([ADR-023](023-installed-operation-registry.md)). The kernel holds no business-specific agent, tool or input format (AGENTS.md: "do not encode customer escalation, CRM, or team-specific evaluators as kernel concepts").

## Context

M3 (ADR-016) proved model invocation and native MCP with a fixed, incident-specific profile:
- `POST /v0.2/agent-tasks` with `AgentTaskRequest`, whose input was a batch of fictional incidents;
- `POST /v0.2/tasks/{id}/inputs` with `AgentInput`, for one additive input update;
- a fixed `cardea-incidents` MCP tool (`runtime/incident_tool.py`), with a matching fixed worker in the root helper;
- an OpenRouter model broker owned by the kernel (`runtime/openrouter.py`, `ModelRuntimeConfig`), with per-supervisor model budgets (`model_budgets`, `GET /v0.2/model-budget`).

ADR-027 then added agent runs, which cover the general case: any installed operation is a tool, each call is a receipted operation with idempotency scopes, reconciliation and live authority, sessions are bounded and retried from a call journal, triggers and expectations apply (ADR-024, ADR-029), and read-only context operations exist (ADR-031). Since then the M3 path has been a second, narrower agent mechanism with its own budgets, tables, API and CLI. No client uses it.

## Decision

1. **Removed from the kernel:**
   - **API:** `POST /v0.2/agent-tasks`, `POST /v0.2/tasks/{id}/inputs` (only agent tasks accepted input) and `GET /v0.2/model-budget`.
   - **Models:** `AgentTaskRequest`, `AgentInput`, `ModelRuntimeConfig` (`ServiceConfig.agent_runtime`), `ModelBudget` (`Provisioning.model_budgets`), the `agent` trigger template kind, and the rights `models.disclose`, `tools.incidents` and `inputs.append`. `models.invoke` stays: agent runs require it.
   - **Runtime:** the `incident-agent-v1` profile and its DBOS workflow, `agent_runtime.py`, `agent_budget.py`, the OpenRouter broker and `openrouter` dependency, the MCP tool adapter `mcp_tools.py`, `incident_tool.py` with its schema, the input-directory helper, and the helper's fixed `tools.incidents` worker.
   - **Client and CLI:** `submit_agent`, `append_input`, and the `submit-agent`, `append-input` and `ingest-agent-directory` commands. `submit-agent-run` (`KernelClient.submit_agent_run`) takes their place.
   - **Load harness:** the M3 load runner. Its comparison code is kept, because the M4 contract reuses it.
2. **Kept for history.** Migration `m3_001` and its tables (`agent_runs`, `agent_inputs`, `agent_effects`, `model_budget_limits`) remain, so existing stores migrate unchanged and old receipts stay readable:
   - `GET /v0.2/tasks/{id}/agent-receipts` still lists an old M3 task's receipts from `agent_effects`.
   - The host budget keeps counting historical M3 tool usage, so cumulative allocations are not silently refunded.
   - The M3 reports and evidence stay under `experiments/m3/` and `packages/cardea-load/reports/`, and are reproducible only at a commit before this change.
3. **Upgrade guard.** The kernel refuses to start (`retired_profile`, 503) while any unfinished `incident-agent-v1` task exists. Cancel such tasks with the previous release first.
4. **The incident example becomes a demo built on the general path.** `experiments/incident-demo/` installs `incidents.compile` as an ordinary read operation (a descriptor plus a handler) and runs it from an agent run with a scripted session over the real per-attempt MCP endpoint, including a crash and resume from the receipt journal (`make incident-demo-test`, run in CI).
5. **Recovery and persistence coverage moves to agent runs.** The M3 portable tests covered several properties. Agent-run tests already cover, or now cover:

   | M3 property | Agent-run test |
   |---|---|
   | committed progress resumes without replay | `test_crash_after_effect_resumes_without_resending` |
   | unknown intent never replayed | new `test_interrupted_effect_intent_is_never_replayed`: the process dies after an effect's intent commits; the call becomes `outcome_unknown`, the run fails `reconciliation_required`, the handler never ran, and another task in the same scope is refused `effect_in_doubt` |
   | an interrupted read does not block the work | new `test_interrupted_read_intent_fails_and_the_run_retries` |
   | revocation before dispatch | `test_revoked_authority_refuses_tool_calls`, `test_revoked_context_authority_stops_the_run` |
   | cancellation | `test_cancellation_stops_the_session` |
   | the task receipt is the latest operation | `test_agent_receipts_list_the_run_calls` |

   A new fault point, `agent_run.call_admitted`, lies between a call's committed intent and its handler. Model-budget tests were removed with model budgets. Agent runs report cost from the session and cap it per run (`max_cost_usd`), as ADR-027 records.

## Consequences

- **Smaller, single-path kernel.** The M3-only code, tables in active use and a dependency are gone.
- **No kernel-owned model budget.** Per-supervisor model accounting, if needed, belongs with agent runs (ADR-027 open item), not with the removed broker.
- **Breaking API change.** It affects only clients of the removed endpoints and CLI commands; there are none in this repository's deployments. Provisioning documents that still carry `model_budgets` or the removed rights are refused, and must drop them.
- **Helper hash change.** The helper's source changes, because its `tools.incidents` worker is gone. Hosts must reinstall the helper from the same commit.

## Verification

`make lint`, `make typecheck`, `make test` (including `incident-demo-test`), `make load-check` and the experiment checks pass.
