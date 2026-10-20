# ADR-029: Supervisor Schedules, Run Expectations and Run Evidence

- **Date / owner / milestones:** 2026-10-15; runtime and API maintainers; ADR-022 gaps #7, #8 and #11 (C4, C5).
- **Status:** selected and implemented; portable tests pass.
- **Requirement:** a client application (Telos) manages its users' recurring work without host-owner access: create, update, disable and remove a schedule under the user's own live authority.
  - A scheduled run is a versioned definition: trigger, instructions, exposed operations, bounds and completion conditions. It is checked when installed and again at every slot.
  - A run is complete only when its declared effects actually happened.
  - The client can read what a run did.

## Decision

1. **Workload definition = trigger + agent-run template.** ADR-024 triggers gain template kind `agent-run` (an ADR-027 `AgentRunRequest` without request or idempotency IDs). The slot identity `<trigger>.<slot>` becomes both the request ID and the idempotency context, so effects within one slot dedupe across attempts and retries (ADR-026).
   - Versioning is the trigger's definition digest. A changed definition applies to future slots only; admitted tasks keep their bound request.
   - No new definition store or process DSL is introduced. ADR-021's process-engine question stays open for company processes; this recurring workload does not need one.
2. **Supervisor-managed triggers.**
   - `PUT /v0.2/triggers/{id}` installs or updates a trigger, and `DELETE` removes it. The caller must be the trigger's supervisor and hold the new kernel right `tasks.schedule` (supervisor and platform) in the namespace.
   - The template is admitted against the caller's live ceiling at install time, and full admission runs again at every slot, so later revocation stops the schedule.
   - A `managed_by` column (migration `m6_003`) separates host-owned triggers, which provisioning replaces as a set, from supervisor-owned ones, which provisioning never touches. Neither owner can modify the other's trigger.
3. **Expectations (C5).** `AgentRunRequest.expect` lists conditions of the form "N to M successful calls of operation X".
   - The prompt states them.
   - A session that ends with an unmet expectation is retried while attempts remain, then fails with `expectation_unmet`.
   - Exceeding a maximum fails at once with `expectation_exceeded`, since an effect cannot be undone.
   - Successful calls include deduplicated ones (the effect happened once, earlier in the scope) and reconciled ones.
   - Destination evidence comes from the receipts and, for uncertain effects, from ADR-026 reconciliation.
4. **Run evidence.** `GET /v0.2/tasks/{id}/run` returns, under `tasks.inspect` on the task:
   - the state;
   - the attempt;
   - the output (text, turns, cost, calls);
   - the verification evidence;
   - every call with its arguments, result, failure and reconciliation flag.

   `GET /v0.2/triggers` already lists slot history with task IDs.

## Bounded run evidence (added 2026-10-20)

Every API response is capped at 256 KiB. A realistic agent run broke that: `GET /run` and `GET /agent-receipts` embedded every call's full arguments and result, so tens of calls returned `413 response_too_large`. The rules are now:

- **Receipts are bounded.** A receipt's arguments and result are each a kernel document of at most 64 KiB (the v0.1 contract).
  - Admission now refuses arguments that would exceed it, with `413 arguments_too_large`.
  - Settlement treats an output that cannot be recorded as `verification_failed` (reason `invalid_output`), whatever the descriptor's `max_output_bytes` says.
  - Before, both raised inside the call: an agent saw an MCP protocol error, and the call stayed `running`, which fenced the run.
  - Descriptors may not declare `max_output_bytes` above 65,536 (refused at load, ADR-023), so no installed operation can produce an output its receipt cannot hold.
  - One receipt therefore always fits a response: `GET /v0.2/tasks/{id}/operations/{operation_id}` returns it whole.
- **The run view summarizes calls and pages them.** Each call carries its receipt ID, timestamps, failure code and details, `deduplicated`, `reconciled`, and for arguments and result: size, SHA-256 of the canonical (RFC 8785) JSON, and a preview of its first `run_preview_bytes`. That is host configuration, 0–16384, default 2048. The full `arguments` and `result` stay inline only when they fit that preview size.
  - Calls are listed in the order made, one page at a time. A page holds as many calls as fit the budget, up to `limit`. `calls_next` is the cursor for `?after=`, and `calls_total` counts all calls.
  - The run's output and verification documents get a 64 KiB budget. An agent run's output keeps its text, trimmed if needed, and drops its `calls` list; anything else over the budget is null. `output_truncated` and `verification_truncated` say so.
- **`GET /agent-receipts` pages by size** (`?after=&limit=`, `next`), with whole receipts in operation-ID order.
- **`GET /output`** returns the document inline when it fits. Otherwise it returns `content: null` with `content_omitted`, `content_bytes` and `content_sha256`, and `GET /v0.2/tasks/{id}/output/content?offset=` serves the stored bytes as base64 chunks of 128 KiB.
- **Every page is built against a 240 KiB budget** under the 256 KiB cap. A page always includes at least one item; one call summary or receipt is far below the budget. `test_large_runs_stay_under_the_response_cap_and_page_everything` checks this: 60 calls with about 64 KiB of arguments and result each, plus an over-large result and a 300 KB reply. Every response stays under the cap, and paging returns every call and receipt plus the whole output.

## Candidates considered

- **Process definition languages** (Open Workflow Specification, BPMN via Flowable; ADR-021): unnecessary for a single-step recurring agent run with declared effects, and they would add a second owner of execution. Revisit for multi-step company processes.
- **Keeping triggers host-owned only:** this would require host-owner provisioning for every user routine change, and replace-all provisioning races with user edits. Rejected for client-managed work.

## Verification

11 tests in `apps/api/tests/service/test_workloads.py` cover:
- slot admission of an agent run with the slot as idempotency context;
- the schedule right;
- supervisor identity;
- the live-ceiling check at install;
- provisioning leaving supervisor triggers in place, and exclusive ownership;
- removal;
- expectation validation;
- retry until met, failure when unmet, failure when exceeded;
- the run view and its authorization.

`make lint`, `make typecheck` and `make test` pass (404 API tests).

## Revisit

Revisit if:
- a workload needs several dependent steps, which would mean evaluating a process engine;
- expectations need content checks beyond call counts, in which case add workload verifiers as installed operations;
- clients need per-user quotas on schedules.
