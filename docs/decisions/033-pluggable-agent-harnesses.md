# ADR-033: Pluggable Agent Harnesses

- **Date / owner / milestones:** 2026-10-20; runtime maintainers. Supersedes the "session adapter" parts of [ADR-027](027-agent-runs.md) (decision items 1 and 6): the Claude Agent SDK session is now one harness of several. The rest of ADR-027 (attempts, tool endpoint, journal, completion), [ADR-028](028-configurable-worker-profiles.md) and [ADR-031](031-agent-run-context-operations.md) are unchanged.
- **Status:** selected and implemented for portable execution: the contract (#17), the registry and selection (#19), and OpenCode. Nothing here has run against a live provider: tests use a fake OpenAI-compatible model server, and the OpenCode tests run the real pinned binary against it. Both harness kinds can run confined under ADR-028 profiles (decision item 7); like all of ADR-028, that is verified portably and not yet on Debian.
- **Requirement:**
  - A *harness* is the program that runs the model loop of an agent-run attempt and calls tools. The host owner installs one or more harnesses, and an agent run selects one, together with a model.
  - Provider choice belongs to the harness. Running GLM via OpenRouter is `harness=<an OpenCode entry whose provider is OpenRouter>`, `model=<the OpenRouter model id>`. The kernel has no provider-specific logic.
  - Whatever the harness, its only tools are the per-attempt Cardea MCP endpoint (ADR-027 item 3). Built-in shell, file, web and edit tools are off, so a harness cannot act outside receipts.
  - Bounds (turns, tool calls, cost, attempt time) hold for every harness. Where a harness cannot enforce one reliably, Cardea enforces it.
  - Selecting a harness is an authority decision, granted like any other right.

## Candidates and primary evidence

Checked 2026-10-20 in each project's source at the pinned tag, plus the npm, PyPI and GitHub release metadata. Pins are the newest release at least two weeks old.

| Harness | Licence | Pin (released) | Remote HTTP MCP with a bearer header | All built-in tools off | Providers | Headless output; cost, turns | Hard turn cap |
|---|---|---|---|---|---|---|---|
| [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python) (existing) | MIT; use is under Anthropic's commercial terms | `claude-agent-sdk==0.2.163` (2026-09-30) | yes (`mcp_servers` type `http`, `headers`) | yes (`tools=[]`, `strict_mcp_config`, `setting_sources=[]`) | Anthropic API or subscription | `ResultMessage.total_cost_usd`, `num_turns`, `usage` | yes (`max_turns`, `max_budget_usd`) |
| [OpenCode](https://github.com/anomalyco/opencode) | MIT | `opencode-ai@1.18.35` (2026-10-06), one Bun-compiled binary per platform | yes (`mcp.<name> = {type: "remote", url, headers}` with `{env:VAR}` substitution; Streamable HTTP, SSE fallback) | yes: `permission: {"*": "deny", "cardea_*": "allow"}` removes every built-in (bash, read, glob, grep, edit, write, apply_patch, task, webfetch, websearch, todowrite, skill, MCP resource tools) from the model request | OpenRouter and any OpenAI-compatible base URL (`@ai-sdk/openai-compatible` is bundled), Anthropic, OpenAI, local servers such as Ollama | `run --format json` NDJSON. Each `step_finish` carries `tokens` and `cost`; the cost is a client estimate (tokens × configured price, 0 when no price is set). There is no turn counter. | no: `steps` is soft (it injects a "maximum steps" message and keeps going) |
| [Goose](https://github.com/aaif-goose/goose) | Apache-2.0 | v1.53.0 (2026-10-02), Rust binary | yes, but headers only from a recipe or config file, not the CLI flag | yes (recipe `extensions:` or `--no-profile` replaces the platform extensions) | OpenRouter, OpenAI-compatible (`OPENAI_HOST`), others | `run --output-format stream-json`; `complete` event with tokens and `cost_usd` (provider-reported when available); no turn count | yes (`--max-turns`) |
| [Codex CLI](https://github.com/openai/codex) | Apache-2.0 | 0.160.1 (2026-10-05), Rust binary | yes (`bearer_token_env_var`, `http_headers`) | partial: shell, web search and others are separate feature flags; `apply_patch` comes from the model catalog and the MCP resource tools are added whenever an MCP server exists | Responses API only (`wire_api = "responses"`), so not most OpenAI-compatible servers | `exec --json`; tokens per turn, no cost | no flag for `exec` |
| [OpenAI Agents SDK](https://github.com/openai/openai-agents-python) | MIT | `openai-agents==0.23.1` (2026-10-02), Python library | yes (`MCPServerStreamableHttp` with headers) | nothing to remove: it has no built-in tools | any OpenAI-compatible base URL; LiteLLM for others | a library: we would build the loop, events and output. Usage has tokens and request count, no cost. Sends traces to OpenAI unless `OPENAI_AGENTS_DISABLE_TRACING=1`. | yes (`max_turns`) |

Sources:
- OpenCode: `packages/opencode/src/cli/cmd/run.ts` (NDJSON emitter), `src/permission/index.ts` (last matching rule wins), `src/session/llm/request.ts` (denied tools are not sent), `src/mcp/catalog.ts` (tool names `<server>_<tool>`), `src/mcp/index.ts` (a failed MCP connection leaves the server `failed` and the run continues without its tools), and the docs `providers.mdx`, `mcp-servers.mdx`, `permissions.mdx`, `config.mdx`, `cli.mdx`, all at tag `v1.18.35`.
- Goose: `crates/goose/src/agents/extension.rs`, `crates/goose-cli/src/session/mod.rs` at `v1.53.0`.
- Codex: `codex-rs/core/src/tools/spec_plan.rs`, `codex-rs/exec/src/exec_events.rs` at `rust-v0.160.1`.
- OpenAI Agents SDK: `src/agents/mcp/server.py`, `src/agents/usage.py` at `v0.23.1`.

**Recommendation and fit.** OpenCode first. It is the only open candidate that meets all of these at once:
- MCP headers can be set from the environment without writing a file;
- a single wildcard rule removes every built-in from the model request, which the tests check by inspecting the request the model server receives;
- OpenRouter and arbitrary OpenAI-compatible endpoints are bundled, with nothing installed at run time;
- per-step token usage is machine-readable;
- MIT licence, one pinned binary, and configuration injected via `OPENCODE_CONFIG_CONTENT`.

Its gaps are a soft step cap, an estimated cost, and a silent continue when MCP fails to connect. Cardea closes each of them itself (decision item 6).

- **Goose 1.53.0** is the documented fallback. It has a hard turn cap and provider-reported cost, but headers need a recipe file holding the per-attempt token on disk.
- **Codex CLI** is rejected for now: it speaks only the Responses API, and its built-ins cannot all be removed.
- **OpenAI Agents SDK** is not rejected. It has the cleanest tool story (none built in), but it is a library: adopting it means Cardea owns the agent loop, which ADR-027 decided against. Revisit if a Python harness is wanted.

## Decision

1. **Contract** (`cardea.runtime.harness`).
   - `HarnessRequest` (Pydantic) carries:
     - the system prompt and the prompt (instructions, journal, context);
     - the per-attempt MCP URL and bearer token, and the exposed tool names;
     - the bounds: `max_turns`, `max_tool_calls`, `max_cost_usd` and `timeout_seconds`;
     - the model id;
     - the attempt's job key.
   - `HarnessResult` (Pydantic) carries `text`, `success`, `turns`, `cost_usd` (nullable), `failure_reason` (set exactly when `success` is false), the harness's own `stop_reason`, and `usage`.
   - A harness implements `async run(HarnessRequest) -> HarnessResult`.
   - **The credential is not in the request.** It is part of the harness's host configuration and is resolved when the harness is built. This keeps the ADR-028 rule that no credential crosses the helper boundary. `ClaudeAgentSession` and `ConfinedAgentSession` implement the contract with no change in behaviour.
2. **Registry: a host file**, `harnesses_file` in the service configuration, conventionally `/etc/cardea-host-harnesses.json`.
   - It must be a regular file, not a symlink, owned by root (or the service user, for development), and not group- or world-writable.
   - It is separate from `cardea-host-workers.json` because a different program reads it. The workers file is parsed only by the root helper, which reports profiles to the service, never the file. The harness registry is read by the service at start, for admission. Folding it into the workers file would widen the helper's strict schema for data the helper does not use.
   - A confined harness refers to a profile and worker of the workers file by name.
   - The file holds `harnesses`, a `default` harness id, and optional `namespace_defaults`. Each entry has:
     - `id` and `kind` (the adapter: `claude-agent-sdk` or `opencode`);
     - `package`, the pin: a Python distribution checked against its installed version, or a binary checked against its SHA-256 at start;
     - `credential`: a private file and its kind;
     - or `confined`: a profile and worker, where the profile holds the credential;
     - `provider`: a `base_url` and optional headers;
     - `default_model` and `models`, the allowed model ids, each with its USD price per million input and output tokens.
   - Validation names the offending entry and field. Start fails with `harness_config_invalid`, `harness_not_installed` or `harness_pin_mismatch`.
   - **One code path.** A configuration with only the ADR-027 `agent_session` block keeps working. It is turned into an implicit registry entry `claude-sdk` (kind `claude-agent-sdk`):
     - its `model` becomes the entry's only allowed model;
     - its credential or confined profile carries over;
     - it is the default harness.

     Only that implicit entry is exempt from the harness grant, so existing clients need no change. Configuring both `agent_session` and `harnesses_file` is refused.
   - **Migration to an explicit entry.**
     1. Write `/etc/cardea-host-harnesses.json` with one `claude-agent-sdk` entry carrying the same credential (or `confined`) and model, plus any further models with prices.
     2. Replace `agent_session` with `harnesses_file` in the service configuration.
     3. Provision `harness:<id>` for the platform and the supervisors, and add it to client requests and trigger templates.
     4. Restart.
3. **Selection.** `AgentRunRequest` gains optional `harness` and `model`.
   - The harness resolves from the request, else the namespace default, else the file default. The model resolves from the request, else the harness's `default_model`.
   - Both are checked at admission, at trigger install and at each attempt. An unknown harness is `harness_not_installed` (422). A model outside the harness's `models` is `model_not_allowed` (422).
   - Trigger templates are full requests, so they carry `harness` and `model` in their definition digest (ADR-024).
4. **Authority.** Using a harness needs right `harness.use` on resource `<id>`, written as the grant `harness:<id>`, alongside `models.invoke`.
   - It is provisioned and checked like any other grant: platform and supervisor policy at admission, and the live ceiling at each attempt start.
   - When a registry is configured, every agent run needs the grant for the harness it resolves to. A run without it is `harness_not_granted` (403).
   - Unknown `harness:` grants are refused at provisioning when a registry is loaded.
5. **Bounds, per harness.**
   - Tool calls are counted at the endpoint for every harness (ADR-027).
   - The attempt timeout is enforced by the runtime for every harness, which stops the harness process tree.
   - Turns and cost:

     | Harness | Turns | Cost |
     |---|---|---|
     | `claude-agent-sdk` | the SDK's `max_turns` | the SDK's `max_budget_usd`; reported cost |
     | `opencode` | **Cardea counts `step_finish` events and kills the process at `max_turns`** (OpenCode's `steps` is not used as a bound) | **Cardea meters it**: tokens from each `step_finish` × the registry price; the process is killed once the metered cost exceeds `max_cost_usd` |

   - OpenCode's own estimate is recorded next to the metered cost in `usage`.
   - Because a harness that cannot report cost must be metered, the registry refuses an `opencode` entry with any model lacking a price. A local free model declares a price of 0.
6. **OpenCode adapter requirements.** Cardea enforces these itself rather than trusting the harness, and each has a test.
   1. **Turn cap and timeout** as in item 5. Exceeding the cap is failure `max_turns`, exceeding cost is `max_cost`, and both kill the process.
   2. **MCP-only tools.** Configuration is injected via `OPENCODE_CONFIG_CONTENT`, with `permission: {"*": "deny", "cardea_*": "allow"}` at the top level and on a dedicated agent. The test asserts that the request the model server receives offers only `cardea_*` tools.
   3. **No toolless runs.** A run whose MCP connection failed must fail, not run without tools. Three checks:
      - the adapter preflights the endpoint (MCP `initialize` with the bearer token) before starting;
      - it stops the process when OpenCode reports the `cardea` server `failed`;
      - the endpoint counts authenticated `tools/list` requests, and an OpenCode attempt that never listed tools fails, even if it ended "successfully".

      Each check is a hard failure with the distinct reason `harness_tools_unavailable`.
   4. **Isolation.**
      - Each attempt gets an empty working directory and a private temporary `HOME`, `XDG_*` and `OPENCODE_DB`.
      - `snapshot: false`, `--title` (so there is no extra title model call) and `--pure`.
      - `OPENCODE_DISABLE_{AUTOUPDATE, MODELS_FETCH, SHARE, PROJECT_CONFIG, CLAUDE_CODE, DEFAULT_PLUGINS, LSP_DOWNLOAD}=1`.
      - The environment is built from scratch, with no inherited variables beyond `PATH`, locale and certificates, so there is no OTEL exporter and no stray secrets.
      - Under a confined profile (ADR-028, `model_api_only`), the only network is the model API through the profile's proxy plus the loopback endpoint (item 7).
   5. **Pinning.** The binary's SHA-256 is in the registry and checked at start. A contract test parses a recorded `run --format json` transcript of the pinned version: `step_start`, `tool_use`, `text`, `step_finish` with `tokens.{input, output, reasoning, cache.{read, write}}` and `cost`, and `error`. An upgrade that renames fields fails it loudly. The end-to-end test runs the real pinned binary.
   6. **Credentials.** The provider key is read from the registry's credential file and reaches the process only through its environment, referenced from the configuration as `{env:CARDEA_PROVIDER_KEY}`. It is never written to a file and never logged. The MCP bearer token is handled the same way.

7. **Confined harnesses** (both kinds). A registry entry may set `confined: {profile, worker}` instead of `credential`, for `claude-agent-sdk` and for `opencode`. The session runs in an ADR-028 worker through the root helper: systemd-run plus bubblewrap, the profile's memory, CPU, task and wall limits, and `model_api_only` networking, which reaches only the profile's proxy (hostname allow-list = the provider's host) and the loopback tool endpoint.
   - **Which harness** runs is worker configuration, never the caller's. The worker runs `cardea.hosts.session_worker`, and its environment sets:
     - `CARDEA_HARNESS_KIND`, either `claude-agent-sdk` (the default) or `opencode`;
     - for the Claude Agent SDK, `CARDEA_CREDENTIAL_KIND`;
     - for OpenCode, `CARDEA_OPENCODE_BINARY`, a path under a read-only bind.
   - **What crosses the helper boundary** is the request plus the entry's non-secret settings: kind, provider base URL and headers, prices, default model, and the binary's pinned SHA-256.
     - The worker refuses `harness_kind_mismatch` if its kind differs, and `harness_pin_mismatch` if its binary's hash differs.
     - The credential stays in the profile (systemd `LoadCredential=`) and reaches OpenCode only through its environment.
   - **The OpenCode adapter** passes the profile's proxy variables through. Model calls therefore go via the proxy, and `NO_PROXY` keeps the tool endpoint direct. Its preflight never uses a proxy.
   - **Unchanged from unconfined runs:** turn, cost and tool-listing enforcement (item 6) work the same way. The runtime's tool-listing check applies to a confined OpenCode entry too.
   - **Kit:** the small-host kit (`deploy/small-host`) has a two-harness registry (Claude and OpenRouter via OpenCode, both confined), the `openrouter` profile, worker and proxy port, and install checks.

## Integration

- **Authority:** one new grant form, `harness:<id>`, alongside the kernel rights and installed operation names. No new tables. Policy rows use the existing `(principal, namespace, operation, mode)` shape.
- **State:** the request records the selected harness and model, and the run output records `harness`, `model`, `turns`, `cost_usd` and `usage`. The resolved defaults are not frozen at admission, so a run that omits `harness` uses the defaults current at each attempt.
- **Compatibility:**
  - Without `harnesses_file`, behaviour is ADR-027's.
  - With a registry, clients add `harness:<id>` to their grants and to the platform and supervisor policy.
  - The confined session worker accepts and emits both the old and the new result shapes.
- **Dependencies:** none added to the Python environment. OpenCode is a host-installed binary pinned by hash, like a confined worker's `argv[0]`.

## Verification

- **Contract** (#17): 7 tests in `tests/service/test_harness_contract.py`; all existing agent-run, confined and workload tests pass unchanged.
- **Registry, selection and authority** (#19): 27 tests in `tests/service/test_harnesses.py` cover entry and file validation (field-named errors, unsafe or symlinked files, unpriced models), pins (binary hash, package version), defaults by request, namespace and file, `harness_not_installed`/`model_not_allowed`/`harness_not_granted`, supervisor policy, revocation stopping a run, provisioning, trigger templates, and service start. Existing tests are unchanged.
- **OpenCode:** `tests/service/test_opencode.py` has 12 tests. The 7 end-to-end tests run the pinned 1.18.35 binary (`packaging/fetch_opencode.py`, archive SHA-256 checked) against `tests/service/fake_model.py`:
  - a kernel agent run with `harness=glm` calls two installed operations, both receipted;
  - the model is offered only `cardea_*` tools on every request, and the provider key arrives only as the request's bearer header;
  - the model asking for `bash` or `write` has no effect;
  - the run is killed at `max_turns` (2 steps) and past `max_cost_usd`, with the metered cost matching OpenCode's estimate;
  - a failed MCP connection after a successful preflight stops the run (`harness_tools_unavailable`);
  - cancellation on timeout kills the process.

  Portable tests cover the rest:
  - the NDJSON schema contract against a recorded transcript of the pinned version;
  - a renamed field failing loudly;
  - a configuration with no secrets, built-ins denied and no inherited environment;
  - the binary pin required in the registry;
  - an unreachable endpoint refused before start;
  - the runtime failing a tool-listing harness that never listed its tools.

  CI fetches the binary and sets `CARDEA_REQUIRE_OPENCODE=1`, so the end-to-end tests cannot skip there.
- **Confined harnesses:** `tests/service/test_confined_harnesses.py` runs the real session worker as a separate process with only a sandbox's environment, in place of the Debian helper.
  - A kernel agent run on a confined OpenCode entry calls two installed operations, both receipted.
  - Every model call goes through a forward proxy to the provider's host, while the tool endpoint is reached directly.
  - The provider key reaches the model only as the bearer header and never appears in the helper payload.
  - The worker refuses a binary that does not match the pin, a kind mismatch, and a Claude worker without a credential.

  `tests/service/test_deploy_examples.py` checks that every kit harness runs under a worker that lists its profile. The profile reaches only the provider's host and holds the credential, and the worker's kind matches the entry's.

  **Not run:** the same flow under bubblewrap on Debian (ADR-028's unverified list applies).

## Limits

- An unconfined harness (an entry with `credential`, not `confined`) has a private directory, a clean environment and denied built-ins, but no egress limit. Production hosts should confine every harness (item 7).
- Confinement is verified portably only. Bun honouring `HTTPS_PROXY`/`NO_PROXY` is tested with a forward proxy, but not inside bubblewrap on Debian. The memory and task sizing of an OpenCode worker is unmeasured.
- Metered cost uses the registry price and charges cache tokens at the input price, so it is conservative. It is not the provider's bill.
- A kill at `max_turns` or `max_cost_usd` is a failed attempt and is retried while attempts remain, as any failed session is (ADR-027).

## Revisit

Revisit if:
- OpenCode adds a hard step cap or provider-reported cost (Cardea's metering can then become a cross-check);
- the MCP-failure behaviour changes;
- a harness needs a capability the contract lacks, such as streaming partial results or resuming a session;
- Goose's CLI accepts MCP headers from the environment;
- Codex gains Chat Completions support.
