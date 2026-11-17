# Runbook: an agent dev team on a host

This runbook covers how to start, watch, pause, resume and stop the dev team from [`deploy/team-kit`](../../deploy/team-kit/README.md), and what to check each day. The team is four roles in one agent group (ADR-034):

| Role | Pool | Wakes on | Does |
|---|---|---|---|
| Product manager | `pm` | its schedule, twice a day; mail | reads main's head (`vcs.read_branch`), tries it with `env.try` and files failures as work items |
| Team lead | `tl` | its daily schedule; a new work item; mail | plans, splits and cancels work, and triages the next item for the developers |
| Developers | `dev` (2 by default) | an item made `ready`; an item `approved`; `request_changes`; mail | claim one item, change the code with `code.session`, push, open a pull request, submit; merge after approval |
| Principal engineer | `pe` | an item entering `in_review`; mail | reviews the pull request and approves it, or requests changes |

Every trigger refuses double work:
- **Work items.** One claim per item: of the developers an assignment wakes, only one claims the item, and each holds at most one at a time.
- **Members.** A member runs one run at a time. Events that arrive meanwhile coalesce into one follow-up.
- **Events and slots.** One event or schedule slot admits at most one run per member.

A transition that needs evidence cites a receipt. `submit` needs the run's `vcs.open_change`. `approve` needs the PE's `vcs.review` with `state == APPROVED`. `merge` needs the developer's `vcs.merge`.

## What the host needs first

- The kernel installed and running with host execution: [deploy/small-host/README.md](../../deploy/small-host/README.md), section 4.
  - The `vcs` and `try` profiles (steps 9 and 10) and `code.session` (step 8).
  - The repository alias in the registry, with its `environment` catalog.
  - The repository's rules ready: a supervisor's `vcs.check_repository` reports `ready: true`.
- `cardea-vcs-ops`, `cardea-coding-ops`, `cardea-env-ops` and, if the PM mails, `cardea-email-ops` installed, with their descriptors in `operations_dir`.
- The private host file, from `deploy/team-kit/team.host.example.json`, at for example `/etc/cardea/team.host.json` (root, `0600`). It holds the namespace, the supervisor's identity, the repository alias, the credential names, the email alias, the model and the budgets.
- The host owner's base provisioning document, without the team, at the host file's `provisioning.base`.
- A supervisor session for the kit's client: `cardea login --session <file>` with the supervisor's identity, or a SPIFFE identity named in the host file.

## Start

```sh
H=/etc/cardea/team.host.json
python3 -I deploy/team-kit/teamkit.py render --host $H --out /etc/cardea/team   # review
sudo python3 -I deploy/team-kit/teamkit.py provision --host $H                 # grants
python3 -I deploy/team-kit/teamkit.py apply --host $H                          # group
python3 -I deploy/team-kit/teamkit.py status --host $H
```

`render` also writes two fragments, which the host owner merges by hand:
- `vcs.team.json` into the vcs host file: which credential each pool uses, who may approve, and the reviewer login;
- `email.team.json` into the email host file: the PM's recipient alias and rate limit.

Grants expire after `grant_days` (14 by default). Run `provision` again before then.

## Observe

- **Status.** `teamkit.py status` prints each member's active or last run, the work items by state, and each spend account's runs and dollars against its ceiling.
- **Full overview.** `GET /v0.2/groups/<group>/overview?namespace=<ns>` adds mail, approvals and the latest journal events.
- **One run.** `cardea inspect <task>` and `cardea events <task>`.
- **Supervisor mail.** The supervisor's inbox receives the kernel's notices:
  - a run stopped on a spend ceiling;
  - a trigger refused because a ceiling is spent, once per pool and day;
  - a lapsed claim;
  - a missed windowed expectation.

## Budgets: the hard stop

The team shares the owner's model subscription, so its budgets are deliberately small:
- **The team:** 30 runs and 20 USD a day.
- **Each role:** 4 PM runs, 6 TL runs, 14 developer runs and 8 PE runs.
- **One run:** at most 1 USD.

When an account is spent:
- the next admission is refused (`budget_exhausted`);
- a run in progress stops before its next attempt when the dollar ceiling is spent;
- the supervisor gets one notice per pool and day;
- nothing runs until the account's day turns, and then the next schedule slot or event admits again.

To change a ceiling, edit `budgets` in the host file and `apply` again. The dollar figures are what the harness reports, an estimate for a subscription. The run counts don't depend on that estimate.

## Pause (the kill switch)

```sh
sudo python3 -I deploy/team-kit/teamkit.py pause --host $H
```

1. **Cancel.** It cancels every member's unfinished run.
2. **Disable.** It provisions the supervisor disabled. Every member's authority is checked live against its supervisor, so no new run is admitted, no trigger fires, and any tool call still in flight is refused at its next check.

Nothing else is changed. Collections, records, claims and mail stay as they were.

If the kit's client cannot reach the kernel, the second step alone is the kill switch: set the supervisor's `disabled: true` in the provisioning document and run `cardea provision`.

## Resume

```sh
sudo python3 -I deploy/team-kit/teamkit.py resume --host $H
```

This provisions the supervisor enabled. Triggers admit again from their next slot or event. Claims taken before the pause lapse on their lease (4 hours) if their holder does not continue.

## Stop

```sh
sudo python3 -I deploy/team-kit/teamkit.py stop --host $H
```

1. **Cancel.** It cancels the runs.
2. **Delete.** It deletes the group, so its members drain and are removed.
3. **Disable.** It disables the supervisor.

The `work` and `decisions` collections stay, for the record. Delete them with `DELETE /v0.2/collections/<name>` if wanted.

## Each day

1. **Status.** Members are idle or on one run each. No item has been `in_progress` longer than its lease. The spend accounts are under their ceilings, or the supervisor has a notice.
2. **Supervisor inbox.** Check the ceiling, claim and expectation notices.
3. **Pull requests.**
   - Each merged change has the PE's approval at its head, and passing checks.
   - Each open one is moving: reviewed, changed or merged within a day.
   - Nothing was pushed outside `agent/<group>/`.
4. **The PM's runs.** Each slot made an `env.try` receipt for main's head, and each failure became a work item.
5. **Credentials.** No token expires within a week (`expires_on` in the vcs host file). No grant expires before the next `provision`.
6. **The host.**
   - Memory, the swap, and disk under `/var/lib/cardea-host`.
   - Nothing stray under `cardea-host-*` units (`systemctl list-units 'cardea-host-*'`).

If anything looks wrong, pause first and investigate second.
