# ADR-024: Time Triggers

- **Date / owner / milestones:** 2026-10-15; runtime maintainers; ADR-022 gap #3 (capability C2, G5).
- **Status:** selected and implemented. Portable tests pass. The DBOS clock was checked against the pinned DBOS 3.0.0 on SQLite and starts inside the real `make try` daemon. No test runs a trigger end to end through the live daemon yet, and nothing has been tested on Debian.
- **Requirement:** a host-owner-installed trigger admits one task per scheduled slot. The slot identity (trigger ID, scheduled instant) deduplicates admission across crashes and restarts. Each trigger declares an IANA timezone, which must be DST-correct, and a misfire policy for slots missed during downtime: `skip`, `run_once_late` or `grace`. Disabling or removing a trigger stops future admission. Every admission is checked against the supervisor's live authority ceiling. A refusal is recorded and does not stop the scheduler. There is one scheduler owner: DBOS already owns dispatch, so no second queue is added.

## Candidates and primary evidence

Checked 2026-10-15 (macOS, Python 3.13).

| Candidate | Version, license, maintenance | Fit evidence |
|---|---|---|
| DBOS scheduled workflows (`DBOS.create_schedule` / `apply_schedules`) | `dbos==3.0.0` (pinned, MIT), source read in `.venv/.../dbos/_scheduler.py` and `_dbos.py` | **Clock: fits.** Schedules are rows in the DBOS system database, in the same SQLite file as Cardea. A per-schedule thread enqueues workflow ID `sched-<name>-<iso time>`, so a slot is enqueued once. `apply_schedules` is an upsert. A local probe applied the same schedule twice with a one-second cron on SQLite: it fired 3 times in 5 s with 3 distinct slots. **Per-trigger semantics: do not fit.** (1) Misfire handling is only `automatic_backfill`, which enqueues *every* missed slot from `last_fired_at`, or nothing. There is no run-once-late and no grace window. (2) DST: `cron_timezone` evaluation in the vendored `dbos/_croniter.py` is wrong. For `0 30 8 * * *` in Europe/London it produced 2026-03-29T06:30Z and 07:30Z (two runs, the first at 07:30 local), and 2026-10-25T09:30Z (09:30 local). For `0 30 1 * * *` it produced 2026-10-26T02:30Z. (3) The scheduled workflow receives only `(scheduled_at, context)`, so authority, template validation and the refusal record would still be Cardea code. (4) One DBOS schedule per trigger means one thread per trigger and a second copy of trigger state outside Cardea's transaction. |
| Cron expressions via [`croniter`](https://github.com/pallets-eco/croniter) | 6.2.4 (2026-07-10), MIT, maintained under the Pallets Community Ecosystem; depends only on `python-dateutil`, already in the lock | **Fits for expansion.** Tested on naive local times. With timezone-aware input, 6.2.4 gets 08:30 London right across both transitions. It runs a repeated local time twice (01:30 on 2026-10-25 gave 00:30Z and 01:30Z) and maps the skipped 01:30 to 01:00Z. Because of the double run, it is used only on the naive local wall clock, and Cardea does the local-to-UTC mapping itself (below). Pinned `croniter==6.2.4`. |
| systemd timers ([systemd.timer(5)](https://man7.org/linux/man-pages/man5/systemd.timer.5.html), [systemd.time(7)](https://man7.org/linux/man-pages/man7/systemd.time.7.html)), page for systemd 262~devel | LGPL-2.1, maintained; on Debian only | `OnCalendar=` accepts a timezone, and `Persistent=true` triggers immediately "if it would have been triggered at least once" while inactive. That is close to `run_once_late`. **Does not fit:** there is no skip-versus-grace window (`AccuracySec=` and `RandomizedDelaySec=` coalesce or spread firings; they are not misfire policy). Each trigger needs a root-installed unit and an activated process that holds a credential to call admission, which adds a second scheduler beside DBOS. It is Debian-only, and the portable tests could not exercise it. Not tested. |
| APScheduler (Telos's current scheduler) | Listed in ADR-022 | Not evaluated beyond the requirement: it is a second in-process scheduler with its own job store, which violates the single-owner requirement. |

## Decision

Compose them. **DBOS owns the clock, croniter expands the expression, and Cardea owns slot state and admission.**

1. **Clock.** One DBOS schedule, `cardea-triggers`, runs the workflow `cardea.m6.triggers` at second 0 of every minute (`0 * * * * *`, UTC, no backfill). The workflow has one non-retried step, `Scheduler.tick()`. No other thread, timer or queue exists. A lost tick costs nothing: the next tick evaluates every slot since each trigger's last decided slot.
2. **Expression.** A five-field POSIX/Vixie cron expression (`minute hour day-of-month month day-of-week`). Seconds, `H`, `L`, `#` and other croniter extensions are refused, and so are expressions that never fire, such as February 30. Expansion runs on the trigger's local wall clock.
3. **Timezone and DST** (`service/schedule.py`). The mapping to UTC uses `zoneinfo`:
   - a local time that occurs twice runs once, at its first occurrence (PEP 495 `fold=0`);
   - a local time that does not exist runs once, when the gap ends (01:30 on 2026-03-29 London runs at 01:00Z = 02:00 BST, the same as cronie and croniter);
   - local times that map to the same instant are one slot.
   Daily 08:30 London gives 08:30Z before and 07:30Z after the last Sunday of March, and the reverse around the last Sunday of October.
4. **Misfire.** A tick decides the newest slot in `(max(anchor, last_slot), now]`.
   - A slot at most 120 s old is on time under every policy. That covers the 60 s tick plus DBOS jitter (at most 10% of the sleep).
   - For an older slot:
     - `skip` admits nothing;
     - `run_once_late` admits that newest slot only;
     - `grace` (60–86400 s) admits it only if its age is at most `grace_seconds`.
   - Older missed slots are never admitted, under any policy. They are reported as one `missed` event with the exact count up to 1000, the first and the last slot.
   - `grace` therefore runs at most the most recent missed slot. It never replays a backlog.
5. **Slot identity and atomicity.** The slot request ID is `<trigger_id>.<YYYYMMDDTHHMMSSZ>`. A trigger ID has at most 111 characters, so the request ID matches `Identifier`. One `BEGIN IMMEDIATE` transaction does four things: it re-reads the trigger, checking that its definition digest, anchor and `last_slot` are unchanged; it admits through `Kernel.admit_scheduled`; it appends the `admitted` event; and it advances `last_slot`. A crash before the commit leaves nothing behind. A crash after it leaves the slot decided. `UNIQUE(trigger_id, slot)` on the history and `UNIQUE(supervisor_id, request_id)` on task bindings are independent guards, and a repeated admission of the same slot returns the existing task. For `operation` templates the slot ID is also the request's `idempotency_context` (ADR-026), so effects within one slot dedupe across retries.
6. **Authority.** The scheduler holds no supervisor credential. `Kernel.admit_scheduled` runs inside the tick's transaction. It authenticates the kernel's own runtime service (`auth.runtime_proof()` must resolve to the kernel's runtime principal), and it takes the supervisor from the host-owner-installed trigger row. It then runs the shared admission body of `Kernel.submit` unchanged:
   - registry and argument validation;
   - `policy.admission(supervisor, …)` against live policy, which checks `tasks.submit` and every grant in both `allow` and `delegate` modes plus platform policy;
   - the agent-to-runtime binding;
   - budgets and capacity.
   A disabled supervisor, a revoked grant, an uninstalled operation or an invalid template is refused like an HTTP submission. The refusal rolls back, and a separate transaction records a `failed` event (with its code) and closes the slot, so that slot is not retried every minute. The tick then continues with the other triggers. The `task.bound` event carries `trigger_id` and `slot`.
7. **Installation.** Triggers are part of the host-owner `Provisioning` document (`cardea provision`), never an agent or HTTP path. Each trigger holds:
   - `trigger_id` and `supervisor_id`, which is immutable per trigger;
   - `schedule`, `timezone`, `misfire`, `grace_seconds` and `enabled`;
   - `template_kind`, one of `task`, `host`, `host-object`, `operation`, `delegation` or `agent`;
   - `template`, a complete request of that kind without `request_id`, `idempotency_context` or `parent_task_id`.
   The template is validated at install and again at every admission. As with policies, the document is the complete set: a trigger left out of it is marked removed, and its history is kept. Re-enabling a trigger, or re-adding a removed one, resets its anchor, so slots while it was disabled are not caught up.
8. **Inspection.** `GET /v0.2/triggers` (read-only, paginated by `after`/`limit`, `history` ≤ 50) lists the caller's own triggers as supervisor, filtered by `tasks.inspect` in the trigger's namespace, with `last_slot` and recent events.

## Integration

- **State.** Migration `m6_002` (file `m6_002_triggers.py`, `down_revision = "m6_001"`; chain `m5_001 → m6_001 → m6_002`) adds two tables:
  - `triggers`: removal is a flag, and deletion is refused;
  - `trigger_events`: append-only, with kinds `installed`, `updated`, `enabled`, `disabled`, `removed`, `admitted`, `failed` and `missed`.
  `SCHEMA_HEAD` is now `m6_002`.
- **Compatibility.** `Kernel.submit` behavior is unchanged. Its body is factored into `_precheck` and `_admit`, which the trigger path shares. Existing provisioning documents have no triggers. Applying one to a store that has triggers removes them, which is the same replacement semantics as policies and budgets.
- **Snapshot restore.** Restore disables principals and seals namespaces, so triggers copied from a snapshot record `principal_disabled` or `restored_scope_sealed` failures until the host owner provisions them again.
- **Cost.** One DBOS workflow row per minute in the system database. DBOS workflow retention is unchanged by this record.
- **Dependency.** `croniter==6.2.4`, MIT. `python-dateutil` was already present.

## Verification

`apps/api/tests/service/test_triggers.py` (34 tests, portable, no real DBOS) covers:
- one admission per slot under repeated ticks;
- a crash after slot computation, inside admission before its commit, and after the commit, each followed by a re-tick, with no duplicate;
- repeated scheduled admission of the same slot returning the same task;
- three days of downtime under `skip`, `run_once_late`, and `grace` with windows of 1800 s and 600 s;
- 08:30 Europe/London daily across 2026-03-29 and 2026-10-25, ticking also at the other offset's 08:30;
- local times that do not exist or occur twice (01:30);
- disabling, re-enabling without catch-up, and removal;
- one broken trigger not stopping another;
- revoked grants (`authority_denied`) and a disabled supervisor (`principal_disabled`) recorded without crashing;
- template revalidation at admission (`invalid_arguments`, `operation_not_installed`);
- refusal of invalid definitions;
- the inspection route.

`make lint`, `make typecheck` and `make test` pass (2026-10-15, macOS, Python 3.13, DBOS 3.0.0); `make test` includes `try-test`, where the real daemon applies the schedule at launch. Not run: an end-to-end tick admitting a task inside the live daemon, a restart spanning a slot on Debian, and any systemd timer test.

## Revisit

Revisit if:
- DBOS adds misfire policies and correct DST handling;
- a workload needs sub-minute schedules or backlog replay;
- triggers need per-trigger catch-up limits other than the newest slot;
- DBOS workflow retention makes one tick per minute too costly.
