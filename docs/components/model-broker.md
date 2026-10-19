# Model and MCP broker

**Purpose.** Let a model propose permitted actions while the kernel retains authority, accounting, effect history and completion decisions.

**Reuse.** [ADR-016](../decisions/016-m3-openrouter-and-native-mcp.md) selects OpenRouter's native Python SDK and the official MCP v2 SDK. HTTPX bounds provider transport; JSON Schema/Pydantic validate proposals. DBOS owns scheduling and SQLite/Alembic own state. Only the Cardea authority/receipt/application integration is custom.

**Boundary.** Host-owner configuration selects the model and private credential source. A task needs explicit model-invocation, disclosure, tool and input grants under its current supervisor ceiling. Model responses are proposals, never executable commands or completion proof. Credentials stay in the broker. One installed trusted MCP server delegates incident compilation to a fixed confined worker; arbitrary servers/plugins are not admitted.

**State and failure.** Each model/tool call reserves capacity and commits intent before dispatch. A model result checkpoints before tool execution. Output versions, verification evidence and durable input waits commit together. Missing/ambiguous outcomes retain full reservations and are not replayed; reported overages are preserved and stop work. Lease recovery fences interrupted execution; host cleanup retains durable no-relaunch markers. Provider cancellation does not guarantee zero remote cost.

**Acceptance/status.** Native SDK and portable service tests cover no-retry behavior, cancellation, schema/input substitution, disclosure revocation, competing allocations, migration and checkpoint/input races. Six deterministic-provider Debian scenarios and the matched load comparison also pass. Live GLM-4.7 restart/new-input acceptance also passes; the bounded M3 profile is complete. The installed incident profile accepts one initial batch and one additive update; it does not implement general autonomous planning or multi-agent coordination. See [usage and limits](../../experiments/m3/README.md).
