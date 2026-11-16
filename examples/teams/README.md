# Teams as configuration: a software team and a trading desk

These are the two worked examples of [ADR-034](../../docs/decisions/034-agent-groups-and-shared-records.md), as files. Neither needs Cardea code of its own. Each is a composition of:
- the kernel's generic primitives: collections, records with state machines and claims, agent groups, approval waits, mailboxes, context sources and event triggers;
- the configuration documents in this directory;
- an operations package. Here that is the fake [`cardea-reference-ops`](../../packages/cardea-reference-ops/README.md), which has no network.

```sh
make install
make teams
```

`make teams` runs both examples on a throwaway kernel in a few seconds and narrates each step. `make teams-test` checks that it still does.

## What the walkthrough shows

**Software team** (`software-team/`):
1. Product files a work item, which starts `open`.
2. The lead assigns it to the `dev` pool and triages it to `ready`. The transition guards allow only that pool to start it.
3. A developer claims it with `start`: the claim gives it a lease and a fencing epoch. The developer opens a change and submits the item. `submit` needs the receipt of `vcs.open_change` from the same run.
4. The principal approves it to `done`. A guard stops anyone from approving work they hold.

**Trading desk** (`trading-desk/`):
1. An analyst checks risk and proposes an order.
2. The risk agent asks to approve it. `approve` is gated by an ADR-036 approval, so the run waits, durably, until a human desk approver decides. The held transition then runs.
3. Execution places the order. Its first attempt dies after placing. The retry's placement is deduplicated by the operation's effect scope, so the broker sees one order.
4. The broker reports the fill as a signed webhook (`trigger.fills.json`, an ADR-035 event trigger). The webhook wakes the execution member, which records the fill.
5. Execution reads the fills and reconciles the order, releasing its claim.

The tests in `apps/api/tests/service/test_worked_examples.py` cover more paths:
- a rejected or expired approval;
- refused guards;
- review rounds;
- the broker's risk-limit refusal.

## The files

| File | What it is | Applied with |
|---|---|---|
| `*/collection.*.json` | `kind: Collection`: schema, claims, states and transitions | `PUT /v0.2/collections/{name}` |
| `*/group.json` | `kind: AgentGroup`: pools, replicas, grants, run templates, triggers | `PUT /v0.2/groups/{name}` (add `?dry_run=true` to see the plan) |
| `trading-desk/trigger.fills.json` | the broker's inbound webhook; inbound event triggers are host-owned | host provisioning (`triggers`) |

Each pool's `grants` are its runs' complete grants, and the supervisor must be able to delegate every one of them. Event triggers on pools read the group's own kernel journal: mail, record and approval events. `$member` and `$pool` stand for each replica.

## What is not real

- **Agents:** in the walkthrough, agents are short scripts that call their tools over each run's real MCP endpoint. On a daemon, a configured harness and model play them.
- **Identities:** fixed test credentials.
- **Execution:** runs execute one step at a time in-process.
- **Operations:** every operation is a fake, and its in-memory state disappears with the process.
