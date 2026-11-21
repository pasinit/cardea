# Error codes

Generated from the kernel source by `make error-codes`; do not edit by hand. A test
fails while this file is stale. Each meaning is the message the code is raised
with, and `{…}` marks a value filled in at run time.

## API errors

What a request can answer with, as `{"schema_version", "code", "message"}`. A
`Fault` carries its own status (403 unless it names one). Store and contract
errors answer 409. A request that fails schema validation answers
`invalid_request` (422), and an unexpected failure answers `internal_error` (500).

| Code | HTTP | Meaning | Raised in |
|---|---|---|---|
| `agent_disabled` | 503 | Agent runs require a configured agent session | `cardea.service.kernel` |
| `ancestor_inactive` | 403 | Task or ancestor cannot execute | `cardea.service.delegation` |
| `approval_binding` | 403 | Approval request belongs to another task | `cardea.service.operation_runtime` |
| `approval_closed` | 409 | Approval request is no longer pending | `cardea.service.approval_requests` |
| `approval_expired` | 403, 409 | The approval request expired; Approval request has expired | `cardea.service.agent_run_runtime`, `cardea.service.approval_requests` |
| `approval_limit` | 409 | This run may wait for approval at most {…} times | `cardea.service.agent_run_runtime` |
| `approval_not_found` | 404 | No accessible approval request | `cardea.service.approval_requests` |
| `approval_pending` | 409 | Request {…} of this run is still open; nothing was done. Make this call again after it is decided. | `cardea.service.agent_run_runtime` |
| `approval_required` | 403 | A context operation cannot wait for approval | `cardea.service.agent_run_runtime` |
| `approval_void` | 409 | The request can no longer run | `cardea.service.approval_requests` |
| `approver_authority_lost` | 403 | Approver no longer holds approve | `cardea.service.agent_run_runtime` |
| `approver_denied` | 403 | Current authority does not permit this approval; The approving task's live ceiling: {…} | `cardea.service.approval_requests` |
| `arguments_too_large` | 413 | Arguments exceed the receipt's 64 KiB limit | `cardea.service.invocation` |
| `artifact_corrupt` | 400 | Stored artifact digest does not match | `cardea.service.maintenance` |
| `artifact_reference_invalid` | 400 | Referenced artifact is missing or changed | `cardea.service.maintenance` |
| `attempts_exhausted` | 403 | Every allowed session attempt was used | `cardea.service.agent_run_runtime` |
| `authority_denied` | 403 | Current authority does not permit this operation | `cardea.service.policy` |
| `backup_changed` | 409 | Backup changed during retention planning | `cardea.service.maintenance` |
| `backup_failed` | 500 | Backup integrity check failed | `cardea.service.database` |
| `backup_has_sidecars` | 400 | Use a standalone completed SQLite snapshot | `cardea.service.admin` |
| `body_too_large` | 413 | A message body is at most {…} bytes | `cardea.service.mailbox` |
| `budget_exhausted` | 429 | The {…} run ceiling ({…} runs per {…}) is spent: {…} runs admitted; The {…} spend ceiling ({…} USD per {…}) would be exceeded: {…} USD used or reserved, this run reserves {…} USD | `cardea.service.agent_run_runtime`, `cardea.service.spend` |
| `builtin_operation` | 422 | Built-in mailbox operations run in agent runs | `cardea.service.kernel` |
| `cancel_requested` | 403 | Cancellation requested; Task or ancestor was cancelled | `cardea.service.agent_run_runtime`, `cardea.service.delegation`, `cardea.service.host_runtime`, `cardea.service.operation_runtime` |
| `capacity_exceeded` | 429 | M1 profile permits at most 100 active tasks | `cardea.service.kernel` |
| `checkpoint_required` | 409 | A kernel contract rule refused the change | `cardea.kernel.lifecycle` |
| `child_objective_failed` | 403 | Too few verified child results | `cardea.service.delegation_runtime` |
| `claim_lease_too_short` | 422 | Pool {…}: collection {…} has a {…} s claim lease, below {…} s (2 x attempts x attempt timeout + 240) | `cardea.service.records` |
| `collection_deleted` | 409 | This collection was deleted; choose a new name | `cardea.service.records` |
| `collection_in_use` | 409 | Groups {…} still use this collection; remove it from them first | `cardea.service.records` |
| `collection_mismatch` | 422 | Path and body collection names differ | `cardea.service.app` |
| `collection_not_found` | 404, 422 | Collection {…} does not exist in this namespace under this supervisor; No accessible collection | `cardea.service.groups`, `cardea.service.records` |
| `collection_supervisor` | 403 | The caller must be the collection's supervisor; The collection belongs to another supervisor | `cardea.service.records` |
| `content_gate_unavailable` | 503 | The content gate cannot run ({…}) | `cardea.service.content_gate` |
| `context_not_read_only` | 422 | Context operations must be read-only | `cardea.service.operations` |
| `custom` | — | A pull request already exists for {…}:{…}. | `cardea_vcs_ops.fake` |
| `deadline_exceeded` | 403, 409 | A kernel contract rule refused the change; Execution deadline elapsed | `cardea.kernel.lifecycle`, `cardea.service.runtime` |
| `deadline_or_terminal` | 403 | Task cannot dispatch | `cardea.service.agent_run_runtime`, `cardea.service.host_runtime`, `cardea.service.operation_runtime` |
| `decision_conflict` | 409 | Decision ID was already used with another body | `cardea.service.approval_requests` |
| `delegation_binding` | 403 | Caller is not the bound coordinator runtime; Only the bound coordinator runtime may delegate; Message does not match the admitted objective (and 6 more) | `cardea.runtime.a2a`, `cardea.service.delegation`, `cardea.service.kernel` |
| `demo_not_ready` | 409 | Demo needs a saved checkpoint before resumption. | `cardea.kernel.demo` |
| `effect_in_doubt` | 409 | An earlier invocation of this effect is unresolved | `cardea.service.invocation` |
| `email_config_invalid` | 500 | The email configuration is invalid | `cardea_email_ops.config` |
| `env_rerun_limit` | 429 | At most {…} forced reruns per day for this subject | `cardea.service.env_runs` |
| `env_run_in_progress` | 409 | An identical run is in progress; retry when it settles | `cardea.service.env_runs` |
| `event_mode_unsupported` | 415 | Send the event in CloudEvents structured JSON mode; Content-Type must be {…} | `cardea.service.events` |
| `event_secret_unavailable` | 503 | The trigger's signing secret cannot be used | `cardea.service.events` |
| `event_too_large` | 413 | Event exceeds {…} bytes | `cardea.service.events` |
| `event_trigger_host_owned` | 403 | Event triggers are installed by provisioning | `cardea.service.kernel` |
| `file_too_large` | 500 | Configuration or credential exceeds limit; {…} exceeds {…} bytes | `cardea.service.database`, `cardea.service.harnesses` |
| `four_eyes_required` | 403 | This request needs an approver other than the supervisor | `cardea.service.approval_requests` |
| `group_deleting` | 409 | The group is still draining after its deletion | `cardea.service.groups` |
| `group_event_source_unsupported` | 422 | Pool {…}: event triggers read {…}; inbound sources are host-owned event triggers | `cardea.service.groups` |
| `group_grant_denied` | 403 | Pool {…} grant {…} is outside the supervisor's delegable authority | `cardea.service.groups` |
| `group_immutable` | 409 | spec.supervisor of a group is immutable | `cardea.service.groups` |
| `group_mismatch` | 422 | Path and manifest group names differ | `cardea.service.groups` |
| `group_not_found` | 404 | No accessible group | `cardea.service.groups` |
| `group_supervisor` | 403 | Supervisors manage only their own groups | `cardea.service.groups` |
| `harness_config_invalid` | 500 | Cannot open {…}: {…} | `cardea.service.harnesses` |
| `harness_not_granted` | 403 | Using harness {…} needs grant {…} | `cardea.service.harnesses` |
| `harness_not_installed` | 422, 500 | Harness {…}: {…} is not installed; Harness {…} is not installed; Harness {…}: kind {…} unavailable (and 3 more) | `cardea.service.harnesses` |
| `harness_pin_mismatch` | 500 | Harness {…}: {…} {…} is installed, {…} is pinned; Harness {…}: binary does not match its pin | `cardea.service.harnesses` |
| `host_already_reserved` | 403 | Existing host intent cannot be dispatched again | `cardea.service.host_budget` |
| `host_budget_exhausted` | 429 | Host allocation has insufficient capacity | `cardea.service.host_budget` |
| `host_budget_missing` | 403 | Host owner must provision an execution allocation | `cardea.service.host_budget` |
| `host_profile_mismatch` | 503 | Installed helper differs from tested source | `cardea.hosts.debian` |
| `host_rejected` | 409 | Host refused the job: {…} | `cardea.hosts.debian` |
| `host_unavailable` | 503 | Host result could not be established | `cardea.hosts.debian` |
| `http_error` | — | HTTP request cannot be served | `cardea.service.app` |
| `identity_immutable` | 403 | Provision a new principal instead of reassigning identity | `cardea.service.kernel` |
| `input_digest_mismatch` | 409 | Input {…} does not match its sha256 | `cardea.service.invocation` |
| `input_namespace` | 403 | Inputs must be objects of the call's namespace | `cardea.service.invocation` |
| `input_too_large` | 413 | Host input exceeds 16384 bytes; Inputs exceed 16 MiB | `cardea.service.host_runtime`, `cardea.service.invocation`, `cardea.service.kernel` |
| `input_unavailable` | 409 | Input {…}: {…} | `cardea.service.invocation` |
| `intent_mismatch` | 409 | intent_digest does not match the stored intent | `cardea.service.approval_requests` |
| `internal_error` | — | Operation failed; inspect state before retry | `cardea.service.app` |
| `invalid_approve` | 403 | Only installed operations take approve rights | `cardea.service.kernel` |
| `invalid_arguments` | 422 | Malformed gate request; The resource argument must name a resource; Input {…} must name an object version (and 4 more) | `cardea.service.content_gate`, `cardea.service.invocation`, `cardea.service.mailbox`, `cardea.service.operations` |
| `invalid_backup` | 400 | Use a standalone SQLite backup, not the active file; Wrong database application identity; Unsupported base schema (and 5 more) | `cardea.service.admin`, `cardea.service.maintenance` |
| `invalid_checkpoint` | 409 | This store checkpoints only settled work. | `cardea.kernel.store` |
| `invalid_credential` | 401 | Bound coordinator proof required; Bearer authentication is required; Credential has expired (and 2 more) | `cardea.runtime.a2a`, `cardea.service.app`, `cardea.service.auth` |
| `invalid_cursor` | 400, 409 | Event cursor must be a nonnegative integer.; Cursor does not name a message; Cursor does not name an item of this list | `cardea.kernel.store`, `cardea.service.app`, `cardea.service.pages` |
| `invalid_diff` | 422 | A hunk header is malformed; An added line is outside a file's hunk | `cardea.service.content_gate` |
| `invalid_event` | 400 | Body is not a supported CloudEvents 1.0 event | `cardea.service.events` |
| `invalid_host_usage` | 500 | Host usage exceeds its reserved allocation | `cardea.service.host_budget` |
| `invalid_lease` | 409 | A dispatch lease must start at admission time. | `cardea.kernel.store` |
| `invalid_offset` | 400 | Offset is past the end | `cardea.service.pages` |
| `invalid_page` | 409 | Event page size must be between 1 and 1000. | `cardea.kernel.store` |
| `invalid_parts` | 422 | A message has 1 to {…} parts; Parts are A2A text or data parts only; Each part holds exactly one of text or data (and 2 more) | `cardea.service.mailbox` |
| `invalid_peer_result` | 403 | Peer must return a durable task | `cardea.service.delegation_runtime` |
| `invalid_platform_policy` | 403 | Platform policy must name platform | `cardea.service.kernel` |
| `invalid_provenance` | 403 | Task provenance cannot be established; Invalid ancestor chain; Ancestor chain exceeds the supported bound | `cardea.service.approval_requests`, `cardea.service.delegation`, `cardea.service.kernel`, `cardea.service.policy` |
| `invalid_recovery_epoch` | 500 | Invalid recovery epoch | `cardea.service.maintenance` |
| `invalid_repositories` | 403 | The repository registry is invalid ({…}) | `cardea.service.repositories` |
| `invalid_request` | — | Request does not match the versioned schema | `cardea.service.app` |
| `invalid_retention` | 400 | Keep and age must be positive | `cardea.service.maintenance` |
| `invalid_signature` | 401 | Event signature could not be verified | `cardea.service.events` |
| `invalid_slot` | 403 | Unknown delegation slot | `cardea.service.delegation` |
| `invalid_submission` | 409 | A new task must start queued at revision zero. | `cardea.kernel.store` |
| `invalid_template` | 422 | The trigger's template is no longer valid; Pool {…}: the run template is not a valid agent run ({…}); Pool {…}: a windowed expectation names {…}, which its runs do not expose | `cardea.service.events`, `cardea.service.groups` |
| `invalid_transaction` | 409 | Store is closed or already in a transaction.; Cannot close an active transaction.; Transaction is closed, failed, or read-only. (and 1 more) | `cardea.kernel.store`, `cardea.service.database` |
| `invalid_transition` | 409 | A kernel contract rule refused the change | `cardea.kernel.lifecycle` |
| `invalid_version` | 409 | Expected version must be a nonnegative integer. | `cardea.kernel.store` |
| `lease_not_expired` | 409 | Recovery requires an expired running lease. | `cardea.kernel.store` |
| `lease_reused` | 409 | Dispatch lease IDs cannot be reused. | `cardea.kernel.store` |
| `login_failed` | 401 | OIDC login could not be verified | `cardea.service.login` |
| `login_timeout` | 401 | Login timed out | `cardea.service.__main__` |
| `member_binding` | 403 | A group member runs only for its group's supervisor; A group member runs only active agent runs | `cardea.service.groups` |
| `member_busy` | 409 | The member's previous run is unfinished | `cardea.service.triggers` |
| `message_not_found` | 404 | No accessible message | `cardea.service.mailbox` |
| `migration_required` | 500 | Run service initialization first | `cardea.service.database` |
| `model_not_allowed` | 422 | Model {…} is not allowed on {…} | `cardea.service.harnesses` |
| `not_refused` | 409 | Only a refused call can be redacted. | `cardea.kernel.store` |
| `not_uncertain` | 409 | Only uncertain operations can be reconciled.; Only uncertain operations can be reconciled | `cardea.kernel.store`, `cardea.service.operation_runtime` |
| `object_corrupt` | 409 | Stored content does not match its version digest. | `cardea.kernel.store` |
| `object_not_found` | 409 | Object does not exist.; Object version does not exist. | `cardea.kernel.store` |
| `object_too_large` | 409 | Objects must be bytes of at most one MiB. | `cardea.kernel.store` |
| `object_version_conflict` | 409 | Object version has changed. | `cardea.kernel.store` |
| `operation_changed` | 403 | Operation was reinstalled | `cardea.service.agent_run_runtime` |
| `operation_duplicate` | 500 | Operation is installed twice | `cardea.service.operations` |
| `operation_handler_missing` | 500, 503 | Operation handler is not installed; Operation handler is not loaded; Reconciler is not loaded (and 1 more) | `cardea.service.operations` |
| `operation_invalid` | 500 | Operation declares no reply poller; Invalid operation descriptor {…}: {…} | `cardea.service.operations` |
| `operation_not_found` | 409 | Operation does not exist. | `cardea.kernel.store` |
| `operation_not_installed` | 403, 409 | Approval rules name an operation that is not installed; Policy names an operation that is not installed; Operation is not installed | `cardea.service.kernel`, `cardea.service.operations` |
| `operation_timeout_exceeds_profile` | 409 | The operation's {…} s timeout exceeds its profile's {…} s wall time | `cardea.service.confined` |
| `output_limit` | 403 | Coordinator report exceeds reserved bytes | `cardea.service.delegation_runtime` |
| `parent_binding` | 403 | Child must retain its supervisor and namespace | `cardea.service.policy` |
| `parent_budget_exhausted` | 429 | Child allocation exceeds parent ceiling | `cardea.service.delegation` |
| `parent_concurrency_exhausted` | 429 | Parent concurrency is fully reserved | `cardea.service.delegation` |
| `peer_binding` | 403 | Peer is not installed in this trust domain | `cardea.service.kernel` |
| `peer_card_changed` | 503 | Installed peer contract differs | `cardea.runtime.a2a` |
| `peer_identity_mismatch` | 403 | Peer returned a different task | `cardea.service.delegation_runtime` |
| `principal_conflict` | 409 | Principal {…} already exists and is not a member of this group; Principal {…} belongs to another group; Principal ID equals an agent group or pool mail address (and 1 more) | `cardea.service.groups`, `cardea.service.kernel`, `cardea.service.mailbox` |
| `principal_disabled` | 403 | Principal is unknown or disabled | `cardea.service.policy` |
| `provider_configuration` | 500 | Invalid identity provider URL; Identity provider requires HTTPS; Discovery issuer does not match configuration (and 2 more) | `cardea.service.auth` |
| `receipt_unavailable` | 409 | Task has not admitted an operation | `cardea.service.app` |
| `reconciler_missing` | 409 | Operation declares no reconciler | `cardea.service.operation_runtime` |
| `reconciliation_required` | 409 | A kernel contract rule refused the change | `cardea.kernel.lifecycle` |
| `record_not_found` | 404 | No such record | `cardea.service.records` |
| `recovery_clock_regressed` | 409 | Restore requires a non-regressing clock | `cardea.service.admin` |
| `recovery_lease_live` | 409 | Wait for copied worker leases to expire | `cardea.service.admin` |
| `repositories_disagree` | 403 | {…} and {…} disagree about: {…} | `cardea.service.repositories` |
| `request_conflict` | 409 | A kernel contract rule refused the change; Control request ID was used for another action; Request ID already has different arguments (and 1 more) | `cardea.kernel.lifecycle`, `cardea.service.kernel`, `cardea.service.mailbox` |
| `request_scope_mismatch` | 409 | Request identity or deadline exceeds task. | `cardea.kernel.store` |
| `request_too_large` | 413 | Request exceeds {…} bytes; Task request exceeds {…} bytes | `cardea.service.app`, `cardea.service.triggers` |
| `response_too_large` | — | Use a smaller page | `cardea.service.app` |
| `restore_incomplete` | 503 | Complete the interrupted restore; Complete the interrupted restore before opening; Pending restore (and 1 more) | `cardea.service.admin`, `cardea.service.database`, `cardea.service.maintenance` |
| `restored_scope_sealed` | 403 | New execution requires a fresh recovery namespace | `cardea.service.policy` |
| `retired_profile` | 503 | Cancel unfinished incident agent tasks with the previous release first | `cardea.service.kernel` |
| `revision_conflict` | 409 | A kernel contract rule refused the change | `cardea.kernel.lifecycle` |
| `runtime_binding` | 403, 500 | Runtime does not match task; Coordinator identity changed; Scheduler is not the installed kernel runtime (and 3 more) | `cardea.service.agent_run_runtime`, `cardea.service.delegation_runtime`, `cardea.service.host_runtime`, `cardea.service.kernel`, `cardea.service.operation_runtime`, `cardea.service.policy`, `cardea.service.runtime` |
| `runtime_identity_unavailable` | 503 | Cannot authenticate peer call; Runtime identity cannot be established | `cardea.service.auth` |
| `self_approval` | 403 | The requesting agent or runtime cannot approve | `cardea.service.approval_requests` |
| `service_running` | 409 | The store already has an active service | `cardea.service.database` |
| `session_expired` | 401 | Sign in again to renew the session | `cardea.service.login` |
| `stale_lease` | 409 | Worker lease is wrong or expired. | `cardea.kernel.store` |
| `stale_session` | 403 | This attempt has ended | `cardea.service.agent_run_runtime` |
| `state_occupied` | 409 | {…} record(s) are in state {…}; remove it with migrate {from: {…}, to: <state>} | `cardea.service.records` |
| `stop_unconfirmed` | 503 | Host stop was not confirmed | `cardea.service.host_runtime` |
| `task_exists` | 409 | Task ID already exists. | `cardea.kernel.store` |
| `task_grant_denied` | 403 | The approving task is not granted {…}; Operation requires additional explicit grants; Operation is outside explicit task grants | `cardea.service.approval_requests`, `cardea.service.kernel`, `cardea.service.policy` |
| `task_not_found` | 404, 409 | Task does not exist.; Unknown peer task; No accessible authenticated task | `cardea.kernel.store`, `cardea.runtime.a2a`, `cardea.service.policy` |
| `task_not_running` | 409 | Task has no live worker. | `cardea.kernel.store` |
| `task_terminal` | 409 | The task has ended; the request cannot be decided; A terminal task cannot be resumed | `cardea.service.approval_requests`, `cardea.service.kernel` |
| `time_regression` | 409 | A kernel contract rule refused the change | `cardea.kernel.lifecycle` |
| `tls_configuration` | 500 | TLS certificate, key or CA is invalid | `cardea.service.server` |
| `transaction_aborted` | 409 | A mutation failed; transaction rolled back.; Failed kernel mutation aborted transaction | `cardea.kernel.store`, `cardea.service.database` |
| `trigger_changed` | 409 | The trigger changed; deliver the event again | `cardea.service.events` |
| `trigger_duplicate` | 403 | Trigger IDs must be unique | `cardea.service.triggers` |
| `trigger_immutable` | 403 | Install a new trigger to change its supervisor; Install a new trigger to change its kind | `cardea.service.triggers` |
| `trigger_mismatch` | 422 | Path and body trigger IDs differ | `cardea.service.app` |
| `trigger_not_found` | 404 | No active event trigger; No accessible trigger | `cardea.service.events`, `cardea.service.kernel` |
| `trigger_owner` | 409 | Host- and group-owned triggers change only through provisioning or their group manifest; Trigger is managed by another owner | `cardea.service.kernel`, `cardea.service.triggers` |
| `trigger_supervisor` | 403 | Supervisors schedule only their own work | `cardea.service.kernel` |
| `unknown_identity` | 401 | Identity is not provisioned | `cardea.service.auth` |
| `unknown_principal` | 403 | Budget principal is not provisioned; Policy principal is not provisioned; Trigger supervisor is not provisioned | `cardea.service.kernel`, `cardea.service.triggers` |
| `unknown_profile` | 500 | Task runtime profile is not installed | `cardea.service.profiles` |
| `unknown_recipient` | 422 | The group has no active members here; The recipient is unknown or disabled; Mail goes to agents of this kernel (and 1 more) | `cardea.service.mailbox` |
| `unresolved_operations` | 409 | Settle operations before changing task state. | `cardea.kernel.store` |
| `unsafe_backup` | 500 | File must be private, regular and owned | `cardea.service.maintenance` |
| `unsafe_directory` | 500 | Service directory must be private | `cardea.service.database` |
| `unsafe_environment` | 500 | Remove inherited DBOS environment overrides | `cardea.service.server` |
| `unsafe_file` | 500 | Credential/configuration file must be private; {…} must be a regular file owned by root and writable only by its owner; A vcs configuration with a token must be private (and 1 more) | `cardea.service.database`, `cardea.service.harnesses`, `cardea_vcs_ops.config` |
| `unsafe_repositories` | 403 | The repository registry must be a private regular file | `cardea.service.repositories` |
| `unsafe_socket` | 500 | Refusing to replace a non-owned socket | `cardea.service.server` |
| `unsafe_store_path` | 409 | Store paths must be private and owned by this user. | `cardea.kernel.store` |
| `unsupported_encoding` | — | Only identity content encoding is supported | `cardea.service.app` |
| `unsupported_host` | 503 | No host configured; {…} requires the installed host; Confined sessions require the confined host (and 1 more) | `cardea.service.host_runtime`, `cardea.service.kernel`, `cardea.service.server` |
| `unsupported_runtime` | 400, 409, 500 | Task is not the installed persistence demo.; Runtime upgrade requires an explicit compatibility path; Use the tested DBOS release (and 1 more) | `cardea.kernel.demo`, `cardea.service.maintenance`, `cardea.service.server` |
| `unsupported_schema` | 400, 500 | Unknown service schema version | `cardea.service.database`, `cardea.service.maintenance` |
| `unsupported_store` | 409 | Unknown database identity or schema version. | `cardea.kernel.store` |
| `usage_regression` | 409 | A kernel contract rule refused the change | `cardea.kernel.lifecycle` |
| `verification_failed` | 403, 409 | Peer omitted the expected artifact; Child has no terminal evidence; Peer artifact failed independent checks (and 1 more) | `cardea.runtime.a2a`, `cardea.service.delegation_runtime`, `cardea.service.operation_runtime` |
| `verification_required` | 409 | A kernel contract rule refused the change | `cardea.kernel.lifecycle` |
| `wait_not_ready` | 409 | No matching event or elapsed timer. | `cardea.kernel.store` |
| `worker_profile_not_installed` | 409 | The host owner has not installed this worker profile; Confined workers are not enabled on this host | `cardea.service.confined`, `cardea.service.kernel` |

