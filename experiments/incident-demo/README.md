# Incident demo: an agent run with an installed operation

The fictional incident example used to be a kernel feature: the M3 profile had its own
`POST /v0.2/agent-tasks` API, a fixed `cardea-incidents` MCP tool and an OpenRouter model
broker (ADR-016). [ADR-032](../../docs/decisions/032-remove-incident-agent-tasks.md) removed
that path. Agent runs (ADR-027) with installed operations (ADR-023) are now the one way to run
an agent, and this directory shows the same example built that way:

- `incidents.compile.json`: the operation descriptor (tool schema, read-only effect, timeout);
- `operation.py`: its handler, ordinary application code;
- `incidents.py`: the fictional record schema and an independent output verifier;
- `test_demo.py`: an agent run whose only tool is `incidents.compile`. A scripted session plays
  the model over the run's real MCP endpoint. The second test crashes the run after the call
  settled and shows the next attempt resuming from the receipt journal.

Run it with `make incident-demo-test`. On a real host, install the descriptor in the kernel's
`operations_dir` and the handler as a `cardea.operations` entry point, then submit an
`AgentRunRequest` (`python -m cardea.service submit-agent-run`).
