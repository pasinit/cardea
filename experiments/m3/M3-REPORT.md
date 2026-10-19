# M3 acceptance report — 2026-09-24

**Status: complete for the bounded M3 incident profile.** The OpenRouter broker, native MCP integration, deterministic Debian scenarios, live `z-ai/glm-4.7` restart/new-input scenario and matched load comparison pass. The M1/M2 regressions and hostile host probes also pass. General agent planning, remote MCP servers and delegation are outside this profile.

## Implemented behavior

An authenticated supervisor submits fictional incident records, explicit task grants and cumulative model/host allocations. The runtime discovers its installed MCP tool, sends only bounded authorized context through the native OpenRouter SDK and validates the returned function proposal against the authorized input. The trusted MCP server dispatches the fixed transformation inside the existing Debian systemd/bubblewrap profile. An independent verifier checks exact incident preservation and one anchored summary reference per record before publishing an output/evidence version.

The first round checkpoints and waits. After a daemon restart, a supervisor-side directory rescan discovers one added file and delivers a content-bound idempotent input event. The second round publishes version 2 and completes. Durable model/tool intents precede effects; committed proposals and outputs survive restart without duplicate dispatch. Unknown effects retain conservative reservations and are never automatically replayed. Authority and allocation checks remain below the current supervisor/delegation/platform ceiling. Child tasks remain rejected.

[ADR-016](../../docs/decisions/016-m3-openrouter-and-native-mcp.md) records the alternatives, licenses, exact dependency pins, native SDK fit, domain-specific gaps, event ingestion choice and load comparison contract. [Usage](README.md) describes configuration and authenticated commands.

## Executed checks

| Check | Result | Evidence |
| --- | --- | --- |
| Full backend suite | 262 passed, including 41 runtime cases | [JUnit](evidence/2026-09-24/portable.xml) |
| Frontend regression | 4 passed; no frontend source changes | [Full test log](evidence/2026-09-24/initial-full-tests.log) |
| Native SDK boundaries | Bounded requests/results; invalid usage/proposals rejected; no hidden retry/redirect; stdio interoperability; cancellation; abrupt MCP disconnect | Runtime cases in portable JUnit |
| Real-provider M1 regression | 46 passed with the updated dependencies | [JUnit](evidence/2026-09-24/m1-regression.xml) |
| Live GLM-4.7 on Debian | Restart/new-input scenario passed in 109.40 seconds | [JUnit](evidence/2026-09-24-live/live.xml), [receipts and artifacts](evidence/2026-09-24-live/live.json) |
| M3 on Debian | 6 passed in 525.46 seconds with deterministic provider fixture | [JUnit](evidence/2026-09-24/debian-m3.xml), [log](evidence/2026-09-24/debian-m3.log) |
| M2 on Debian with current helper/dependencies | 20 passed in 631.34 seconds; 11 hostile confinement probes passed | [JUnit](evidence/2026-09-24/debian-m2-regression.xml), [host probes](evidence/2026-09-24/debian-host-probes.json) |
| Lint and type checks | Backend, frontend, load package and M3 harness passed | [Log](evidence/2026-09-24/static-checks.log) |
| M3 matched load campaign | 12/12 tasks passed; 48 successful receipts, 24 verified output versions; no provisional warnings | [Report](../../packages/cardea-load/reports/2026-09-24-m3/REPORT.md), [result summary](evidence/2026-09-24/load-results.json) |
| Load package logic | 18 passed; build includes driver, fixture and dependency lock | [Harness checks](evidence/2026-09-24/load-harness-checks.log) |

The six Debian cases cover restart/input received during downtime, recovery after model settlement, recovery after output publication, interruption after a model intent, interruption after worker launch, and disclosure revocation before dispatch. Uncertain-effect tests observe a failed task with an `outcome_unknown` receipt and no verification, not an invented successful result.

The successful [deterministic scenario](evidence/2026-09-24/debian-m3.json) records two model calls, two successful tool calls, two output versions and final verification. Its fixture reports 100 total model tokens; these are synthetic, not observed OpenRouter usage. Measured workers used 1,640 milliseconds and 455 output bytes in total. One forced daemon restart and one harness-driven input rescan occurred. There were no automatic model retries and no human approval clicks during execution. Receipt intent-to-settlement elapsed times were 4.334 and 3.332 seconds for the model fixture and 4.200 and 3.509 seconds for the MCP/tool steps; these include broker/authentication/VM overhead and are not live provider latency.

## Live provider acceptance

The user-provided `.env` became available and supplied the private OpenRouter credential. The successful fresh task made exactly two `z-ai/glm-4.7` calls, dispatched two confined tools, produced output/evidence versions 1 and 2 and passed independent final verification after one forced daemon restart and a directory rescan. No human approval step or automatic retry occurred within this task.