## Call and task failures

What a receipt's or a task's `failure.code` can hold. These are not request errors:
a run or an operation task records them, and the run view and receipts show them.
Codes raised in `cardea_coding_ops` and `cardea_vcs_ops` come from those operations
packages, and codes raised in `cardea_reference_ops` from the fake reference package
used by the examples.

| Code | Meaning | Raised in |
|---|---|---|
| `approve_refused` | APPROVE reviews are not allowed on this host | `cardea_vcs_ops.ops` |
| `artifact_refused` | — | `cardea.service.kernel` |
| `bad_cursor` | cursor is not one this operation returned | `cardea.service.records` |
| `branch_moved` | The branch moved since the caller read it | `cardea_vcs_ops.ops` |
| `branch_not_allowed` | Only agent-group members push branches or open changes; Branches must be named {…}<name> | `cardea_vcs_ops.ops` |
| `branch_unknown` | No branch {…} | `cardea_vcs_ops.ops` |
| `call_abandoned` | — | `cardea.service.invocation` |
| `change_invalid` | The change bundle was not staged | `cardea_vcs_ops.ops` |
| `checkpoint_missing` | Completed work has no resumable checkpoint. | `cardea.kernel.store` |
| `claim_limit` | The holder already has {…} claim(s); the collection allows {…} | `cardea.service.records` |
| `claim_lost` | This claim is no longer held at epoch {…}; the record is at epoch {…}; Writes to a held record need claim_epoch {…} | `cardea.service.records` |
| `claims_disabled` | This collection has no claims | `cardea.service.records` |
| `claims_not_standalone` | Claims on this collection are taken by state transitions, not records.claim | `cardea.service.records` |
| `collection_not_found` | No such collection in this namespace | `cardea.service.records` |
| `content_gate_unavailable` | The content gate is unreachable; The content gate is not available to this call | `cardea.service.content_gate`, `cardea_email_ops.ops`, `cardea_vcs_ops.ops` |
| `content_refusal_ignored` | the worker continued after the content gate refused | `cardea.service.confined` |
| `content_refused` | The content gate refused a call (ADR-037 decision 4). | `cardea.service.operations` |
| `credential_in_change` | the change or a command's output contains the model credential; nothing is returned | `cardea_coding_ops.session` |
| `credential_not_loaded` | systemd did not load the profile's credential: check that the credential file exists and that / is shared (findmnt -o PROPAGATION /) | `cardea.service.confined` |
| `credential_unavailable` | — | `cardea_vcs_ops.ops` |
| `deadline_exceeded` | Task deadline elapsed. | `cardea.kernel.store` |
| `dependencies_mismatch` | dependencies is registries, but this worker's profile allows no package registry; dependencies is cache, but this worker's profile allows package registries ({…}); remove them from the profile and the proxy, or set dependencies to registries | `cardea_coding_ops.session` |
| `effect_not_applied` | Destination shows no effect | `cardea.service.operation_runtime` |
| `env_commit_unknown` | the commit is not in the repository; the checkout is not the named commit | `cardea_env_ops.try_` |
| `env_invalid` | the scenario environment may not set {…} | `cardea_env_ops.try_` |
| `env_scenario_unknown` | Unknown scenarios: {…} | `cardea_env_ops.try_` |
| `env_source_unavailable` | the repository source could not be read | `cardea_env_ops.try_` |
| `env_tier_unavailable` | Scenario {…} needs the {…} tier, which this host does not offer | `cardea_env_ops.try_` |
| `env_toolchain_missing` | The pinned toolchain image is missing | `cardea_env_ops.try_` |
| `env_unavailable` | env.try is not configured for this repository | `cardea_env_ops.try_` |
| `executor_busy` | — | `cardea.service.invocation` |
| `expectation_exceeded` | — | `cardea.service.agent_run_runtime` |
| `gate_changed_checkout` | a gate changed the checkout ({…}); gates may check the change, not alter it | `cardea_coding_ops.session` |
| `guard_refused` | {…} guard failed to evaluate: {…}; {…} guard refused this call | `cardea.service.records` |
| `host_mount_propagation` | the host's / or /run mount is not shared, so systemd cannot give the worker its credential; run `mount --make-rshared /` or reboot the host | `cardea.service.confined` |
| `host_outcome_unknown` | Interrupted host intent; no replay | `cardea.service.host_runtime` |
| `host_refused` | The host reports a duplicate it does not list | `cardea_vcs_ops.ops` |
| `input_digest_mismatch` | Staged input {…} does not match its digest | `cardea.hosts.operation_worker` |
| `input_too_large` | The call's inputs exceed what a worker may take | `cardea.service.confined` |
| `input_unavailable` | The job's inputs were not staged; A staged input is missing | `cardea.hosts.operation_worker` |
| `invalid_arguments` | Invalid decision; commit must be a full 40-hex object ID; Not a branch name | `cardea.service.approval_requests`, `cardea_env_ops.try_`, `cardea_vcs_ops.ops` |
| `isolation_failed` | untrusted steps not contained: {…} | `cardea_coding_ops.session` |
| `member_draining` | A draining group member takes no new claims | `cardea.service.records` |
| `merge_already_merged` | The pull request is already merged | `cardea_vcs_ops.ops` |
| `merge_approval_stale` | An allowed reviewer approved an earlier head; a new approval is needed | `cardea_vcs_ops.ops` |
| `merge_checks_failed` | Checks did not succeed: {…} | `cardea_vcs_ops.ops` |
| `merge_checks_missing` | No check ran on the head | `cardea_vcs_ops.ops` |
| `merge_checks_pending` | Checks still running: {…} | `cardea_vcs_ops.ops` |
| `merge_head_moved` | The pull request's head moved since the caller read it; The head moved before the merge | `cardea_vcs_ops.ops` |
| `merge_host_not_approved` | The host shows no approval of the head by the configured reviewer | `cardea_vcs_ops.ops` |
| `merge_not_approved` | No approval from an allowed reviewer | `cardea_vcs_ops.ops` |
| `merge_not_configured` | Merging is not enabled on this host | `cardea_vcs_ops.ops` |
| `merge_not_mergeable` | The host does not report the pull request as mergeable | `cardea_vcs_ops.ops` |
| `merge_not_open` | Only an open, ready pull request can merge | `cardea_vcs_ops.ops` |
| `merge_not_opened` | No vcs.open_change receipt for this pull request | `cardea_vcs_ops.ops` |
| `merge_not_opener` | Only the member that opened the pull request may merge it | `cardea_vcs_ops.ops` |
| `operation_failed` | no result within {…} s; {…} (HTTP {…}) | `cardea.service.invocation` |
| `operation_outcome_unknown` | Interrupted operation; no replay | `cardea.service.invocation` |
| `operation_rejected` | — | `cardea.service.invocation` |
| `push_rejected` | — | `cardea_vcs_ops.ops` |
| `rate_limit_unavailable` | The rate limit is not available | `cardea_email_ops.ops` |
| `rate_limited` | This role may send {…} emails per {…} days | `cardea_email_ops.ops` |
| `receipt_invalid` | Cite a receipt of {…}; The cited call did not succeed; The cited call was not made for this record (and 3 more) | `cardea.service.records` |
| `receipt_required` | This transition needs receipt_id of a {…} call | `cardea.service.records` |
| `receipt_result_refused` | The cited call has no result to check; The receipt result guard failed to evaluate: {…}; The cited call's result does not satisfy this transition (and 1 more) | `cardea.service.records` |
| `recipient_not_allowed` | This role may not email that recipient | `cardea_email_ops.ops` |
| `reconciliation_required` | An interrupted effect has unknown outcome. | `cardea.kernel.store` |
| `record_claimed` | Another principal holds this record | `cardea.service.records` |
| `record_invalid` | labels must be distinct; data must be an object; data does not match the collection schema at {…} ({…}) (and 2 more) | `cardea.service.records` |
| `record_not_found` | No such record | `cardea.service.records` |
| `record_too_large` | data is {…} bytes; the collection allows {…} | `cardea.service.records` |
| `repository_unknown` | No such repository alias on this host | `cardea_env_ops.try_`, `cardea_vcs_ops.ops` |
| `risk_limit` | The order would breach the desk limit | `cardea_reference_ops.__init__` |
| `snapshot_restored` | Historical execution sealed by snapshot recovery. | `cardea.service.admin` |
| `stale_call` | This call is no longer running | `cardea.service.approval_requests`, `cardea.service.records` |
| `transition_not_allowed` | Transition {…} needs state {…}; the record is in {…} | `cardea.service.records` |
| `transition_unknown` | Collection {…} has no transition {…} | `cardea.service.records` |
| `verification_failed` | Stored index or input versions failed checks.; result does not match the operation's output schema or size limit (at most 64 KiB is recorded) | `cardea.kernel.demo`, `cardea.service.invocation` |
| `version_conflict` | expected version {…}, record is at version {…} | `cardea.service.records` |
| `worker_error` | {…} (HTTP {…}) | `cardea.service.confined` |
| `worker_failed` | exit status {…}, systemd result {…} | `cardea.service.confined` |
| `worker_lost` | Worker lease expired. | `cardea.kernel.store` |
| `worker_output_invalid` | unexpected artifacts; output is not JSON; not a reconciliation | `cardea.service.confined` |
| `worker_start_unknown` | helper start failed | `cardea.service.confined` |
| `worker_status_unknown` | worker status unavailable | `cardea.service.confined` |
