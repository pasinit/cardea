# ADR-017 — Native A2A delegation and shared execution ceilings

2026-09-24. Status: selected, implemented and verified for the bounded M4 delegation profile.

## Requirements and candidates

Two controlled agents on the same Debian host exchange a bounded objective through native A2A. Persist peer task/message association, preserve supervisor and ancestor authority, reserve child work within parent budgets, propagate cancellation, recover without duplicate effects, and independently verify returned artifacts. No arbitrary remote peer is admitted.

Evaluate A2A 1.0 and official Python SDK 1.1.5 (Apache-2.0, PyPI release 2026-09-21). Primary sources: [specification](https://a2a-protocol.org/v1.0.0/specification/), [SDK](https://github.com/a2aproject/a2a-python), [releases](https://pypi.org/project/a2a-sdk/1.1.5/), [database store](https://a2a-protocol.org/latest/sdk/python/api/a2a.server.tasks.database_task_store.html). Reuse native models, discovery, transport, task retrieval and cancellation; exercise the SQLAlchemy DatabaseTaskStore rather than assuming it lacks persistence. Check the installed source and executable restart fit before selecting integration.

DBOS 3.0.0, SQLite and Casbin remain selected for execution scheduling, transactional state and live authority (ADR-008/010). Their prior fits already show that cached execution steps do not establish current permission and external effects need intent/reconciliation. A replacement workflow engine is not justified by this milestone. A custom wire protocol, mailbox or task store is rejected unless measured SDK gaps require a small adapter.

## Fit and ownership decision

The two executable SDK fits below pass. The selected adapter preserves existing persisted task/receipt contracts, uses one execution owner, and exposes A2A views without independently writable duplicate lifecycle state. A2A protocol task persistence and execution checkpoint recovery are evaluated separately. New child accounting is integration data; it must be atomic with admission and bounded by the parent's original allocation, never a second supervisor allocation.

## Acceptance and limitations

Native client/server discovery, task persistence/reconstruction, duplicate/changed-message handling, cancellation, interrupted outcomes, ancestor/supervisor revocation, budget races, child failure and independent artifact checks. Run portable regressions and the bounded Debian profile; record source/dependency hashes and load/fault comparison. The [M4 report](../../experiments/m4/M4-REPORT.md) records passing evidence; general multi-agent or remote-peer support remains outside this profile.

### Initial observed fit and selection

`experiments/m4/test_sdk_fit.py`: 2 tests pass with SDK 1.1.5. SQL task records/history survive engine disposal and reconstruction; owner scopes deny another owner. A direct stale `save` overwrites a completed record with working. This is a store-level observation, not a claim that the A2A protocol permits reopening terminal tasks. Source inspection shows DatabaseTaskStore opens its own asynchronous session/commit, while DefaultRequestHandler owns in-process producer tasks. It cannot participate directly in the current synchronous DBOS/Cardea admission transaction and does not restore our host operation intents.

Select native A2A 1.0 protobuf models, JSON-RPC client/server routes, Agent Card discovery and the SDK RequestHandler extension point. Use an authenticated read-through view of existing tasks/objects; do not deploy a second writable DatabaseTaskStore for those same tasks. Existing SQLite/DBOS records remain the only execution state. A2A IDs equal the admitted child task IDs; context/message association is immutable delegation metadata. New tables only store child-slot association and bounded allocation, never a second task status. Preserve v0.1 receipts and v0.2 API records with an additive Alembic migration.

Bounded profile: installed deterministic coordinator and installed host worker agent, same private Debian service/trust domain, up to four one-level host children (hash/copy). Native A2A runs over the private Unix socket; no external URLs/cards, credentials or tools supplied by callers. Model calls are zero for this coordination profile, so it cannot mint model budgets. Arbitrary remote peers and nested coordinator plans remain unsupported. Existing ancestor policy checks and cancellation/deadline checks apply across the full stored chain. Child failure can be recorded as partial completion only when an explicit minimum-success objective is met and successful artifacts independently verify.

### Enforcement refinements

Children have no submission/resume grant, and legacy admission cannot attach unallocated descendants anywhere under an M4 coordinator. The coordinator's step ceiling is the declared child count plus one report publication; each slot consumes one child step. A failure decision awaiting worker stop is retained in the existing control/event journal so restart or transient recovery cannot resume admission after abort. This adds no competing lifecycle table. The existing kernel continues to own terminal transitions and host reconciliation.

### Dependency and maintenance evidence

The [exact-release metadata review](../../experiments/m4/evidence/final/dependency-review.json) records versions, license identifiers, upload dates and PyPI advisory responses for 13 newly introduced runtime dependencies. The additional `types-protobuf` pin is a development-only typing dependency. The runtime dependencies’ published licenses are Apache-2.0, MIT, ISC or BSD variants; SDK 1.1.5 was released three days before this review. No advisory was returned for these exact releases by PyPI at review time; this is a limited metadata check, not a security audit. The API lock pins the full transitive graph. SQLite extras are included to keep the native store fit reproducible, but that store is not installed as a competing execution owner.

### Concurrent-load refinement: async handlers

The initial two-client load attempt hit the unchanged 10-second HTTP read timeout. Both repetitions were rejected as baseline evidence; the first still stored all 24 expected object versions, while the second was stopped with 18. See [failed run](../../packages/cardea-load/reports/2026-09-24-m4-attempt1/reference/run.json). Synchronous SPIRE and SQLite calls inside the SDK's async handlers could block the server event loop under contention.

Keep all native handler methods async and await Starlette's existing `run_in_threadpool` (AnyIO worker pool) for the synchronous transactional kernel boundary. This preserves the existing single-transaction authority/admission behavior and yields the event loop; it adds no custom executor or new database owner. Discovery authentication uses the same awaited boundary. The regression test blocks authentication or admission and verifies another authenticated request completes before release. Client and peer timeouts, concurrency and workloads are unchanged. Fresh Debian acceptance and the unchanged-load reference/comparison campaign pass; see the final M4 report.

The first async Debian rerun exposed a real cancellation race: a peer rejection concurrent with an acknowledged parent cancellation could classify the parent as failed. The stop transaction now gives an existing parent cancellation precedence over that protocol error, while still waiting for children to stop. A targeted regression covers the race; the failed Debian run is retained in `experiments/m4/evidence/async-verified`. Final acceptance is recorded separately.

### Read-only snapshots under concurrent delegation

The next campaign passed both reference repetitions but one comparison inspection still hit the unchanged read timeout; all 24 output/evidence versions were present. The API and native peer read paths acquired `BEGIN IMMEDIATE`, unnecessarily reserving SQLite’s single writer. Reuse [SQLite read transactions](https://www.sqlite.org/lang_transaction.html) and [SQLAlchemy SQLite URI/transaction support](https://docs.sqlalchemy.org/en/20/dialects/sqlite.html): read-only `mode=ro` connections use `BEGIN` against the same WAL database. Authority and task/artifact reads share one short snapshot, with a fresh snapshot on each request. No cached authority or new persistence owner is introduced. Admission, cancellation, reservations and effects retain the existing immediate writer transaction. Tests must prove reads proceed while a writer holds its reservation, uncommitted changes stay invisible, subsequent reads observe revocation, and read connections cannot mutate state.
