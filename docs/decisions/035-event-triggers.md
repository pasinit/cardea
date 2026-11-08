# ADR-035: Event Triggers

- **Date / owner / milestones:** 2026-11-08; runtime and API maintainers; extends ADR-024 (time triggers) and ADR-029 (supervisor schedules) with the "wait for events" part of the kernel contract.
- **Status:** selected; first slice implemented. Portable tests pass (macOS, Python 3.13). Not run: a delivery through the live daemon's TLS listener, any test on Debian, and interoperability with a third-party sender.
- **Requirement:** a task can be started by an authenticated inbound event, not only by the clock.
  - **Authentication.** Only a sender holding the trigger's secret can start its task. A replayed request is either outside the replay window or a duplicate. An unauthenticated request does nothing and records nothing.
  - **Matching.** A trigger matches events by source and type, plus simple attribute filters. An event that does not match does nothing.
  - **Exactly one admission per event.** One event starts at most one task per trigger, across sender retries, kernel restarts and crashes before or after the commit.
  - **Bounded data.** The event reaches the run as size-capped context that is clearly marked as untrusted data, never as instructions.
  - **Live ceiling.** Admission goes through the same checks as a time-trigger slot: the trigger's supervisor's live permissions, platform policy, budgets and capacity. A revoked or disabled supervisor starts nothing.
  - **Configuration.** Limits are host configuration, following the `max_task_request_bytes` pattern.
  - **Acceptance tests:** dedupe across restart, signature and authentication rejection, size caps, ceiling enforcement, filter matching.

## Candidates and primary evidence

Checked 2026-11-08 (macOS, Python 3.13).

