# Operating the single Debian host

M5 supports the fixed Debian profile in [packaging](../packaging/README.md), using SQLite, Alembic, DBOS and systemd. Installation does not grant authority. The daemon runs as `cardea-kernel`; confined jobs use `cardea-worker` on the same machine. Use the authenticated client for tasks and the explicit host-owner commands below for maintenance. See [ADR-019](decisions/019-m5-operational-recovery.md) and the [acceptance report](../experiments/m5/M5-REPORT.md) for evidence and limits.

## Configuration and enrollment

Keep `/etc/cardea/config.json` privately owned by `cardea-kernel`, mode `0600`, inside root-owned `/etc/cardea`. Keep `data_dir` at `/var/lib/cardea`, mode `0700`. Provision SPIRE/OIDC identities separately; provider enrollment and model keys are neither package contents nor backup contents. Renew an expired host enrollment using your identity provider's procedure. The service fails closed when it cannot authenticate its runtime.

Review a private provisioning document for current supervisor, agent and runtime identities, scoped permissions and budgets. Run `cardea provision` as the kernel account. Provisioning replaces current policy; it cannot change immutable identity/task provenance or bypass the live supervisor/ancestor/platform ceiling.

## Confined worker profiles

Installed operations and agent sessions may run as confined workers under profiles that the host owner defines in `/etc/cardea-host-workers.json` (root-owned, not group- or world-writable). See [ADR-028](decisions/028-configurable-worker-profiles.md) for the format and limits. Networked profiles reach only their loopback egress proxy, which must enforce the same hostname list as the profile. Keep session credentials in root-only files under `/etc/cardea/credentials/`. After changing the file, restart `cardea-kernel`. The package installs the matching `/usr/libexec/cardea-host`; source-based acceptance hosts must rerun their helper installer, or the source-hash probe refuses to start. Debian enforcement of these profiles is not yet verified.

## Restart and reboot

Use `systemctl restart cardea-kernel` or reboot the host. DBOS recovers the **current** shared database. Durable waits remain waiting and can receive authenticated input/resume. A host intent interrupted before receipt settlement becomes `outcome_unknown`, even if its process might have finished. It is never automatically retried or called successful. Preserve `/var/lib/cardea-host`, including reconciliation tombstones; do not clear it to force a retry.

## Upgrade

The verified compatibility path is package `0.1.0-1` / schema `m4_001` to `0.1.0-2` / schema `m5_001`, keeping DBOS `3.0.0` and workflow version `cardea-m1-v1`. Later service schemas, up to the current head `m18_env_runs`, have portable migration tests only; no package upgrade across them has run on Debian (see the [Debian acceptance checklist](acceptance/debian-adr-034-037.md)). Arbitrary future dependencies, runtime versions and host versions require another fit test.

1. Arrange a maintenance window, stop admissions and reconcile outstanding work. Stop with `systemctl stop cardea-kernel`.
2. As `cardea-kernel`, run `cardea backup /private/path/pre-upgrade.sqlite3 --config /etc/cardea/config.json`. The destination must not already exist.
3. Install the reviewed package. Package replacement stops the daemon and leaves it stopped; scripts neither migrate nor provision.
4. Run `cardea init --config /etc/cardea/config.json` as `cardea-kernel`. It checks compatibility, preserves a pre-migration snapshot and applies Alembic transactionally. Repeating `init` on the current schema changes nothing and makes no redundant backup.
5. Run `cardea verify --config /etc/cardea/config.json`, then start the service and inspect `cardea diagnostics` plus authenticated `/v0.2/system` and a permitted test task.

A failed migration rolls back. An unsupported schema/runtime refuses startup. Do not downgrade the database: an older package refuses an M5 store. Reinstall the supported package to recover from accidental binary downgrade; existing receipts remain unchanged.

## Backup and snapshot restore

A stopped-service backup uses SQLite's backup API and contains metadata, objects, journal, policies, budgets and DBOS state consistently. It is a private standalone SQLite file, independent of WAL sidecars. It contains sensitive workload and authority history: retain it privately, use your storage provider's encryption/access controls, and transfer it through an authenticated private channel.

