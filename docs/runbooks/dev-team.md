# Runbook: an agent dev team on a host

This runbook covers how to start, watch, pause, resume and stop the dev team from [`deploy/team-kit`](../../deploy/team-kit/README.md), and what to check each day. The team is five roles in one agent group (ADR-034):

| Role | Pool | Wakes on | Does |
|---|---|---|---|
| Product manager | `pm` | its schedule, twice a day; mail | reads main's head (`vcs.read_branch`) and the open pull requests (`vcs.list_changes`), tries it with `env.try` and files failures as work items |
| Team lead | `tl` | its daily schedule; a new work item; mail | plans, splits and cancels work, and triages the next item for the developers |
| Developers | `dev` (2 by default) | an item made `ready`; an item `approved`; `request_changes`; mail; an optional schedule (`schedules.dev`) | claim a ready item, change the code with `code.session`, push, open a pull request, submit (with `env_try.dev_scenarios`, such as `["check"]`, only after `env.try` passes on the pushed head); merge after approval; then take the next ready item in the same run |
| Principal engineer | `pe` | an item entering `in_review`; mail; an optional schedule (`schedules.pe`) | reviews the pull request and approves it, or requests changes |
| UX designer | `ux` | an item entering `in_review`; an item made `ready`; mail; an optional schedule (`schedules.ux`) | owns the design of every page: writes the approved mock (phone 390px and desktop) into UI items, files UX items with screenshots, and reviews every pull request that changes the UI. It requests changes unless it saw the page rendered at both widths and it matches the approved mock; otherwise it posts a `UX REVIEW: PASS <head sha>` review. The PE does not approve a UI pull request before that review |

Every trigger refuses double work:
- **Work items.** One claim per item: of the developers an assignment wakes, only one claims the item. Each developer holds at most two, so one can wait in review while it works on the next.
  - Developers wake together, so each tries the ready items from its own offset: developer `n` starts at position `n mod k` of the `k` ready items, in `records.query` order. A start refused because another developer claimed the item first is normal; the developer tries the next and gives up after three refusals.
  - A developer does not end its run after a merge or a submit while an item is ready, and it does not stop on a lost start.
- **Members.** A member runs one run at a time. Events that arrive meanwhile coalesce into one follow-up.
- **Events and slots.** One event or schedule slot admits at most one run per member.

Mail to a pool wakes every member of that pool. So the team lead and the PE hold no grant to mail the whole developer pool. They answer about an item by mailing its holder at the holder's own address (`<group>.dev.<n>`); the kit grants one address per developer. A developer that holds no item and finds none ready stops without sending mail.

Only the member that opened a pull request may merge it (`merge_not_opener`). When a developer's claim lapses (its lease and follow-ups run out, or the team is paused), what happens depends on the item's state:
- **In progress:** the item returns to `ready` and the next developer claims it at a new epoch.
- **In review or approved:** the item keeps its state and its review, and only the holder is dropped. The PE still reviews it. If the PE requests changes, the item is in progress with no holder, and any developer may `start` it. Approved, it waits for the developer that opened its pull request, which merges it without holding it; any other developer's `vcs.merge` is refused with `merge_not_opener`, and it leaves the item alone. If that developer is gone, the team lead cancels the item and files it again.
- `data.change` stays in every case, so a developer taking an item over pushes to its own branch per claim, `agent/<group>/<record_id>-<claim_epoch>`, and opens its own pull request. It may start its coding session from the earlier branch.

A transition that needs evidence cites a receipt. `submit` needs the run's `vcs.open_change`, and it copies the pull request number from that call onto the item as `data.change`, where the PE finds it. `approve` needs the PE's `vcs.review` with `state == APPROVED`, and `request_changes` needs one with `state == CHANGES_REQUESTED`. `merge` needs the developer's `vcs.merge`. Each of these calls passes the item's `record_id`.

## What the host needs first

`teamsetup.py plan --host $H` checks every item below and the setup steps that follow from them; `apply` does what it can. The list stays the reference. See [team kit](../../deploy/team-kit/README.md).

