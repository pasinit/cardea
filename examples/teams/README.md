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
5. The supervisor reads the group's overview, `GET /v0.2/groups/eng-team/overview?namespace=eng`, from one read snapshot. It shows each pool's members and their last runs, each collection's counts and claims, mail and approvals, spend, and the latest journal events.

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

## Dev team: the milestone 1 dress rehearsal (`dev-team/`, `dev_team.py`)

The same kind of software team, run on the **real** operation packages with fake backends instead of the reference fakes:
- `cardea-coding-ops`: `code.session` in its confined-worker shape, with a fake harness and fake gates on a local bare repository. The helper is replaced by a local process that runs the real worker command.
- `cardea-vcs-ops`: `vcs.*` against the in-process fake GitHub, with the test-only token.
- `cardea-email-ops`: `email.send` for the product manager only, through the AgentMail adapter against the in-process fake AgentMail ([ADR-039](../../docs/decisions/039-external-email-operations.md)). `dev-team/email.json` is the host configuration's shape, with the placeholder alias `client`, a placeholder address and a limit of 3 emails a week. Real addresses live only in the host's private configuration. The product manager owns feature design: features and bugs go straight to the backlog, and a client email is one short preference question that no ticket waits on. The rehearsal sends one question. The fake client and a stranger both reply, and the PM's next run reads only the client's reply, as untrusted mail. The stranger's reply is dropped and counted.
- The kernel's content gate, with a stand-in scanner and a denylist.

One group manifest (`dev-team/group.json`) holds a principal engineer, a product manager, a team leader and three developers. It also sets a group spend account and a developer pool account. Developer runs carry `expect: [{any_of: [vcs.open_change, mailbox.send to the team lead]}]`. Each run is built from the member's own trigger template, so its operations, context, bounds and expectations are the pool's.

1. **Filing and assignment.** The product manager files two work items from a scripted test pass. The team leader assigns each to the developer pool and mails the pool.
2. **One developer's whole run.** dev.0 claims an item with `start`, then calls `code.session`. It passes the session's `change.bundle` artifact straight to `vcs.push_branch`, then `vcs.open_change`, and `submit`s with that call's receipt as evidence (`requires_receipt`).
3. **A refused double claim.** dev.1 tries to take the same item. The claim refuses it, so dev.1 mails the lead, which also meets its `any_of`.
4. **Review.** The principal engineer reads the change, submits an `APPROVE` review on the host and approves the item. The rehearsal's vcs configuration allows `APPROVE` for the `dev-team.principal` pool only (`allow_approve`, ADR-037 question 2). No agent can merge: the owner's merge is simulated outside the team.
5. **The content gate.** dev.2's change carries a planted secret, and the content gate refuses the push with nothing sent. dev.2 tells the lead it is blocked.
6. **Overview and checks.** The script prints the group overview, including spend. It checks that no claim was ever taken while another was held, that every run has receipts and a reported cost, and that the gate refused exactly one change.
7. **Restructure by configuration only.** `dev-team/group.restructured.json` has two developers and a `security` pool. Applying it drains dev.2, which releases its claim, so the blocked item goes back to `ready`. One new item then goes dev → security review → principal → done. The security pool holds `vcs.review` too, but its `APPROVE` is refused: only the principal pool may approve.

Found by the rehearsal, recorded in ADR-037:
- `requires_receipt` ties evidence to a record through the cited call's `record_id` argument, or through a `<record>.<epoch>` idempotency context that only claim follow-up runs have. The real `vcs.open_change` had no `record_id`, so a developer could never `submit`.
  - `vcs.open_change` and `vcs.push_branch` now take an optional `record_id`, recorded in the receipt and never sent to the host. Their descriptors are at version 2.
- `code.session` declared no `repo:<alias>` resource grant, unlike `vcs.*`. *Resolved:* it now needs `repo:<alias>` (descriptor version 2).
- The coding and vcs packages each kept their own repository aliases. *Resolved:* one host registry, `CARDEA_REPOSITORIES`, holds each alias's mirror source, host, gates and branch prefix, and both packages read it. The rehearsal writes one `repositories.json` for both.

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