| Candidate | Version, license, maintenance | Fit evidence |
|---|---|---|
| [CloudEvents 1.0.2](https://github.com/cloudevents/spec/blob/v1.0.2/cloudevents/spec.md) (CNCF graduated) and its [HTTP protocol binding](https://github.com/cloudevents/spec/blob/v1.0.2/cloudevents/bindings/http-protocol-binding.md) | Spec, Apache-2.0. Python SDK [`cloudevents==2.2.0`](https://pypi.org/project/cloudevents/2.2.0/) (2026-06-11, Apache-2.0, typed, depends on `deprecation` and `python-dateutil`, which was already locked) | **Envelope: fits.** Required `id`, `source`, `type`, `specversion`; optional `subject`, `time`, `datacontenttype`; extension attributes are lower-case alphanumerics. The spec says `source` + `id` is unique per distinct event, which is exactly the dedupe key needed. The SDK's `cloudevents.core.bindings.http.from_structured` parses structured mode and validates required attributes; tested here. **Binary mode does not fit this authentication:** its attributes travel in `ce-*` headers, which a body signature does not cover (below). |
| [CloudEvents HTTP Webhook spec 1.0.2](https://github.com/cloudevents/spec/blob/v1.0.2/cloudevents/http-webhook.md) | Spec | Delivery is an HTTP `POST`; 200/201/202/204 mean delivered. Its authorization section allows a bearer token or an access token in the query, and an `OPTIONS` abuse-protection handshake for the sender's side. **Partial fit:** the delivery semantics are followed (202 for every accepted delivery). Its bearer option is the existing identity model, discussed below. There is no body signature, so it does not on its own authenticate a sender without a Cardea identity. The `OPTIONS` handshake protects the sender from being used against arbitrary targets; it adds nothing to the kernel's protection and is not implemented. |
| [Standard Webhooks 1.0](https://github.com/standard-webhooks/standard-webhooks/blob/main/spec/standard-webhooks.md) | Spec, Apache-2.0. Python library [`standardwebhooks==1.1.0`](https://pypi.org/project/standardwebhooks/1.1.0/) (2026-07-21, MIT, no dependencies, typed) | **Fits.** HMAC-SHA256 over `webhook-id.webhook-timestamp.body`, base64, sent as `webhook-signature: v1,<sig>`. Several space-separated signatures are allowed, so secrets rotate without downtime. Secrets are `whsec_<base64>`. The spec recommends a timestamp tolerance against replay. The library verifies with a constant-time compare and a fixed 5-minute window. Tested against the spec's published example (secret `whsec_MfKQ…`, message `msg_p5jX…`, signature `v1,g0hM…`), not only against its own `sign`. **Gap:** the tolerance is not configurable, so Cardea additionally checks a host-configured tolerance of at most 300 s first. |
| [GitHub webhook signatures](https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries) | Vendor scheme | `X-Hub-Signature-256: sha256=<hex HMAC of the body>`, `X-GitHub-Delivery` (GUID), `X-GitHub-Event`. **Does not fit as the general mechanism:** it is vendor-specific, it has no signed timestamp (so replay is limited only by dedupe), and its body is not a CloudEvent. A GitHub sender would need a mapping of delivery to CloudEvent `id`, of event to `type`, and of repository URL to `source`. Deferred as an adapter. Not tested. |
| Existing identity model (SPIFFE JWT-SVID / OIDC bearer, ADR-010/025) | In use | **Fits for senders that already hold a Cardea identity.** But such a sender can already `POST /v0.2/agent-runs` under its own authority, so an event path adds only templating. Typical event senders (trackers, CI, SaaS webhooks) cannot obtain a SPIFFE or OIDC token from this deployment's providers. Deferred, not rejected. |
| DBOS 3.0.0 event features (pinned; source read in `.venv/.../dbos/`) | MIT | `DBOS.send`/`recv` and `set_event` are messaging *within or between* workflows that already exist; Cardea already uses them for control signals. `@DBOS.kafka_consumer` (`_kafka.py`) requires `confluent_kafka` and a Kafka broker. Enqueue `deduplication_id` (`_enqueue_options.py`) dedupes only while a workflow with that ID is still queued, not for the lifetime of the event. The `Debouncer` merges bursts. **No HTTP intake and no permanent per-event dedupe**, so DBOS stays the execution owner after admission, as for every task, and the event's identity is recorded in Cardea's own transaction. |
| Existing Cardea time triggers (ADR-024/029) | In tree | **Reused.** The same `triggers` table, provisioning and history. The same admission (`Kernel.admit_scheduled`, the shared body of `Kernel.submit`). The same identity-in-request-ID pattern: `<trigger>.<slot>` becomes `<trigger>.e<digest>`. The same use of the request ID as the idempotency context. |

## Decision

Compose them: **CloudEvents 1.0 structured JSON mode for the envelope, Standard Webhooks for authentication, and the ADR-024 trigger and admission path for everything after verification.** Custom code is limited to the matching rules, the delivery record and the glue.

1. **Endpoint.** `POST /v0.2/events/{trigger_id}`, served on the same listeners as the rest of the API (the Unix socket, and the TLS listener of ADR-025 for remote senders). It takes no bearer credential.
2. **Authentication: a per-trigger signing secret, not the identity model.** A sender must sign with Standard Webhooks under the trigger's secret.
   - The secret lives in a host-owner private file, named by the trigger's `event.secret_file`. It holds one `whsec_…` value per line, and any of them verifies (rotation).
   - The file is read at installation, which fails with `event_secret_unavailable` if it is missing, not private or malformed, and again at every delivery, so a rotation needs no restart.
   - The timestamp must be within `event_triggers.timestamp_tolerance_seconds` of the kernel clock.
   - The signature authenticates the sender. It confers no authority: the task runs under the trigger's supervisor, inside that supervisor's live ceiling.
   - Order of checks: body size (`413 event_too_large`), an active event trigger (`404 trigger_not_found`), the signature (`401 invalid_signature`), then parsing. Nothing is recorded before the signature verifies.
3. **Envelope.** CloudEvents 1.0 in structured JSON mode only (`Content-Type: application/cloudevents+json`), parsed with the CloudEvents SDK.
   - Binary mode (`ce-*` headers) is refused with `415 event_mode_unsupported`. Standard Webhooks signs only the body, so headers such as `ce-id` or `ce-type` could be changed on a captured request within the tolerance window. Structured mode keeps every attribute inside the signed body.
   - `data` must be JSON. `data_base64` and non-JSON `datacontenttype` are refused (`400 invalid_event`).
4. **Matching.** `event.source` must equal the event's `source` exactly, and its `type` must be one of `event.types`.
   - Up to 8 `filters` must all match. Each is one filter of the [CloudEvents Subscriptions API](https://github.com/cloudevents/spec/blob/main/subscriptions/spec.md) `exact`, `prefix` or `suffix` dialects on one context attribute, compared on its canonical string form. An absent attribute does not match.
   - A verified event that does not match returns `202` with `outcome: "ignored"` and starts nothing.
5. **One task per event.** The dedupe key is (trigger, CloudEvents `source`, `id`), the uniqueness the CloudEvents spec gives producers. One `BEGIN IMMEDIATE` transaction does four things:
   - it re-reads the trigger, which must be unchanged;
   - it looks up an earlier admission and, if one exists, returns its task as `outcome: "duplicate"`;
   - it admits through `Kernel.admit_scheduled`;
   - it appends an `admitted` row to `trigger_deliveries`.
   A partial unique index on admitted rows and the deterministic request ID `<trigger_id>.e<first 15 hex digits of SHA-256 over RFC 8785 [source, id]>` (at most 128 characters, so an `Identifier`) with `UNIQUE(supervisor_id, request_id)` are independent second guards. The request ID is also the run's `idempotency_context` (ADR-026), so effects of one event dedupe across attempts and retries.
   - A crash before the commit leaves nothing, and the sender's retry admits.
   - A crash after the commit leaves the delivery decided, and a retry returns the same task.
   - The `webhook-id` is not the dedupe key: a sender's retry may carry a new one.
6. **Refusals leave the event open.** If admission is refused, the transaction rolls back and the refusal is appended as a `refused` delivery with its code. Refusals include authority denied, a disabled principal, request too large, capacity and an invalid template. The response carries the same status and code as an HTTP submission. This differs from a time slot, which a refusal closes. A slot recurs; an event does not, and senders retry non-2xx deliveries, so a transient refusal (capacity, a missing grant the owner then restores) does not lose the event. Each retry is evaluated again, and only an admission is unique.
7. **The event as data.** Agent runs gain `trigger_event`: `id`, `source`, `type`, `subject`, `time` and `data`. The trigger sets it, and a template may not.
   - It is part of the bound request, so it is in the request digest and is checked by `max_task_request_bytes`.
   - The prompt adds a fixed preface stating that the event is untrusted data, not instructions. It then adds one `<event>…</event>` block with the event as JSON, with `<` and `>` escaped, so markup in the event cannot close the block. This is the same convention as ADR-031 context blocks.
8. **Size caps.** Three caps apply, in this order:
   - the listener's `max_request_bytes`;
   - `event_triggers.max_event_bytes` on the whole signed body, before signature verification;
   - `max_task_request_bytes` on the resulting request (template plus event), at admission.
9. **Ownership.** Event triggers are host-owned in this slice. They are part of the `Provisioning` document's `triggers`, with `event` set instead of `schedule`/`timezone`/`misfire`. `PUT /v0.2/triggers/{id}` refuses them with `403 event_trigger_host_owned`, because the secret is a host-owner file. A trigger's kind is immutable (`trigger_immutable`).
10. **Template kinds.** `agent-run` only in this slice, because that is the template that consumes event data.
11. **Inspection.** `GET /v0.2/triggers` lists each trigger's `kind` and, under the same `history` bound, its most recent `deliveries` (source, event ID, type, outcome, task, refusal code).

## Integration

- **State.** Migration `m6_004` (`m6_004_event_triggers.py`, `down_revision = "m6_003"`) adds:
  - `triggers.kind`, `time` (the default for existing rows) or `event`. The scheduler evaluates only `time` triggers.
  - `trigger_deliveries`, append-only (update and delete abort), with `UNIQUE(trigger_id, source, event_id) WHERE outcome='admitted'`.
  `SCHEMA_HEAD` is now `m6_004`. Existing time triggers keep their definition digest: the digest omits a null `event`.
- **Compatibility.** `TriggerSpec` gains an optional `event`, and `schedule`, `timezone` and `misfire` become optional; a time trigger still requires all three. Existing documents and requests are unchanged. `AgentRunRequest` gains an optional `trigger_event`.
- **Authority.** The endpoint holds no supervisor credential. It uses the scheduler's admission (`admit_scheduled`): the kernel's runtime identity, the supervisor taken from the host-owner-installed trigger, and the unchanged `_precheck` and `_admit` checks. The `task.bound` event carries `trigger_id`, `event_source` and `event_id`.
- **Snapshot restore.** As for time triggers, restored principals are disabled and namespaces sealed, so deliveries are refused until the host owner provisions again.
- **Exposure.** A remote sender needs the TLS listener. With `client_ca_file` (mutual TLS) set, the sender must also present a client certificate. Third-party senders usually cannot, so put a TLS-terminating proxy in front, or leave mTLS off for that listener.
- **Cost.** One delivery row per admitted or refused verified event. Rows are retained for the lifetime of the store, like trigger history.
- **Dependencies.** `cloudevents==2.2.0` (Apache-2.0, adds `deprecation==2.1.0`) and `standardwebhooks==1.1.0` (MIT), pinned.

### Configuration

```json
"event_triggers": {"max_event_bytes": 8192, "timestamp_tolerance_seconds": 300}
```

- `max_event_bytes`: 1024–262144, default 8192.
- `timestamp_tolerance_seconds`: 1–300, default 300. The 300 s ceiling is the library's fixed window.

## Verification

`apps/api/tests/service/test_event_triggers.py` has 18 portable tests and no real DBOS. They cover:
- admission of one agent run with the event as `trigger_event`, its request ID and idempotency context, and the prompt's data block and preface, through a real attempt;
- escaping of markup in event data;
- redelivery with a new `webhook-id` returning the same task, before and after reopening the store, with only one task bound;
- a crash before the commit leaving the event open, then one admission;
- rejection of a wrong secret, a stale or future timestamp, a tampered body, missing signature headers and a bearer credential in place of a signature, with nothing recorded;
- the Standard Webhooks published example, so verification is not checked only against the library's own signer;
- the configured tolerance;
- rotation with two secrets;
- refusal of binary mode, non-CloudEvents media, invalid envelopes and `data_base64`;
- 404 for unknown, disabled and time triggers;
- the body cap, at its default and when configured;
- the task-request cap refusing and recording, then admitting after the host owner raises it;
- a revoked grant (`403`) and a disabled supervisor (`principal_disabled`) refused and recorded, then admitted once authority is restored;
- source, type, `prefix` and `exact` filters, including an absent attribute;
- filter-dialect validation;
- definition validation;
- refusal of supervisor-managed event triggers;
- refusal of a missing or non-private secret file at install;
- the scheduler never ticking event triggers;
- deliveries in `GET /v0.2/triggers`.

`make lint`, `make typecheck` and `make test` pass (2026-11-08, macOS, Python 3.13).

## Deferred

- Supervisor-managed event triggers. These need a secret the supervisor can obtain without a host file, for example a kernel-generated secret returned once by `PUT`.
- A bearer-authenticated publish path for senders that hold a Cardea identity.
- CloudEvents binary mode. It needs a signature that covers headers, such as [HTTP Message Signatures, RFC 9421](https://www.rfc-editor.org/rfc/rfc9421).
- A GitHub signature adapter.
- `operation` and other templates, whose handlers would need the event in their context.
- The Subscriptions API `all`, `any`, `not` and `sql` dialects.
- Per-trigger or per-source rate limits.
- Retention of delivery rows.

## Revisit

Revisit if:
- Standard Webhooks or the library makes the tolerance configurable, or adds header coverage;
- senders need binary mode, or vendor schemes without a CloudEvents envelope;
- clients need to manage event triggers themselves;
- verified-sender traffic makes per-event refusal rows or unbounded delivery history too costly;
- DBOS gains durable HTTP intake with permanent dedupe.
