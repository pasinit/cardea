# Run the Persistence Demonstration

This evidence covers the current implementation only. R04/R05 in the [review register](standards-review-2026-09-22.md) govern future storage/scheduler choices; the demo does not establish that a bespoke runtime is preferable.

This is a trusted, model-free storage demonstration. It is not the authenticated daemon, a real reasoning agent, or a Debian sandbox.

## One-Command Check

From the repository root:

```bash
make kernel-demo
```

This launches two separate Python processes with a temporary private database. The first builds an index of fictional incident records and checkpoints in `waiting`. After that process exits, the second opens the same database, resumes the task, reads the stored input/output versions, checks the index, and records completion. It reports `waiting` then `succeeded` and removes its temporary state.

## Inspect Persistent State Yourself

These commands run from `apps/api` and keep a private `.cardea` directory there:

```bash
cd apps/api
PYTHONPATH=src uv run python -m cardea.kernel.cli demo-start --task-id example
PYTHONPATH=src uv run python -m cardea.kernel.cli inspect example
PYTHONPATH=src uv run python -m cardea.kernel.cli demo-resume example
PYTHONPATH=src uv run python -m cardea.kernel.cli object example index
PYTHONPATH=src uv run python -m cardea.kernel.cli events --task-id example
```

Each invocation is a new process. Use a new task ID for another run. A repeated `demo-resume` after success returns the existing task without publishing another artifact or completion event. `--directory /absolute/private/path` before the command selects another state directory; it must be owned by the current OS user and private. The CLI does not silently change permissions on an existing shared directory.

`events` accepts `--after CURSOR` and `--limit N` for pagination. `cancel TASK_ID` cancels a task with no unresolved work; it does not kill external host processes. `recover TASK_ID` inspects an expired running lease and reconciles its stored intents conservatively. These are owner-level administration commands, not permission-filtered agent endpoints.

## What Is Proven

- Durable task snapshots, checkpoints, bounded versioned objects, and operation receipts.
- State/object changes and journal entries share a database commit.
- Competing write connections cannot both claim the same task revision.
- Wrong/expired/reused worker leases are rejected.
- Event wait registration observes an already-journaled matching event; persisted waits can be explicitly woken after restart.
- Interrupted dispatched operations remain uncertain and are never automatically replayed.
- The installed demo verifier reads actual input/output bytes and rejects changed output versions.

The store API is trusted internal code. Grant IDs and producer IDs are provenance fields, not proof of authentication. It does not enforce agent namespace permissions, model/tool budgets, host isolation, or network restrictions. There is no continuously running scheduler/event-ingestion service yet, and object reads do not yet emit their own audit events. Those guarantees must be added at the broker/service boundary before exposing this to untrusted agents.

## Tests

`make kernel-test` includes portable storage tests and subprocess crashes before/after commits. `make test`, `make lint`, and `make typecheck` include the existing demo regression checks. Host-isolation conformance has not been run on Debian during this milestone.
