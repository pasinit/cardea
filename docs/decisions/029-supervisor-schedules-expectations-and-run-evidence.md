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
