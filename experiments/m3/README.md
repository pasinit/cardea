# M3 — OpenRouter agent and native MCP

Status: complete for the bounded incident profile. Portable tests, six deterministic-provider Debian scenarios, the live `z-ai/glm-4.7` restart/new-input scenario and matched load comparison pass. See the [acceptance report](M3-REPORT.md) and [ADR-016](../../docs/decisions/016-m3-openrouter-and-native-mcp.md).

## What this profile does

An authenticated incident task sends a bounded authorized batch to the configured OpenRouter model. The model proposes one `compile_incidents` tool call. Cardea checks its arguments against the authorized records, records a separate tool intent and invokes an installed trusted MCP server using the native SDK. The server delegates the transformation to a fixed systemd/bubblewrap worker. The kernel independently checks the index and summary references and publishes output version 1, then waits for one additive record. After authenticated input delivery it repeats with the updated batch and publishes verified version 2.

This is one bounded agent application, not a general prompt-to-shell executor. The model does select and populate a real function call; it cannot choose commands, endpoints, mounts, output paths or its own verifier. The trusted local MCP server is a broker adapter, not a sandbox. Its actual transformation runs in the M2 worker profile. SDK stdio interoperability is separately tested; arbitrary remote MCP servers, provider-hosted tools, asynchronous MCP Tasks and A2A are not enabled.

## Configuration

Stop the daemon and run `python -m cardea.service init --config /private/config.json` to migrate to `m3_001`. Reinstall the fixed host helper from current source: its digest must match the service. Existing M1/M2 request and receipt encodings are preserved. Old M1/M2 backups require explicit migration before serving. Follow [Debian setup](../m2/INTEGRATION.md) and the [authenticated service quickstart](../../docs/m1-quickstart.md).

Add to the private service configuration:

```json
{
  "host_execution": true,
  "agent_runtime": {
    "model": "z-ai/glm-4.7",
    "api_key_env": "OPENROUTER_API_KEY"
  }
}
```

Load the user's private `.env` into the broker process explicitly, for example from the repository root:

```sh
PYTHONPATH=apps/api/src uv run --env-file .env --locked --project apps/api \
  python -m cardea.service serve --config /private/config.json
```

Alternatively use `api_key_file` pointing to a service-owned mode-0600 file containing only the key. Exactly one credential source is required. No default model or custom endpoint is accepted. `.env` is Git-ignored; do not copy it into tests, artifacts or workers. A missing credential fails startup.

In the complete host-owner provisioning document, grant the supervisor allow/delegate and platform allow for `models.invoke`, `models.disclose`, `tools.incidents`, `inputs.append`, plus the existing task/object/event rights. Add explicit cumulative model and host allocations:

```json
{
  "model_budgets": [{"principal_id":"supervisor","namespace":"test","calls":10,"tokens":1000000}],
  "host_budgets": [{"principal_id":"supervisor","namespace":"test","tool_milliseconds":100000,"output_bytes":1000000,"concurrent_operations":1}]
}
```

These are fragments, not replacement provisioning documents. Omitted allocations deny new work. Reprovisioning never resets consumption. Host pools cover both M2 tasks and M3 tool calls. Model reservations use 32,768 input tokens plus the maximum output tokens per call; see [accounting limits](#accounting-and-recovery).

## Submit, inspect and add input

A request file:

```json
{
  "request_id":"incident-index-001",
  "agent_id":"agent-a",
  "namespace":"test",
  "objective":"Use the permitted tool to create an incident index and Markdown summary, then update them when new input arrives.",
  "inputs":{"records":[{"id":"INC-1","title":"Fictional service delay","severity":"low"}]},
  "grants":["objects.read","objects.write","tasks.inspect","tasks.cancel","tasks.resume","events.read","tasks.submit","models.invoke","models.disclose","tools.incidents","inputs.append"],
  "timeout_seconds":600,
  "max_output_tokens":2048
}
```

Use `submit-agent --socket /private/kernel.sock --session /private/session.json request.json`, or workload credential flags from the existing CLI. Python exposes `KernelClient.submit_agent(AgentTaskRequest(...))`; HTTP is `POST /v0.2/agent-tasks`. Normal inspect/output/events/cancel routes apply. `agent-receipts` lists the separate model/tool receipts; `/v0.2/model-budget` and `/v0.2/host-budget` report the caller's allocations and usage under current inspection authority.

To resume with additional input, send `append-input --task-id TASK_ID ... update.json`. The JSON has a fresh `request_id` and `inputs.records` containing all original records unchanged plus exactly one new record. Repeated delivery IDs with identical content are idempotent; changed content conflicts. This profile accepts one update only and no child binding. An ordinary resume signal cannot invent the missing record.

For a directory containing one JSON incident per file, use `ingest-agent-directory --task-id TASK_ID --socket ... --session ... /absolute/inputs`. It rescans the directory, rejects symlinks/non-regular/oversized files, derives the delivery ID from its canonical snapshot, and calls the same authenticated endpoint. It can be rerun after downtime or a lost response. Initial input can be constructed with `cardea.runtime.directory.snapshot(path)`. This bounded adapter has no background watcher; the supervisor-side process/harness starts it after reconnecting.

## Accounting and recovery

Two model calls and two tool calls are the fixed task maximum. Each call reserves capacity before dispatch. SDK HTTP retries and provider fallbacks are disabled; local validation rejects any response other than one permitted tool call. The request uses the advertised `max_tokens` field and omits an unsupported parallel-call preference; redirects/custom API origins and provider-side tools are not enabled. Reported model/input/output tokens and optional cost are retained without hidden reasoning. Unknown consumption retains its reservation. A reported token overage is recorded in full and stops subsequent work. The conservative input reserve is not a provider invoice guarantee: tokenizers and provider-added framing differ. Maximum output tokens is enforced through the provider parameter and checked locally.

All explicit task grants are intersected with live supervisor/delegation/platform authority. Revocation is checked before effects, while waiting for results, and before publication. Cancelling an HTTP request cannot prove a provider stopped billing; interrupted model intents remain unknown. Local workers retain independent deadlines and process-tree cancellation. Existing 90-second agent leases fence uncertain effects before terminal recovery; no ambiguous call is automatically replayed. Committed model proposals checkpoint before tool execution. Published outputs, receipts and event waits commit together; input delivered before waiting is retained. Cleanup retries independently of task outcome and keeps helper replay tombstones.

## Reproduce acceptance

Portable tests: `make m3-test`. These use the real SDKs with controlled HTTP and host fixtures; they cannot establish model choice or Debian isolation.

Prepare an owned Debian VM using the M2 setup, then:

```sh
PYTHONPATH=apps/api/src uv run --locked --project apps/api \
  python experiments/m3/integration_vm.py --state /tmp/cardea-m3-vm
```

For the live scenario, provide a private raw-key file using `--key-file /private/openrouter.key`. The runner uses the explicitly selected `z-ai/glm-4.7`, transfers only that credential into the owned test host, and removes the temporary guest copy afterward. It records results without credentials. Deterministic model injection exists only in `experiments/m3/daemon_probe.py`, never in production service configuration.
