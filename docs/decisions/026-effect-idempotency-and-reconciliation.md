# ADR-026: Effect Idempotency Scopes and Reconciliation

- **Date / owner / milestones:** 2026-10-15; runtime and broker maintainers; ADR-022 gap #2 (C1).
- **Status:** selected and implemented for installed in-process operations (ADR-023). Portable tests pass. No production provider reconciler is installed yet; Telos supplies its own.
- **Requirement:**
  1. An external effect, such as one email for one scheduled slot, happens at most once, even when a retried agent phrases the request differently.
  2. An interrupted effect is never dispatched again on assumption. It stays `outcome_unknown` until destination state shows whether it happened.
  3. Only then may a retry proceed (not applied) or be answered from the observed result (applied).

## Why the existing receipt is not enough

`OperationRequest.digest()` covers the full arguments, so two invocations that differ only in wording are different requests. The kernel's receipt dedupes an identical retry of the same `(task, operation_id)` and nothing else. A retried agent session, or a new task for the same slot, produces a new receipt. `reconcile_operation` exists in the store, but nothing calls it.

## Candidates and primary evidence

Checked 2026-10-15.

| Need | Candidate | Fit |
|---|---|---|
| Identify which argument values define "the same effect" | [RFC 6901 JSON Pointer](https://www.rfc-editor.org/rfc/rfc6901) via [`jsonpointer`](https://github.com/stefankoegl/python-json-pointer) 3.1.1, Modified BSD, released 2026-03-23 (newer 3.2.x releases are under two weeks old and were skipped) | Selected. A standard way to name fields in the JSON arguments that are already validated by JSON Schema. |
| Provider-side deduplication | HTTP `Idempotency-Key` header ([IETF draft](https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/)); provider idempotency keys (Stripe-style); RFC 5322 `Message-ID` for mail | Used where the provider supports it. Cardea passes a stable `effect_key` to the handler for this purpose, but cannot rely on it: SMTP relays generally do not deduplicate. |
| Durable execution engines' own idempotency | DBOS step idempotency (pinned 3.0.0), Temporal activity IDs | They make a workflow step run at most once *per workflow*. They do not tell whether a remote effect happened after a crash, and they do not dedupe across tasks or reworded calls. Cardea keeps DBOS for dispatch and adds only the scope table and reconciliation. |

## Decision

1. **Scope.** An effect operation may declare `idempotency_scope`, a list of RFC 6901 pointers into its arguments. The scope key is the SHA-256 of the RFC 8785 encoding of `{namespace, operation, context, values}`.
   - The context is the invocation's idempotency context: a caller-supplied identifier such as a trigger slot, defaulting to the task.
   - The key is recorded in the intent.
2. **Scope table** (`effect_scopes`, migration `m6_001`). It is written in the same transaction as the intent and each settlement. States are `running`, `succeeded`, `failed` and `unknown`. At admission:
   - **`succeeded`:** the new invocation gets its own receipt marked `deduplicated`, carrying the earlier output. The handler is not called.
   - **`running` or `unknown`:** refused with `effect_in_doubt`, because a second dispatch could repeat the effect.
   - **`failed` or absent:** the invocation claims the scope.
3. **Effect key.** The handler context carries `effect_key`: the scope key when declared, else the receipt ID. Handlers should derive provider idempotency keys and mail `Message-ID`s from it, so that a retry after "not applied" reuses the same identity.
   - The context also describes the caller (added 2026-10-20). `agent_id` is the agent principal of the agent run that made the call, or null outside agent runs. `idempotency_context` is the request's idempotency context; scheduled tasks get `<trigger_id>.<slot>`. `slot_time` is the scheduled slot as ISO 8601 on the trigger's wall clock, with its UTC offset, or null outside triggers.
   - Scopes are keyed by `idempotency_context` when it is set and by the task otherwise. Another task for the same slot therefore deduplicates against the first task's effect.
   - The receipt records these fields under `arguments.context`, so a reconciler receives the same context the handler saw.
4. **Reconciliation.** An effect operation may declare a `reconciler` entry point, which returns `Reconciliation(applied, output)` from observed destination state, for example a provider's events API looked up by `Message-ID`.
   - Cardea stores the observation as an evidence object.
   - It then calls the existing `reconcile_operation`:
     - `SUCCEEDED` with the verified output when the effect was applied;
     - `FAILED` with `effect_not_applied` when it was not.
   - The scope follows.
   - A reconciler error leaves the effect uncertain.
   - Triggers for reconciliation:
     - `POST /v0.2/tasks/{task}/operations/{op}/reconcile`, which requires live authority over the task and the operation;
     - a service loop every 30 s for operations whose descriptor names a reconciler.
5. **The task stays terminal.** A task failed with `reconciliation_required` remains failed. Reconciliation settles the effect and its scope, so the next invocation in that scope either dedupes or may proceed.


### Failure reasons (added 2026-10-20)

A failed call's receipt, the run view (`calls[].failure_details`) and the agent's error text explain *why* it failed. Each failure carries `details = {"reason", "summary"}`.

`reason` is one of:

| Reason | Meaning |
|---|---|
| `timeout` | no result within the operation's timeout |
| `queue_timeout` | no confined-worker slot within the profile's queue timeout (code `executor_busy`, retryable); nothing ran |
| `worker_exit` | the confined worker exited, was killed at a limit or was stopped without a result |
| `worker_refused` | the helper refused to start the worker, or it was stopped before starting; nothing ran |
| `upstream_http_error` | the handler failed on an HTTP error from a remote service |
| `invalid_output` | the result did not match the output schema or size limit |
| `rejected` | the operation refused the call itself (`OperationRejected`) |
| `denied` | authority changed before the result was recorded |
| `error` | any other handler exception |

`summary` is one printable line of at most 200 characters. It holds the message only when an `OperationRejected` was written for the agent. For any other exception it holds only the class name, plus the HTTP status for HTTP errors; never the exception text, stderr or a traceback, which may contain URLs, tokens or personal data. For confined workers it holds the systemd result and exit status. Uncertain effects record the same details next to `operation_outcome_unknown`, and failed context calls record them too (their prompt block carries the `reason` attribute; ADR-031). Codes are unchanged except for the new `executor_busy`; `message` keeps its fixed text.

## Integration

- **Authority.** Unchanged ceiling checks at admission, dispatch and settlement. Reconciliation needs live authority over the task and the operation, and it performs no effect.
- **Shared invocation core.** Admission, settlement and uncertainty marking live in `service/invocation.py` and run inside the caller's transaction and lease. Both the operation task and later multi-call runtimes (the agent step, ADR-022 gap #4) use them.
- **Schema head.** `SCHEMA_HEAD` in `service/database.py` replaces the scattered literal checks.

## Verification

Seven new tests in `apps/api/tests/service/test_operations.py`, plus updated schema-rewind tests:
- a reworded retry in the same scope is deduplicated;
- another context sends;
- an uncertain scope blocks retries;
- `not applied` permits a retry with the same effect key;
- `applied` deduplicates later attempts;
- an unavailable reconciler leaves the effect uncertain until the loop succeeds;
- the HTTP route requires task authority;
- read operations cannot declare scopes.

`make lint`, `make typecheck` and `make test` pass (345 tests).

## Revisit

Revisit if:
- a provider offers authoritative idempotency, so the reconciler becomes unnecessary for it;
- scopes need expiry, which they do not have today; a scope lives as long as the store;
- reconciliation needs human evidence rather than a programmatic observation.
