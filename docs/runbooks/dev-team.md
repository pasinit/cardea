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
- `email.team.json` into the email host file: the PM's recipient alias and rate limit.

Grants expire after `grant_days` (14 by default). Run `provision` again before then.

`provision` and `apply` refuse a host file that still holds a placeholder from the example: any value ending in `.invalid` or `example`.

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
$KIT pause --host $H
```

It provisions the supervisor disabled, and nothing else; this is the first and only step, so nothing can stop it.

- Provisioning a newly disabled supervisor makes the kernel cancel every unfinished task that supervisor oversees, in the same transaction. A running session stops within seconds.
- Every member's authority is checked live against its supervisor, so no new run is admitted and no trigger fires.
- The kit lists the runs that were active, read beforehand on a best-effort basis. A kernel that cannot answer does not stop the pause.

Nothing else is changed. Collections, records, claims and mail stay as they were. While paused, `status` prints "team paused" instead of an overview, because the disabled supervisor cannot read it.

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
   - Each merged change has the PE's approval at its head, and passing checks.
   - Each open one is moving: reviewed, changed or merged within a day.
   - Nothing was pushed outside `agent/<group>/`.
4. **The PM's runs.** Each slot made an `env.try` receipt for main's head, and each failure became a work item.
5. **Credentials.** No token expires within a week (`expires_on` in the vcs host file). No grant expires before the next `provision`.
6. **The host.**
   - Memory, the swap, and disk under `/var/lib/cardea-host`.
   - Nothing stray under `cardea-host-*` units (`systemctl list-units 'cardea-host-*'`).

If anything looks wrong, pause first and investigate second.
