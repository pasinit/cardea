# M1 headless kernel

The `cardea.service` package runs a private, authenticated Unix-socket service with DBOS dispatch and shared SQLite persistence. It accepts a bounded JSON publication task, checkpoints it, optionally waits for an authenticated resume signal, and verifies the stored artifact before success. No browser, model, external tool, or arbitrary process is needed for task execution.

## Try the restart demonstration

On macOS, build the pinned native SPIRE fixture once using `experiments/spire/build-spire.sh`, then run:

```sh
make install
make m1-demo
```

The demonstration creates disposable SPIRE and kernel state, authenticates a supervisor, submits a task, kills the daemon while waiting, restarts it, resumes, and checks that its acknowledged receipt survived. It removes its processes and temporary state afterward. This is a test environment, not a host-wide provider installation.

For the complete acceptance suite, including human login, see [the reproducible provider/Debian setup](../experiments/m1/README.md). The authenticated contract is [kernel-openapi.json](../packages/kernel-openapi.json).

## Run a persistent service

Install the locked API dependencies, provide a trusted SPIRE Workload API endpoint, and provision separate external identities for the supervisor and runtime. Register logical agents administratively against the runtime principal. SPIRE registration/bootstrap is provider administration: use a verified trust bundle and the existing SPIRE attestation mechanism, not a Cardea credential issuer. See [ADR-009](decisions/009-spire-bootstrap-fit.md).

Create a private configuration file (0600), using absolute paths:

```json
{
  "data_dir": "/absolute/private/cardea",
  "spiffe_socket": "unix:///absolute/spire/agent.sock",
  "trust_domain": "example.test",
  "audience": "cardea-kernel",
  "runtime_spiffe_id": "spiffe://example.test/runtime/kernel"
}
```

Create a private provisioning document from [provision.example.json](../experiments/m1/provision.example.json). Replace fictional identity bindings, namespaces and expiry dates deliberately. Provisioning **replaces the complete current policy**; omitted principals retain their existing disabled flag and identity. Disable a principal explicitly; removing its permissions alone also restricts its tasks. Exact external bindings and admitted task provenance cannot be reassigned. The special `platform` policy identifier cannot be provisioned as a principal.

From `apps/api`, with `PYTHONPATH=src`:

```sh
export PYTHONPATH=src
uv run python -m cardea.service init --config /absolute/config.json
uv run python -m cardea.service provision --config /absolute/config.json /absolute/provision.json
uv run python -m cardea.service serve --config /absolute/config.json
```

The daemon owns `kernel.sock` (0600) in its private directory (0700). Run it as a dedicated, trusted host account. One process owns the store; a second service, initialization, backup or restore is rejected while it runs. Policy provisioning is a separate trusted host-owner operation and may run while the service is active; it atomically audits the change and wakes pending workflows to recheck authority. The socket path must fit the host's Unix-socket path limit.

## Submit and resume

A workload client fetches a fresh audience-bound JWT-SVID from its local Workload API. Identity never comes from a JSON body:

```python
from pathlib import Path
from cardea.service.client import KernelClient
from cardea.service.models import Signal, TaskRequest

client = KernelClient.for_workload(
    Path("/absolute/private/cardea/kernel.sock"),
    "unix:///absolute/spire/agent.sock",
    "spiffe://example.test/supervisor/alice",
    "cardea-kernel",
)
try:
    admitted = client.submit(TaskRequest(
        request_id="publish-001", agent_id="agent-a", namespace="workspace",
        objective="Publish and verify this JSON",
        payload={"message": "hello"}, wait_for_event=True,
    ))
    task_id = admitted.task.task_id
    print(client.inspect(task_id))
    client.control(task_id, Signal(request_id="resume-001"))
    # Inspect until terminal; output and receipt routes require current authority too.
    print(client.request("GET", f"/tasks/{task_id}/events", params={"after": 0, "limit": 20}))
finally:
    client.close()
```

Submission IDs are scoped to the authenticated supervisor. Retry the same ID and arguments after a lost response; changed arguments conflict. Control IDs are scoped to a task; a repeated resume/cancel signal is idempotent. A resume arriving before the wait is retained. Cancelled, failed and successful tasks cannot reopen. The CLI exposes the same submit/inspect/resume/cancel/output/events operations; `python -m cardea.service --help` lists options.

The seven explicit task rights are `objects.read`, `objects.write`, `tasks.inspect`, `tasks.cancel`, `tasks.resume`, `events.read`, and `tasks.submit`. All are requested by default; the runtime requires object read/write. Admission rejects grants outside the supervisor's permission and delegation authority or platform policy. Every ancestor restricts child authority. Broader runtime credentials cannot enlarge a task's grants. Namespaces are exact; wildcard policies and arbitrary operation dispatch are unsupported.