An old snapshot cannot know about later revocations, spending or effects. Restore therefore seals historical execution rather than resuming it:

1. Stop the service and confirm the old executor is stopped. Retain the host reconciliation directory; never roll it back with a database snapshot.
2. Run `cardea restore /private/path/snapshot.sqlite3 --config /etc/cardea/config.json` as `cardea-kernel`. It rejects unsafe files, sidecar-dependent copies, unsupported versions, broken artifacts/references, clock regression and still-live copied worker leases. A lease rejection requires waiting for its real expiry.
3. Restore validates and migrates private staging before publication. It keeps the original DB/WAL/SHM in a private `before-restore-<uuid>/` forensic directory, even if corrupt. A durable `.restore-in-progress` marker blocks opening interrupted publication; rerun the restore to finish. **Do not delete the marker to force startup.** Validation failure before publication leaves the destination unchanged.
4. Completed receipts/results remain unchanged. Unfinished tasks are failed; running operations become `outcome_unknown`. All restored principals are disabled and all restored policies removed. Provision current identities and reviewed authority explicitly before starting the daemon.
5. The command, diagnostics and authenticated `/v0.2/system` expose `new_namespace_prefix`, such as `r<32 hexadecimal characters>.`. New effectful work must use that prefix and newly provisioned scoped budgets. Old namespaces are permanently sealed for execution, including namespace names absent from the snapshot. Historical reads still require current supervisor/ancestor/platform rights. Re-provisioning old rights or raising old budgets cannot reopen them.

Every subsequent restore creates another prefix. Stable request IDs can still retrieve a historical task under read authority; this does not dispatch it. Restoring after total-host loss cannot establish whether external effects occurred: use external reconciliation evidence. No snapshot rollback undoes an external effect.

## Diagnostics and verification

`cardea diagnostics --config /etc/cardea/config.json` may run online. Its JSON contains a fixed allowlist of dependency versions, configured-profile booleans, task-state counts, local checks and the recovery prefix. Errors use bounded codes; raw configuration, identities, paths, payloads, provider errors and journals are excluded. `ready` means the local schema/database, service lock and configured host probe passed. It is not proof of provider health, supervisor authority or successful task completion; check those through authenticated operations and receipts.

`cardea verify --config /etc/cardea/config.json` requires the service to be stopped. It checks supported schema/runtime, SQLite integrity/foreign keys, task and receipt snapshots/indexes, all object digests and installed-profile artifact references. Verification detects inconsistency; it does not authenticate a backup against a malicious host administrator. Host-root compromise remains outside the trust boundary.

Use local journald for operational investigation. Raw logs are not a redacted support bundle. The host owner controls journald limits using its standard configuration; Cardea does not change global host log retention.

### Agent-run attempt logs

The daemon writes one JSON object per line to stderr, which journald stores under `cardea-kernel`. `log_level` in the service configuration sets the level (default `INFO`); the `CARDEA_LOG_LEVEL` environment variable overrides it. Every agent-run attempt logs, with its `task_id` and `attempt`:

- `attempt started` (harness, model, lease expiry, attempt timeout) and `attempt finished` (outcome, failure code, duration, turns and tool-call counts);
- `harness process started` / `harness process exited` (pid, argument vector, return code, signal, duration) for process harnesses, and `confined session starting` with the worker's `unit` for confined ones;
- `model request` / `model turn` / `model result` as the harness reports them (token counts, stop reason, provider error status, tool names), and `sdk system message: init` with the MCP server status;
- `tool call started` / `tool call finished` (operation, `operation_id`, duration, outcome code and reason), plus refused (`tool call refused`, with its code, for example `tool_busy`) and deduplicated calls;
- `harness stderr: ...`: the harness's stderr line by line;
- `attempt still running`, every `attempt_logging.heartbeat_seconds`, with `since_model_s`, `since_tool_s`, `since_output_s` (absent means never in this attempt) and `tools_in_flight`. A stall shows here first. A growing `since_model_s` with no tool in flight means the harness is waiting on the model API. A tool in flight with a growing `since_tool_s` means a tool is hung. No heartbeat at all means the kernel itself is stuck.
- lease expiry, worker reaping and recovery of an interrupted attempt, at `WARNING`.

