# M4: bounded native A2A delegation

Status: complete for the bounded profile; [acceptance and load evidence](M4-REPORT.md) pass. See [ADR-017](../../docs/decisions/017-m4-native-a2a-delegation.md).

The installed coordinator delegates up to four independent hash/copy jobs to a distinct logical worker agent on the same Debian host. Both agents use the trusted broker; workers retain M2's systemd/bubblewrap confinement and receive no identity/provider credentials. This profile uses deterministic agents and makes no model calls. External peers, arbitrary agent code and nested coordinator plans are not supported.

## Run a task

Initialize/migrate the private service store with `python -m cardea.service init --config <private-config>`, provision identities/policies and a cumulative host allocation, and start the existing Debian service with `host_execution: true`. No OpenRouter key is needed. Provision two distinct logical agents assigned to the installed runtime. The supervisor needs current allow **and delegate** permissions and platform allowance for every requested grant, including `tasks.delegate`, `tasks.submit`, `tasks.inspect`, `tasks.cancel`, `events.read`, `objects.read`, `objects.write` and the selected host operations.

Submit through `KernelClient.submit_delegation`, `POST /v0.2/delegation-tasks`, or:

```sh
python -m cardea.service submit-delegation --socket <store>/kernel.sock \
  --spiffe-socket <workload-api> --spiffe-id <supervisor-id> request.json
```

Example request (substitute provisioned IDs/namespace):

```json
{
  "request_id": "verify-two-inputs",
  "agent_id": "agent-a",
  "namespace": "test",
  "objective": "Verify both delegated results",
  "grants": ["tasks.delegate", "tasks.submit", "tasks.inspect", "tasks.cancel", "events.read", "objects.read", "objects.write", "host.sha256", "host.copy"],
  "children": [
    {"agent_id": "agent-b", "operation": "host.sha256", "payload": {"document": "one"}},
    {"agent_id": "agent-b", "operation": "host.copy", "payload": {"document": "two"}}
  ],
  "minimum_success": 2,
  "tool_milliseconds": 20000,
  "output_bytes": 163840,
  "concurrent_children": 2
}
```

Each child reserves a fixed 10,000 tool milliseconds and 65,536 output bytes within the parent's allocation. The coordinator reserves an additional 32,768 output bytes for its report and evidence. Child allocation slots are not recycled after failure or low measured usage; observed usage is still reported. Dispatch also consumes the existing cumulative supervisor host allocation. Limits are ceilings, not promises that capacity will remain available. Parent deadlines cap child deadlines. Child grants exclude submission/resume and cannot expand ancestor authority. Unallocated descendants through older endpoints are rejected.

Use `children`, `inspect`, `output`, `events`, `receipt` and `cancel` with the same CLI authentication flags; child receipts use their returned child task IDs. `/v0.2/tasks/{id}/children` exposes immutable slots and measured/retained child usage. A `minimum_success` below the number of children permits a partial objective; failed/unknown children remain visible in the report and their receipts. It never turns those child operations into success.

## State and failure ownership

The native A2A 1.0 SDK supplies discovery, protobuf models, JSON-RPC transport, task retrieval/listing, history bounds, pagination and cancellation. The private peer endpoint is `/a2a/worker`; its card is `/a2a/worker/.well-known/agent-card.json`. The broker uses a fresh SPIFFE JWT-SVID and an authenticated parent binding. The logical HTTP origin is fixed; HTTPX transports it over the existing private Unix socket. Caller URLs, cards and credentials are never forwarded to other origins.

A2A task IDs are the actual admitted child IDs. There is no duplicate SDK task table. Immutable `(parent, slot, child, message)` links commit with task admission and DBOS enqueue. Read-through A2A task views come from the same authoritative task/object records. `GetTask` polling supplies status/artifact updates; streaming/push and extended cards are not advertised. Read-only peer/API views use fresh SQLite WAL snapshots containing both permission checks and data, without reserving the writer. A2A handlers remain async, awaiting the existing synchronous kernel/authentication boundary through Starlette’s worker pool so other requests can progress. DBOS owns coordinator waits and a separate bounded coordinator queue, preserving worker capacity.

A lost local peer response is reconciled through these committed links. Identical admission messages preserve the task; changed content is rejected. Interrupted host effects retain M2's `outcome_unknown` and conservative charge, with no automatic relaunch. Parent cancellation first records child cancellation; the parent becomes terminal after children are stopped or classified. Live policy, supervisor/ancestor disablement, delegation revocation and deadlines are checked again at dispatch and publication. A native `completed` response is independently checked against the authorized input and stored child verification.

## Acceptance commands

```sh
uv run --locked --project apps/api pytest apps/api/tests/runtime/test_delegation.py
uv run --locked --project apps/api pytest -o pythonpath=apps/api/src experiments/m4/test_sdk_fit.py
uv run --locked --project apps/api python experiments/m4/integration_vm.py \
  --state <owned-prepared-vm> --output <fresh-evidence-directory>
```

The test VM represents the whole Debian machine, not a per-job sandbox. `integration_vm.py` updates only the owned acceptance host. Its real SPIRE and confined worker scenarios are separate from portable injected-host tests.

The independent `cardea-load` package supplies `delegation-run`, `delegation-baseline` and `delegation-compare`. `experiments/m4/load_vm.py` installs/runs that package on the owned host. The profile uses four parents/two clients, two children each, two reference repetitions and one comparison. Native round-trip timing includes serialization, transport, authentication and admission/read transactions; it is not a causal estimate of protocol-only overhead or a saturation benchmark.