## Human login

Configure an existing HTTPS OIDC issuer with a public authorization-code client (PKCE S256, loopback redirect `http://127.0.0.1:8765/callback`) and a confidential introspection client. Include the kernel audience in access tokens; use the kernel audience as the introspection client ID or explicitly include both. Current Keycloak requires the introspecting client in the audience ([provider guidance](https://www.keycloak.org/docs/26.7.2/upgrading/)). Configure endpoints on the issuer's origin. Store the confidential secret in its own 0600 file, never in task/config JSON.

Add this optional configuration:

```json
{
  "oidc": {
    "issuer": "https://identity.example/realms/cardea",
    "client_id": "cardea-kernel",
    "audience": "cardea-kernel",
    "client_secret_file": "/absolute/private/introspection-secret"
  }
}
```

Provision a human principal with `auth_kind: "oidc"`, exact issuer and provider subject, plus current permission/delegation entries. Then:

```sh
uv run python -m cardea.service login --issuer https://identity.example/realms/cardea \
  --client-id cardea-cli --session /absolute/private/session.json
uv run python -m cardea.service submit --socket /absolute/private/cardea/kernel.sock \
  --session /absolute/private/session.json /absolute/request.json
```

The browser is used only for human authentication. Authlib handles state and PKCE; signed ID-token issuer/audience/nonce checks establish login identity. The API accepts provider-introspected access tokens, never ID tokens. The private session refreshes access tokens as needed. Runtime authentication still uses SPIFFE. An explicit loopback-only HTTP exception exists for disposable development providers; production issuers require HTTPS. Provider outages fail closed. Disabling a kernel principal immediately denies future requests and new task effects; provider account/registration deletion alone is not a substitute for revoking persisted kernel authority.

## Recovery, migration and limits

Stop the daemon before backup or restore:

```sh
uv run python -m cardea.service backup --config /absolute/config.json /absolute/private/backup.sqlite3
uv run python -m cardea.service restore --config /absolute/config.json /absolute/private/backup.sqlite3
```

SQLite's backup API captures Cardea and DBOS tables together. M5 restore validates schema/runtime and artifact integrity in private staging, preserves the original files, and seals historical execution. Restored principals are disabled and policies removed; explicitly provision current rights and fresh budgets in the reported recovery namespace before new execution. Completed receipts remain readable under fresh authority. Follow the [operating guide](operations.md); snapshot restore deliberately does not resume old waiting tasks. Alembic adds authenticated tables without rewriting v0.1 snapshots or hashes; old tasks without provenance are not exposed through authenticated routes. Initialization snapshots an existing store before migration; the first DBOS launch takes another whole-store snapshot. Unknown schema/runtime versions fail explicitly. Upgrading beyond DBOS 3.0.0/workflow `cardea-m1-v1` requires a new tested migration decision, not a dependency bump.

SIGTERM lets Uvicorn drain requests; process exit releases the store lock. DBOS 3's synchronous executor does not join active workflows on destruction, so the dedicated daemon retains ownership until OS termination instead of releasing the lock to surviving worker threads. In-flight database transactions roll back; native DBOS history resumes committed phases on restart. No automatic multi-host takeover is offered.

M1 admits at most 100 active tasks, with four concurrent DBOS workflows. A waiting workflow occupies one of those slots. Requests are limited to 32 KiB by default (host configuration `max_request_bytes`; normalized task documents to 16 KiB, agent runs to 128 KiB), responses to 256 KiB, event pages to 100 entries, and task deadlines to 5–3600 seconds. Queued time counts toward the deadline. Policy expiry or provider failure prevents new effects; failure is terminal and requires an explicitly new task. Task/artifact/event history is retained for the lifetime of the store. M5 adds explicit age/count pruning of generated maintenance backups; it never prunes live history or host tombstones. Monitor disk use and follow the [retention procedure](operations.md#retention-and-deletion).

The M1 runtime runs trusted fixed code inside the kernel, and all artifacts are transactional local bytes. Debian acceptance establishes this userspace profile. It does **not** establish worker isolation, arbitrary command execution, model budgets, external input ingestion, general delegation coordination, a Debian package, or reboot installation. Subsequent M2–M5 reports establish their bounded profiles; arbitrary commands and general remote coordination remain unsupported. Same-UID processes share a host trust boundary and may share SPIRE identities; untrusted workers must not receive this account's storage or credentials.