Logs never contain tool arguments or outputs, prompts, completions, credential files, provider keys or the per-attempt endpoint token. Harness stderr is the one free-text source. It is truncated to `output_line_bytes` per line and `output_max_bytes` per attempt (later lines are counted, not logged), and scrubbed of registered secrets, bearer tokens and key-like assignments. A confined session's harness runs inside its worker, whose stderr shares the result file, so its turns and stderr are not visible in the kernel log yet. Use its tool calls and the worker unit name.

```sh
journalctl -u cardea-kernel -o cat --since -2h | jq -c 'select(.task_id == "task-...")'
journalctl -u cardea-kernel -o cat -f | jq -c 'select(.msg == "attempt still running")'
```

With `attempt_logging.files` set, each attempt's records are also written, with timestamps, to `<data_dir>/attempt-logs/<task_id>-<attempt>.log` (directory `0700`, files `0600`). Only the newest `files_retained` files are kept. The files receive the same records at the same level as the service log.

```json
"log_level": "INFO",
"attempt_logging": {"heartbeat_seconds": 60, "output_line_bytes": 2048, "output_max_bytes": 262144, "files": false, "files_retained": 200}
```

### Agent-run tool calls

The harness's MCP client must wait for tool calls that queue for a confined-worker slot. If it gives up sooner, the model is told the call timed out while Cardea still runs it. `agent_tools` in the service configuration controls this (ADR-027, revised 2026-10-26):

- `tool_timeout_seconds` (1–86400, default unset): how long the harness waits for one tool call. Unset, each attempt derives it from the slowest exposed operation's timeout plus its profile's `queue_timeout_seconds`, plus 10 s, never longer than the attempt. It reaches Claude Code as the MCP server's `timeout` and `MCP_TOOL_TIMEOUT` (milliseconds), and OpenCode as the remote MCP server's `timeout`.
- `max_concurrent_tool_calls` (1–64, default 4): tool calls of one attempt that may be in flight at once. Another call is refused before admission with the retryable `tool_busy` error and leaves no receipt. The system prompt states this limit. Size it to the host: a heavy profile with `concurrency` 2 gains nothing from many more queued calls.

A call still waiting for a slot when its client disconnects or cancels it is withdrawn, settling with code `call_abandoned`. A call already running finishes and settles. `harness session starting` logs `tool_timeout_s` and `max_concurrent_tool_calls`; `tool call refused` with `code=tool_busy` and the `tool_calls_refused` count in `attempt still running` and `attempt finished` show refusals.

```json
"agent_tools": {"tool_timeout_seconds": null, "max_concurrent_tool_calls": 4}
```

## Event triggers