- The kernel installed and running with host execution: [deploy/small-host/README.md](../../deploy/small-host/README.md), section 4.
  - The `vcs` and `try` profiles (steps 9 and 10) and `code.session` (step 8).
  - The repository alias in the registry, with its `environment` catalog.
  - The repository's rules ready: a supervisor's `vcs.check_repository` reports `ready: true`.
    - On a host plan without rulesets (the rules API answers 403), the report says `rules_available: false` and the repository is not ready. The host owner can waive the host-side rules for that repository in the registry (`host_rules: "waived"` with a `waiver_reason`). The report is then `ready: true`, with `host_rules: "waived"`, the reason and the `waived_checks`. `teamkit.py status` prints the waiver on every call. With a waiver, also set `"require_up_to_date": true` on the alias, so `vcs.merge` refuses a branch behind the base (`merge_behind_base`); the report's `require_up_to_date` and `status` show whether it is on.
    - Waiving gives up the host's own stop on a direct push to the default branch made with the repository token. Keep that token in the vcs worker's credential only. Cardea's guards, the agent prefix, the content gate and `vcs.merge`'s conditions, are unchanged. See [team kit](../../deploy/team-kit/README.md#repositories-without-host-rules).
- `cardea-vcs-ops`, `cardea-coding-ops`, `cardea-env-ops` and, if the PM mails, `cardea-email-ops` installed, with their descriptors in `operations_dir`.
- The private host file, from `deploy/team-kit/team.host.example.json`, at for example `/etc/cardea/team.host.json` (root, `0600`). It holds the namespace, the supervisor's identity, the repository alias, the credential names, the email alias, the model, the budgets and the product's goals (`product.goals` or `product.goals_file`, optional `non_goals`, at most 8 KiB together). Replace the example's placeholder goals: the PM decides features from them.
- The host owner's base provisioning document, without the team, at the host file's `provisioning.base`.
- The supervisor's identity for the kit's client: on one host, the SPIFFE ID below. A `cardea login --session <file>` OIDC session also works where a real OIDC provider exists.

## Identity and the kernel service on one host

The kit needs a running kernel and a supervisor identity the kernel accepts. On one dev host, use the production path, **SPIRE**, with the server and the agent on the same host. This is what the small-host kit already does ([deploy/small-host/README.md](../../deploy/small-host/README.md), sections 2 and 3, without the Telos host). It is the path fit for a week-long unattended run: SVIDs rotate on their own and nothing expires mid-week.

The OIDC test issuer the acceptance scripts use (`experiments/debian-acceptance/oidc_issuer.py`) is **not** fit for this. It is a throwaway stand-in: it has no persistence, its keys live in memory, and it must be restarted by hand. Use it only inside an acceptance run.

Steps, as root, on the dev host:

1. **SPIRE.** Follow small-host sections 2.1–2.4 for one node, `spiffe://cardea.internal/node/cardea`. You need only the Cardea node's join token.
2. **Register the kernel and the team supervisor.** Both use the `cardea-kernel` account. The kit talks to the kernel's private socket, `/var/lib/cardea/kernel.sock`, which only that account can open, so the kit runs as it (`sudo -u cardea-kernel`). Anything else running as `cardea-kernel` could also ask for the supervisor's SVID. Confined workers run as `cardea-worker`, so they cannot.

   ```sh
   S=/opt/spire/bin/spire-server; A="-socketPath /run/spire-server/api.sock"
   $S entry create $A -parentID spiffe://cardea.internal/node/cardea \
     -spiffeID spiffe://cardea.internal/runtime/kernel -selector unix:user:cardea-kernel
   $S entry create $A -parentID spiffe://cardea.internal/node/cardea \
     -spiffeID spiffe://cardea.internal/supervisor/dev-team -selector unix:user:cardea-kernel
   ```

3. **The kernel.**
   - Use `deploy/small-host/cardea/config.confined.json` as `/etc/cardea/config.json` (`0600`, `cardea-kernel`). Its `spiffe_socket`, `trust_domain` and `runtime_spiffe_id` match step 2.
   - Leave out the `network` listener unless something off the host must reach the kernel. The kit uses the Unix socket.
   - Then follow small-host section 3: steps 1, 2, 5 and 6, and step 7's `init`.
4. **The base provisioning document.** Write `/etc/cardea/provision.base.json` (`0600`, `cardea-kernel`) with:
   - the `runtime` service principal (`auth_kind: spiffe`, `issuer: cardea.internal`, `subject: spiffe://cardea.internal/runtime/kernel`);
   - no other team rows.

   The kit adds the team supervisor, `spiffe://cardea.internal/supervisor/dev-team`, and its grants when you run `provision`.
5. **Confined workers and the operations.** Follow small-host section 4, including steps 8–10, so the operations below are installed.
   - The `coding` profile's credential is `/etc/cardea/credentials/claude-code`.
   - The `vcs` profile's named credentials map each token name to its file. On a host whose files are `/etc/cardea/credentials/github` and `/etc/cardea/credentials/github-reviewer`, use `{"github": ".../github", "github-reviewer": ".../github-reviewer"}`, and set the host file's `credentials.author` to `github` and `credentials.reviewer` to `github-reviewer`.
   - The email configuration's AgentMail key file is the one the email host file names, for example `/etc/cardea/agentmail.key` (`0600`, `cardea-kernel`).
6. **Enable the service:** `systemctl enable --now cardea-kernel`, then check `systemctl status cardea-kernel` and the socket's presence.

Every `teamkit.py` command below runs as `cardea-kernel`, with the kernel's Python: `sudo -u cardea-kernel /opt/cardea/apps/api/.venv/bin/python -I deploy/team-kit/teamkit.py ...`. `provision` writes under `/etc/cardea`, so give that account write access to the document it writes (`provisioning.out`), or run `provision` as root with the same Python.

## Start

```sh
H=/etc/cardea/team.host.json
KIT="sudo -u cardea-kernel /opt/cardea/apps/api/.venv/bin/python -I /opt/cardea/deploy/team-kit/teamkit.py"
$KIT render --host $H --out /etc/cardea/team   # review
$KIT provision --host $H                                # grants
$KIT apply --host $H                                    # group
$KIT status --host $H
```

`render` also writes two fragments, which the host owner merges by hand:
- `vcs.team.json` into the vcs host file: which credential each pool uses, who may approve, and the reviewer login;
- `email.team.json` into the email host file: the PM's recipient alias and rate limit. With `email.inbound` set in the host file, the fragment also holds the `inbound` section: the client's new mail to the team's inbox goes to the PM pool and wakes it. To check what was rejected, run `SELECT json_extract(payload,'$.reason'), json_extract(payload,'$.sender_domain'), COUNT(*) FROM events WHERE event_type='operation.inbound_dropped' GROUP BY 1,2`.

Grants expire after `grant_days` (14 by default). Run `provision` again before then.

`provision` and `apply` refuse a host file that still holds a placeholder from the example: any value ending in `.invalid` or `example`.

## Observe

- **Status.** `teamkit.py status` prints each member's active or last run, the work items by state, and each spend account's runs and dollars against its ceiling. It also lists when the vcs tokens expire (names and dates only), flagged `EXPIRES SOON` within `credentials.warn_days` (default 7) of `expires_on` and `EXPIRED` after it, and the last failed runs (member, failure code, task id, time).
- **Full overview.** `GET /v0.2/groups/<group>/overview?namespace=<ns>` adds mail, approvals and the latest journal events.
- **Status API.** Read-only, paged JSON for the supervisor: `GET /v0.2/groups/<group>/status/{work,runs,mail,changes,spend,standing}?namespace=<ns>` gives work by state and holder, runs with cost and outcome, mail between roles, pull requests with review state, spend against each ceiling, and what is paused or waived. `changes` is what the kernel's receipts record, not the host's live view.
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
$KIT pause --host $H
```

It provisions the supervisor disabled, and nothing else; this is the first and only step, so nothing can stop it.

- Provisioning a newly disabled supervisor makes the kernel cancel every unfinished task that supervisor oversees, in the same transaction. A running session stops within seconds.
- Every member's authority is checked live against its supervisor, so no new run is admitted and no trigger fires.
- The kit lists the runs that were active, read beforehand on a best-effort basis. A kernel that cannot answer does not stop the pause.

Nothing else is changed. Collections, records, claims and mail stay as they were. While paused, `status` prints "team paused" instead of an overview, because the disabled supervisor cannot read it. It still prints the pause reason (`pause --reason "..."`, one plain line, no secrets), the token expiries and the last failures, all from a small record (`<group>.pause.json`, mode 0600) the kit writes beside the provisioning file at pause time and removes on `resume`.

If the kit itself cannot run, the same kill switch is to set the supervisor's `disabled: true` in the provisioning document and run `cardea provision`.

## Resume

```sh
$KIT resume --host $H
```

This provisions the supervisor enabled. Triggers admit again from their next slot or event. Claims taken before the pause lapse on their lease (4 hours) if their holder does not continue.

## Stop

```sh
$KIT stop --host $H
```

1. **Delete.** It deletes the group, so its members drain and are removed. This needs the supervisor's authority, so it comes first.
2. **Disable.** It always disables the supervisor, even if the deletion failed, and that cancels whatever is still running. If the deletion failed, `stop` says so and exits 1: `resume`, then delete the group.

The `work` and `decisions` collections stay, for the record. Delete them with `DELETE /v0.2/collections/<name>` if wanted.

## Each day

1. **Status.** Members are idle or on one run each. No item has been `in_progress` longer than its lease. The spend accounts are under their ceilings, or the supervisor has a notice.
2. **Supervisor inbox.** Check the ceiling, claim and expectation notices.
3. **Pull requests.**
   - Each merged change has the PE's approval at its head, and passing checks. With `env_try.merge_scenarios` in the host file (default `try`), the checks are a successful `env.try` receipt for the head SHA; `merge_checks_missing` means none exists.
   - Each open one is moving: reviewed, changed or merged within a day.
   - Nothing was pushed outside `agent/<group>/`.
4. **The PM's runs.**
   - Each slot made an `env.try` receipt for main's head, and each failure became a work item.
   - Each feature item states a user problem, the proposed behaviour and acceptance criteria within the goals.
   - Only the PM and the UX designer email the client. Any such email is one short preference question ("X or Y?") with the options and the team's pick, never a status report. The host's `email.rate_limit` (default 10 a day) bounds it. A reply arrives in the sender's mailbox and is optional; work never waited for it.
   - To change direction, edit `product` in the host file and `apply` again.
5. **Credentials.** No token expires within a week (`expires_on` in the vcs host file). No grant expires before the next `provision`.
6. **The host.**
   - Memory, the swap, and disk under `/var/lib/cardea-host`.
   - Nothing stray under `cardea-host-*` units (`systemctl list-units 'cardea-host-*'`).

If anything looks wrong, pause first and investigate second.
