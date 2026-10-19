# Agent Runtime

**Purpose.** Execute durable, bounded agent steps under the authenticated task's current authority.

**Reuse.** [ADR-010](../decisions/010-m1-authenticated-kernel.md) selects DBOS and shared SQLite transactions. [ADR-016](../decisions/016-m3-openrouter-and-native-mcp.md) adds the maintained OpenRouter and MCP SDKs; the [model broker](model-broker.md) owns provider interaction.

**Design.** M1 retains deterministic JSON publication and independent byte/version verification. M3 adds one incident application with two rounds. Each round reserves a model call, retains a validated proposal, invokes one installed MCP tool through a confined Debian worker, verifies its artifacts and publishes a version. The first round waits for one authenticated additive input; the second completes the task. Both adapters use the existing scheduler, receipts and current supervisor ceiling.

**Recovery.** Model/tool intents precede effects. A committed proposal resumes without another model call; output, receipt and event wait commit together. Interrupted effects remain `outcome_unknown` and are never silently retried. Cancellation and revocation stop new dispatch/publication; an HTTP cancellation cannot prove remote billing stopped. Provider credentials and service storage remain outside workers.

**Status.** The M3 profile passes portable tests and six real Debian recovery/denial scenarios with a deterministic provider fixture. Live `z-ai/glm-4.7` restart/new-input acceptance and matched load comparison also pass; M3 is complete for this profile. This is a fixed two-round application, not arbitrary agent code, remote MCP server admission or a general reasoning loop. Native SDK stdio interoperability is tested separately; optional MCP Tasks are deferred. The separate M4 installed deterministic coordinator uses native A2A to delegate bounded host work; see [delegation](delegation-messaging.md). See [M3 usage](../../experiments/m3/README.md).