An event trigger starts its agent-run template once per signed inbound CloudEvent ([ADR-035](decisions/035-event-triggers.md); worked example in [getting started](getting-started.md#3b-an-agent-run-started-by-a-signed-event)). Install it through `cardea provision`, never over the API.

- **Secret.** Create its secret file as the kernel account, mode `0600`, holding one `whsec_<base64>` per line: `printf 'whsec_%s\n' "$(head -c 32 /dev/urandom | base64)" > /etc/cardea/event-secrets/<trigger>`. Give the same value to the sender. To rotate, add the new line, switch the sender, then remove the old line. The file is reread at every delivery, so no restart is needed. Provisioning fails with `event_secret_unavailable` if the file is missing, not private or malformed. If the file breaks later, deliveries get `401 invalid_signature`, the same answer as for an unknown trigger, so senders cannot probe which triggers exist. The log line `event refused` carries the real code.
- **Exposure.** Remote senders post to `https://<listener>/v0.2/events/<trigger>` on the TLS listener. With `client_ca_file` set, a sender must present a client certificate, which most webhook senders cannot. Terminate their TLS at a proxy in front of the listener instead. The endpoint takes no bearer credential: the signature authenticates the sender, and the trigger's supervisor's live ceiling bounds what the task may do.
- **Limits.** `event_triggers.max_event_bytes` (1024–262144, default 8192) caps the whole signed body. `timestamp_tolerance_seconds` (1–300, default 300) bounds clock skew and replay. The listener's `max_request_bytes` and `max_task_request_bytes` still apply. The resulting request, template plus event, must fit `max_task_request_bytes`.
- **Inspection.** `GET /v0.2/triggers` shows recent `deliveries`. A `refused` delivery carries the admission code and leaves the event open for the sender's retry. The log line `event admission refused` carries the same code.

```json
"event_triggers": {"max_event_bytes": 8192, "timestamp_tolerance_seconds": 300}
```

## Collections and records

Collections are shared, versioned records with a JSON Schema ([ADR-034](decisions/034-agent-groups-and-shared-records.md), slice 1; worked example in [getting started](getting-started.md#8-a-shared-collection-of-records)). Migration `m10_records` adds the `collections`, `records` and `record_events` tables.

- **Who manages them.** A supervisor applies its own collections with `PUT /v0.2/collections/{name}`. It needs the right `groups.manage` in the namespace, with a matching `platform` row, as for every right. The host owner grants that right by provisioning; there is no host-owner collection file.
- **Grants.** The built-in operations `records.create`, `records.get`, `records.query` and `records.update` are always installed. They are authorized by resource-qualified grants `record:<collection>/<action>` (`create`, `read`, `update`), provisioned like any other right (`"operation": "record:work/update"`, with `delegate` for supervisors). Revoking one stops the next call of every task that relies on it.
- **Limits.** Record data is at most `max_record_bytes` (64–49152, default 16384) of canonical JSON, with at most 16 labels and 32 links. A query page holds at most 100 records and 56 KiB. Guards are CEL with ADR-036's limits: 512 characters, no comprehension macros, a fixed function list, failing closed.
- **Recovery.** A create or update interrupted by a crash becomes `outcome_unknown`. The 30 s reconciliation loop settles it from `record_events`, which records the receipt of every change: the change happened exactly when its history row exists.
- **Claims.** A collection's `claims` (`lease_seconds` 300–2592000, default 14400; `max_per_holder`; `max_followups`; `standalone`; `fence_writes`, default true, which lets only the holder with its current epoch write a held record) enable `records.claim`, `records.release` and `records.claimed` under the grant `record:<name>/claim` (and `read` for `records.claimed`). The trigger tick releases lapsed claims and admits follow-up runs for idle group members; each release appends a `claim_expired` history row and a journal event `record.claim_expired` whose `reason` is `lease_expired` or `followups_exhausted`. A claim that lapses after its follow-ups also sends the collection's supervisor a message from the kernel runtime. Applying a group, or a collection a group uses, is refused with `claim_lease_too_short` when the lease is below 2 x attempts x attempt timeout + 240 s for a pool granted `claim`.
- **State machines.** A collection's `states`, `initial`, `terminal` and `transitions` drive `records.transition`, authorized by `record:<name>/<transition>`. Migration `m13_state_machines` adds the columns it needs. Removing a state that records occupy is refused with `409 state_occupied` unless the spec maps it with `migrate`; the PUT moves those records and records `migrated` history (preview it with `?dry_run=true`). A transition with `approval` waits for a principal holding the `approve` mode on `record:<name>/<transition>` (with a matching `platform` row); `approve` on `records.transition` itself does not decide it. Transitions, claims, expiry and migrations append `cardea.record.*` journal events.
- **Draining and deletion.** Lowering a pool's `replicas`, removing or renaming a pool, or `DELETE /v0.2/groups/{name}` drains members on the minute tick (migration `m14_group_drain`): no new slots, claims or pool mail; the current run may finish until the pool's `drain_timeout_seconds` and is then cancelled; then claims are released, mail is re-addressed to the pool (or dead-lettered with notices), triggers are removed and the principal is disabled. `status.observedGeneration` (and `status.state` for a deleted group) shows when it converged. `DELETE /v0.2/collections/{name}` is refused while a group lists the collection and otherwise leaves a tombstone that keeps records and history; the name cannot be reused.
- **History.** `record_events` is append-only (SQLite triggers refuse `UPDATE` and `DELETE`), and records are never deleted. `GET /v0.2/collections/{name}/records/{record_id}/events` pages it for the supervisor and for holders of `record:<name>/read`.

## Group spend and windowed expectations

A group and each of its pools may set `spend: {usd, window: day|week, timezone}` ([ADR-034](decisions/034-agent-groups-and-shared-records.md), slice 9). Migration `m15_spend` adds `run_attempt_costs`, `spend_reservations` and `expect_window_reports`.

- **Admission.** A member run reserves its `max_cost_usd` against the current window of both accounts, and is refused with `429 budget_exhausted` when either would exceed its ceiling. A refused trigger slot or journal event is recorded as refused.
- **Settlement.** When the run has ended, its reservation settles to the cost its attempts reported, and the unused part is released. A run with an attempt of unknown cost (a lost attempt, or a harness that does not meter) keeps its full reservation (state `unknown`). A retry or an approval continuation does not start once the run's group or pool account is at or over its ceiling for the window it was charged to; the run then fails with `budget_exhausted` and the kernel mails the supervisor. Spend is metered, not a provider bill.
- **Inspection.** `GET /v0.2/tasks/<id>/run` shows `spend`: each attempt's reported cost, the total and the reservation. Every agent run records its attempts' cost, in a group or not.
- **Windowed expectations.** A pool's `expect_window` entries (`operation`, optional `transition` for `records.transition` and `match` on arguments, `min`/`max`, `window`, `timezone`; or `any_of` such counts, met when one is) are checked by the minute tick (step `cardea.m8.groups.windows`) once each window has closed. A miss sends `expectation_unmet` or `expectation_exceeded` mail from the kernel runtime to the supervisor; nothing fails a run after the fact. Only the latest closed window is checked, so longer downtime skips earlier windows, and windows that began before the group existed are not checked.

## Approval waits

Single-operation tasks and agent runs can wait for an approver before an effect runs ([ADR-036](decisions/036-approval-waits.md); worked example in [getting started](getting-started.md#4b-an-effect-that-waits-for-approval)).

- **Configuration.** Default rules live in the operation descriptor (`cardea.approval`). Host rules go in the provisioning document under `approvals.entries`, one per namespace and operation, and replace the descriptor default there (an empty `rules` list makes the operation autonomous in that namespace). `cardea provision` replaces them as a whole, like policy; the `policy.updated` event and `service_meta.approval_rules_digest` carry the rule-set digest. Provisioning refuses rules for operations that are not installed and conditions that do not pass the ADR-036 static checks.
- **Approvers.** A permission row with `"approve": true` installs the Casbin `approve` mode; `"allow": false` makes it approve-only. A `platform` row with `"approve": true` for the same namespace and operation is required too. The requesting agent, the kernel runtime and the agent and runtime of any ancestor task can never approve; the supervisor can, unless a rule sets `distinct_from_supervisor` (`403 four_eyes_required`).
- **Inspection.** `GET /v0.2/approvals?namespace=<ns>&state=pending` lists requests the caller may decide or supervises; `GET /v0.2/approvals/<id>` shows the whole frozen intent; the task view shows `waiting_for`. The journal carries `approval.requested`, `approval.decided`, `approval.expired`, `approval.withdrawn` and `approval.void` in the task's namespace, plus the per-request `approval.<id>`, without arguments; route them to people with an event trigger or a client of the events API.
- **Restart.** A waiting task holds no lease and no workflow; the request, the task and its checkpoint are in SQLite, so restarts change nothing. A decision enqueues the continuation workflow `cardea:<task>.approval.<request>` in its own transaction.
- **Expiry.** A request expires at `expires_at`: the rule's `expires_after_seconds`, capped by the task deadline, or the deadline when no rule sets it. The existing minute schedule (`cardea-triggers`, step `cardea.m6.approvals.expire`) expires overdue pending requests, so an expiry is applied at most about a minute late; a request that passed `expires_at` while the daemon was down expires on the first tick after it starts. A single-operation task then fails with `approval_expired` whatever `on_expiry` says; an agent run resumes and is told (`on_expiry: resume`, its default) or fails (`fail`). A decision after `expires_at` is refused with `409 approval_expired`, before or after the tick. If the request could no longer run at expiry (descriptor version or live ceiling changed), it becomes `void` and the task fails with `approval_void`. An expiry that fails is logged (`approval expiry failed`) and retried on the next tick.
- **Cancel.** `tasks.cancel` on a waiting task marks its pending request `withdrawn` in the same transaction; a later decision gets `409 approval_closed` with `"state": "withdrawn"`. Cancelling a task whose approved request has not started voids the request (`task_cancelled`).
- **Agent runs.** A held tool call returns `{"status": "approval_pending", "request_id", "expires_at"}` at once (as an MCP error result, so output schemas do not apply), creates no receipt and uses no tool-call budget. A run has at most one open request: the same call while it is pending returns it again, a different held call is refused with `approval_pending`, and more than `max_approval_waits` requests (default 4, at most 16) with `approval_limit`. When the attempt ends with a request open, or with one that closed during the attempt, the run waits (`waiting`, log line `run parked for approval`) instead of finishing; expectations are evaluated only once no request is open. A closed request enqueues the continuation workflow `cardea.m6.agent_run.approval` on the agent-run queue; that attempt runs an approved call as `apr-NN` first, then the session gets one `<approval>` block per closed request (decision, approver, time, reason, and the result of `apr-NN`). A continuation attempt does not use a retry; an attempt that parks after a successful session uses none either, a failed one still does. If an attempt loses its lease after proposing, the next attempt parks again without a session. Parking settles the attempt's leased mail: consumed if the session succeeded, released for redelivery if it failed. `approval_wait_seconds` (at most 30 days) is added to the run's deadline at admission; `expires_at` never exceeds the deadline. Cancelling a waiting run cancels it at once and withdraws its request.
- **Agent approvers.** An agent run may decide requests through the built-in tool `approvals.decide` (list it in `operations`). Each call is checked live: the run's agent needs the `approve` row on the operation, the run must be granted `approve:<operation>`, and its supervisor (and every ancestor's) must hold `approve` and `delegate` on it, as the platform allows; the requester exclusion applies. Admission checks the same `approve:<operation>` grant against the supervisor.
- **Not yet.** Per-grant rules in agent-run requests and triggers (ADR-036 slice 5). A snapshot restore voids open requests (`snapshot_restored`).

## Mailboxes, context and journal triggers

These are the rest of [ADR-034](decisions/034-agent-groups-and-shared-records.md) (slices 5–7; worked example in [getting started](getting-started.md#9-a-pool-that-wakes-on-mail-reads-its-context-and-stays-within-budget)). Migrations `m11_mailboxes` and `m12_journal_triggers` add their tables.

- **Mail.** Addresses are an instance, a pool `<group>.<pool>` or a group. A sender needs `mail:<address>`, and the platform must allow it there. A message is leased to a run when its attempt checkpoints. It is consumed when the run succeeds and released when it fails or is cancelled. After `max_deliveries` (1–10, default 3) it is dead-lettered, and the sender and the supervisor get a notice. The kernel's own notices (dead letters, spend, windowed expectations, content refusals) come from the kernel runtime.
- **Context.** A run's context blocks are read in one snapshot. Each block is cut to its `max_bytes`, and all of them together to `context_max_bytes` (default 24 KiB, at most 64 KiB). The inbox (default 6 KiB) and the caller's claims are never dropped entirely.
- **Journal triggers.** A pool's `event` triggers read the group's own kernel journal (`cardea://<namespace>`): `cardea.mailbox.*`, `cardea.record.*` and `cardea.approval.*` events. The minute schedule (`cardea-triggers`, step `cardea.m6.journal_triggers.tick`) admits each matching event once, through the same admission and live ceiling as any trigger. A member is not woken by its own actions unless `include_own` is set. While its run is unfinished, further events coalesce into one follow-up admission.
- **Overview.** `GET /v0.2/groups/{name}/overview?namespace=` gives the supervisor one read snapshot of the group: pools and members, collections, mail, open approvals, spend and recent journal events.

## Long confined operations and `code.session`

[ADR-023's amendment](decisions/023-installed-operation-registry.md#amendment-2026-11-16-long-confined-operations) lets a confined operation run up to its profile's `wall_seconds`, at most two hours. A call longer than five minutes holds a five-minute lease that a heartbeat renews every minute. If a renewal is refused, the worker stops and the call becomes `outcome_unknown`. A descriptor whose timeout exceeds the installed profile's wall time is refused with `409 operation_timeout_exceeds_profile`. Artifacts (`max_artifact_bytes`, at most 11 MiB) are stored as task-owned objects of at most 1 MiB each.

`code.session` ([ADR-037](decisions/037-coding-and-vcs-operations.md) slice 3, package `packages/cardea-coding-ops`) is the first such operation. The host owner names a read-only JSON file in `CARDEA_CODING_CONFIG` holding the harness; repositories come from the repository registry below. A call needs `repo:<alias>` for its repository, exactly as `vcs.*` does, and without it is refused before any worker starts. The harness: `"kind": "claude-code"` runs the pinned Claude Code CLI headless with repository settings, hooks and MCP servers turned off. The operation needs the confined profile `coding`; on a host without the ADR-028 helper it is refused. The small-host kit has the profile and an example configuration (`deploy/small-host/cardea/cardea-host-workers.json` and `cardea-coding.json`): model API egress only, with gates using a read-only package cache offline. Commands run with an environment built from nothing. Only the harness receives the profile's model credential, and a change or output that contains it is refused with `credential_in_change`. The harness is tested against a fake `claude` only. The Debian fit test in [`experiments/code-session`](../experiments/code-session/README.md) is written but has not run. The repository-host operations are below.

## Repository registry

One host-owner file, named by `CARDEA_REPOSITORIES`, lists the repository aliases that both `code.session` and `vcs.*` read ([ADR-037](decisions/037-coding-and-vcs-operations.md)). Set the variable for the kernel and for the `code-session` worker. The small-host kit uses `/opt/cardea-code/repositories.json` and the drop-in `cardea-kernel.service.d/repositories.conf`. The file must be a regular file of at most 256 KiB that neither group nor others can write, or it is refused with `unsafe_repositories`. Each alias holds:

- `source`: the read-only mirror `code.session` clones.
- `owner`, `name`, `api_url`, `git_url`: where `vcs.*` pushes and opens changes. Give all four or none. An alias without them is read-only, and `vcs.*` treats it as unknown.
- `gates`: the commands `code.session` runs on the change.
- `branch_prefix` (default `agent/{group}/`): pushed branches must start with it.

Callers need `repo:<alias>` to name an alias in either package.

**Deprecated.** For one release the old locations still work, with a deprecation warning (`repositories_deprecated` in the log): `repositories` in `CARDEA_CODING_CONFIG`, and `repositories` and `branch_prefix` in `CARDEA_VCS_CONFIG`. If the registry is also set and disagrees with them on any alias, the kernel refuses to start with `repositories_disagree`, and each call is refused the same way. To migrate, move the entries into the registry, check that the kernel starts, then delete them from the package files.

## Repository-host operations

The `cardea-vcs-ops` package installs `vcs.push_branch`, `vcs.open_change`, `vcs.comment`, `vcs.review`, `vcs.read_change` and `vcs.check_repository` ([ADR-037](decisions/037-coding-and-vcs-operations.md), slices 4 and 5, without credential minting).

- **Confined.** The installed descriptors run in the `vcs` worker profile (ADR-028), one worker per operation, so they need host execution and the root helper. See [deploy/small-host/README.md](../deploy/small-host/README.md), section 4, step 9. The service process holds no repository credential; the worker reads the repository registry (`CARDEA_REPOSITORIES`) bound read-only.
- **Configuration.** A JSON file named by `CARDEA_VCS_CONFIG` (in the worker, `/opt/cardea-vcs/vcs.json`) holds `allow_approve` (default false) and the credential provider. With `{"kind": "profile_credential"}` it holds no secret and may be a root-owned, read-only file; with a token it must be private. Repository aliases, their hosts and branch prefixes come from the repository registry. Install the descriptors from the package's `descriptors/` directory under `operations_dir`.
- **Authority.** Each call needs the operation's grant and `repo:<alias>` in the caller's live ceiling. Provision supervisor rows for `repo:<alias>` like any right, with `delegate` to pass it to group pools.
- **Branches.** Only group members push or open changes, under `agent/<group>/`; other names are refused with `branch_not_allowed` before any network call. `APPROVE` reviews are refused with `approve_refused` unless `allow_approve` is set.
- **Content gate.** Every push, pull request, comment and review passes the kernel's content gate first (ADR-037 slice 6): a push's added lines and commit messages, and every text field. Without a configured gate these operations refuse (`content_gate_unavailable`) and send nothing.
- **Credentials.** `profile_credential` reads the `vcs` profile's credential, a root-only file systemd loads into the unit. Until the minter exists that file is a token; the test provider (`{"kind": "test", "token": ...}`) is for fakes and tests. A production minter (a GitHub App or a personal access token, ADR-037 open question 7) is not available yet, so do not point these operations at a real host.
- **Readiness.** `vcs.check_repository` reports whether the repository's rules require a human review on the default branch and allow pushes only under the agent prefix. It lists what is missing and changes nothing; check it before pointing a team at a repository.
- **Recovery.** An interrupted push, pull request, comment or review is `outcome_unknown`. `POST /v0.2/tasks/<id>/operations/<operation_id>/reconcile` reads the host (the branch head, the pull request for the head, or the comment or review marker) and settles it without sending again.

## Content gate

The content gate ([ADR-037](decisions/037-coding-and-vcs-operations.md) decision 4) checks what an operation sends to a repository host before it sends it. Configure it under `content_gate` in the service configuration:

- `gitleaks_binary` and `gitleaks_sha256`: the pinned Gitleaks binary and its digest. `python3 packaging/fetch_gitleaks.py <dir>` downloads it, checks the release archive and prints both. The Debian package does not ship it.
- `gitleaks_config` (optional): a Gitleaks TOML file. Gitleaks's built-in rules apply without one.
- `denylist_file` (optional): one case-insensitive term per line, or `re:<pattern>`, with blank lines and `#` comments ignored. Keep it private (mode `0600`, owned by the kernel account). Terms are never logged or echoed: a finding names only `denylist:<line>`.
- `notify_supervisor` (default true) mails the run's supervisor on a refusal. `timeout_seconds` (1–300, default 30) bounds one scan.

The gate fails closed. A finding fails the call with `content_refused`. A missing or misconfigured gate, or a scanner that fails, fails it with `content_gate_unavailable`. A refused call's matching argument strings are replaced in its receipt, in the task's bound request and in a held approval's frozen intent, with `{"redacted": "content_refused", "sha256", "bytes"}`; digests are kept. Service connections use `secure_delete`, and a refusal truncates the write-ahead log. Confined workers reach the gate through a per-call loopback callback, so its configuration and denylist never enter the sandbox; a profile that cannot reach the loopback endpoint is refused with `content_gate_unavailable`.

## Error codes

[Error codes](error-codes.md) lists every code an API request can answer with, and every failure code a receipt or task can record, with its HTTP status and meaning. It is generated from the source (`make error-codes`), and a test keeps it current.

## Retention and deletion

Task, object, event, provenance, budget, receipt and idempotency history is retained for the lifetime of a store. Host payload cleanup is recoverable and retains reconciliation tombstones. Partial history or tombstone garbage collection is unsupported because deleting it could destroy evidence or permit duplicate effects.

For automatically named SQLite maintenance snapshots, stop the service and run:

```sh
runuser -u cardea-kernel -- cardea prune-backups --config /etc/cardea/config.json --keep 3 --days 7
```

This previews eligible generated filenames. Add `--apply` to delete them. A snapshot must be older than the chosen age **and** outside the newest minimum count. The command refuses symlinks and active-service access. It never deletes the live database/WAL, user-named backups, forensic recovery directories, task data or host tombstones. Three copies/seven days is a starting policy for generated snapshots; no scheduled deletion is installed.

Whole-store decommissioning is an explicit host-owner operation: stop and reconcile work, revoke provider enrollment and credentials, retire every retained backup/forensic copy and then retire storage. Package removal preserves accounts, configuration and data. Unlinking files is not secure erasure; use the actual storage/encryption provider's erasure guarantees where required. Do not introduce real sensitive data without an appropriate lifetime-retention and decommissioning policy.
