# Events and Input Adapters

**Reuse decision.** Evaluate standard event envelopes and maintained watchers; use the selected store/scheduler for durability. Event formatting or filesystem notifications alone do not guarantee recovery. See [R08](../standards-review-2026-09-22.md#r08--events-and-ingestion--m1m3).

**Purpose.** Resume tasks when relevant state changes, including changes received during downtime.

**Design.** Journal scoped events with durable ordered cursors. Commit a checkpoint and its wait registration together, checking for matching events already after that cursor. The scheduler explicitly wakes eligible tasks for events or elapsed timers. Consumers persist progress and tolerate at-least-once delivery. Input adapters publish under scoped identities and stable source IDs.

**Boundary and recovery.** Filter reads and subscriptions by current authority. The M3 supervisor-side directory adapter rescans up to 20 bounded incident files after reconnecting. Its canonical snapshot digest is the delivery ID; the kernel commits input content, event cursor and DBOS wakeup atomically. It rejects symlinks and changed original records. There is no background watcher; its caller starts the rescan. Duplicate delivery must not repeat committed work.

**Acceptance.** Events arriving before registration, around a crash, and during downtime all lead to the appropriate durable wakeup.

**Status.** Journal, wait registration, and explicit event/timer wakeups exist in [store.py](../../apps/api/src/cardea/kernel/store.py). M1 now commits authenticated resume/cancel events together with native DBOS messages and uses native durable timers for deadlines. Early signals, retries, restart and paginated reads are tested. M3 implements one additive input delivery and directory rescan under ADR-016. Portable early/late input tests and real Debian restart-with-new-file acceptance pass. These are authenticated task commands, not exchanged event envelopes; CloudEvents mapping is deferred until an external event producer needs it.
