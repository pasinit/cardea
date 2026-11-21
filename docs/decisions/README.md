# Decision records

Each record states the requirement, the open-source candidates considered, fit evidence and what remains unverified. New records use the [template](../decision-record-template.md) and follow the [open-source-first policy](../standards-first.md).

| ADR | Decision |
|---|---|
| [001](001-m0-contracts.md) | M0 Identity, Wire, and Digest Contracts |
| [002](002-workflow-engine-comparison.md) | Workflow Engine Comparison |
| [003](003-dbos-fit.md) | DBOS First Runtime Fit |
| [004](004-temporal-fit.md) | Temporal Runtime Fit |
| [005](005-absurd-fit.md) | Absurd Runtime Fit |
| [006](006-hatchet-fit.md) | Hatchet Runtime Fit |
| [007](007-runtime-integration-direction.md) | Runtime Integration Direction |
| [008](008-dbos-storage-integration.md) | DBOS and Cardea Storage Integration |
| [009](009-spire-bootstrap-fit.md) | SPIRE Bootstrap and Identity Binding Fit |
| [010](010-m1-authenticated-kernel.md) | M1 Authenticated Headless Kernel |
| [011](011-m1-load-testing.md) | Separate M1 load-test package and reference baseline |
| [012](012-m2-host-confinement.md) | M2 Debian host confinement fit |
| [013](013-installed-host-operations.md) | Installed host operations |
| [014](014-object-scoped-host-inputs.md) | Object-scoped host inputs |
| [015](015-m2-accounting-retention-and-acceptance.md) | M2 accounting, retention and acceptance |
| [016](016-m3-openrouter-and-native-mcp.md) | M3 OpenRouter broker and native MCP (agent-task path superseded by 032) |
| [017](017-m4-native-a2a-delegation.md) | Native A2A delegation and shared execution ceilings |
| [018](018-m5-debian-package.md) | Debian deployment, first M5 increment |
| [019](019-m5-operational-recovery.md) | M5 operational recovery and maintenance |
| [020](020-company-product-direction.md) | Company Product Direction and Reuse Shortlist |
| [021](021-company-process-data-integration.md) | Cross-Team Process and Data Integration |
| [023](023-installed-operation-registry.md) | Installed Operation Registry (implemented, with the 2026-11-16 amendment for long confined calls and artifacts; portable tests only) |
| [024](024-time-triggers.md) | Time Triggers (implemented; portable tests only) |
| [025](025-remote-kernel-api.md) | Remote Kernel API over TLS (implemented; portable tests only) |
| [026](026-effect-idempotency-and-reconciliation.md) | Effect Idempotency Scopes and Reconciliation (implemented; portable tests only) |
| [027](027-agent-runs.md) | Agent Runs with Cardea-Owned Tools (implemented; portable tests only) |
| [028](028-configurable-worker-profiles.md) | Configurable Worker Profiles (implemented; portable tests only, Debian probes written but not run) |
| [029](029-supervisor-schedules-expectations-and-run-evidence.md) | Supervisor Schedules, Run Expectations and Run Evidence (implemented, with the 2026-11-16 `any_of` and `match` amendment; portable tests only) |
| [030](030-small-host-profile.md) | Small-Host Profile (selected; kit validated by tests, not installed on a real host) |
| [031](031-agent-run-context-operations.md) | Agent-Run Context Operations (implemented; portable tests only) |
| [032](032-remove-incident-agent-tasks.md) | Remove the Incident Agent-Task Path |
| [033](033-pluggable-agent-harnesses.md) | Pluggable Agent Harnesses (implemented; portable tests only) |
| [034](034-agent-groups-and-shared-records.md) | Agent Groups, Shared Records, State Machines and Mailboxes (implemented: all eleven slices and the team overview; portable tests only) |
| [035](035-event-triggers.md) | Event Triggers (slice 1 implemented, plus journal triggers from ADR-034; portable tests only) |
| [036](036-approval-waits.md) | Approval Waits and Per-Operation Autonomy (slices 1–4 implemented, slices 5–6 pending; portable tests only) |
| [037](037-coding-and-vcs-operations.md) | Coding and Version-Control Operations for an Agent Team (proposed; implemented: the content gate with redaction, long confined calls, `code.session` with the Claude Code harness entry and `coding` profile, the `vcs.*` operations without credential minting; portable tests only, Debian fit test written but not run) |
| [038](038-throwaway-test-environments.md) | Throwaway Test Environments for Agent Teams (accepted; process tier) |
| [039](039-external-email-operations.md) | External Email Operations: `email.send` with AgentMail and SMTP/IMAP adapters, and kernel reply polling (implemented; portable tests only, live smoke step written but not run) |
| [040](040-team-setup-command.md) | One Setup Command from a Fresh Debian Host to a Running Team (implemented; portable tests only) |

**Verified on Debian** means a run on the supported Debian host, recorded as evidence in the ADR. The milestone ADRs up to ADR-019 record their own Debian evidence. From ADR-023 on, nothing is verified on Debian yet: "portable tests only" above means the tests pass on the development platforms and in CI, and nothing more. The [Debian acceptance checklist](../acceptance/debian-adr-034-037.md) lists what must run to verify ADR-034, ADR-035, ADR-036 and the ADR-023 amendment.

ADR-022, a gap analysis for a first client application, is not published. ADRs 023–030 cite its gap numbers (for example "gap #3"); each of those records restates its own requirement in full.