| Round | Input tokens | Output tokens | Total tokens | Reported cost (USD) | Model intent-to-settlement |
| --- | --- | --- | --- | --- | --- |
| Initial records | 464 | 281 | 745 | 0.0008966 | 7.719 s |
| Added record after restart | 484 | 312 | 796 | 0.0009768 | 6.923 s |
| Total | 948 | 593 | 1,541 | 0.0018734 | 14.642 s |

Costs are the successful responses' reported `usage.cost`, not a total account invoice; OpenRouter denominates its credits in [USD](https://openrouter.ai/support/). Model latency includes broker/authentication/VM overhead. The workers used 1,667 measured milliseconds and 455 output bytes. Their MCP/receipt elapsed times were 4.255 and 4.112 seconds. Exact model/response/call IDs and verified artifacts are retained in [live evidence](evidence/2026-09-24-live/live.json).

An earlier attempt did not reach a tool: its model intent became `outcome_unknown` with the full 34,816-token reservation retained. Actual provider usage for that attempt is unknown and is excluded from the successful-task totals. The [failed test](evidence/2026-09-24-live/attempt-1.xml) and [administrative observation](evidence/2026-09-24-live/attempt-1-observation.json) are preserved. No attempt was silently retried. After read-only key/network/catalog checks, a new task used the advertised `max_tokens` parameter and omitted the unsupported `parallel_tool_calls` preference. Local validation still rejects multi-call proposals. The client also requests identity encoding and now retains sanitized numeric HTTP status for SDK errors. [ADR-016](../../docs/decisions/016-m3-openrouter-and-native-mcp.md#live-provider-compatibility-follow-up) records this compatibility decision.

The final production code passes the full backend/frontend suite, lint and type checks again; see the [final checks](evidence/2026-09-24-live/checks.log), [tested source manifest](evidence/2026-09-24-live/source-manifest.json) and [matching guest service hashes](evidence/2026-09-24-live/guest-service-hashes.json). Earlier deterministic/load evidence remains frozen at its original source hashes. The subsequent harness cleanup change references the runner-owned key directly rather than copying it into pytest temporary directories; a fixture smoke check and harness lint/type checks pass. Temporary local and guest key copies were deleted, the owned test VM was stopped, and `.env` remains ignored by Git. See [cleanup verification](evidence/2026-09-24-live/cleanup.json), [harness checks](evidence/2026-09-24-live/harness-checks.log) and the [delivery manifest](evidence/2026-09-24-live/delivery-manifest.json).

## Environment and limits

The acceptance host is one Debian 13 machine running the service, SPIRE, SQLite, DBOS, trusted brokers and confined worker processes. On this macOS development machine it is a disposable amd64 QEMU VM under emulation; a job does not boot its own Debian VM. The [verified VM limits](evidence/2026-09-24/vm-environment.json) give the guest two vCPUs and 1,536 MiB RAM; the outer QEMU container is capped at two CPUs, 3 GiB RAM and 128 processes. These timings are not native host capacity estimates.

OpenRouter 1.2.22, MCP/MCP-types 2.2.0 and JSON Schema 4.26.0 are pinned. OpenRouter's dependency constraint selects Pydantic 2.12.5; local and real-provider regression checks explicitly cover that change. Credentials stay in the broker. No credential, raw provider response or hidden reasoning is part of evidence. The [source manifest](evidence/2026-09-24/source-manifest.json) identifies the uncommitted tested content; the [guest inventory](evidence/2026-09-24/debian-inventory.json) records exact packages and confirms uploaded service source matches the checkout. The legacy escalation fixture JSON is not uploaded or used by Debian kernel acceptance.

The profile has exactly one installed tool and at most two rounds; it does not implement arbitrary commands/plugins, remote MCP servers, optional asynchronous MCP Tasks, A2A or general agent delegation. The directory adapter is an explicit authenticated rescan after reconnecting, not a background file watcher. The independent verifier checks observable index/reference criteria, not unrestricted semantic summary quality.

Local call/step/output and allocation admission ceilings are enforced. Model input reservation is conservative, not a provider invoice guarantee; remote cancellation cannot prove billing stopped. Unknown consumption retains its ledger charge. A lease-expiry recovery receipt may have no measured usage; the budget ledger remains authoritative for the retained reservation. General history retention, packaged installation/reboot/restore and operational reconciliation after restoring an older backup remain M5.

## Next scope

M3 is complete for this installed two-round application. M4 remains planned: bounded child delegation, shared parent budgets and native A2A between actual agents. Production packaging and operational reboot/restore acceptance remain M5.
