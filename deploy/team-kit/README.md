# Team kit: an agent dev team on a host

This kit builds a generic, configuration-driven dev team from [`examples/teams/dev-team`](../../examples/teams/README.md). It has four roles: a product manager, a team lead, developers and a principal engineer. They work on one repository through `code.session`, `vcs.*`, `env.try` and the kernel's records, mail and spend ceilings.

- `teamkit.py`: the `render`, `provision`, `apply`, `status`, `pause`, `resume` and `stop` commands.
- `teamsetup.py`: `plan` (dry run) and `apply`, one command from a fresh Debian host to a running team ([ADR-040](../../docs/decisions/040-team-setup-command.md)). Each step checks, acts and prints its verify result; a failed or killed run resumes at its step.
- `team.host.example.json`: the shape of the private host file. `repository` is the vcs alias the team is granted (`repo:<alias>`), and `default_branch` (default `main`) is its default branch. Both are written into the instructions of every role that uses `vcs.*`, `code.session` or `env.try`, so no role has to guess them. The repository holds no host-specific value. Copy the file outside the repository, for example to `/etc/cardea/team.host.json`, and fill it in.

Further host-file fields:
- **`run_timeouts`:** overrides each role's run timeout (defaults: pm 3600 s, tl 900 s, dev 3600 s, pe 1800 s). A run timeout must not exceed the `wall_seconds` of the confined session profile the team's sessions run under. Otherwise systemd kills the session mid-run, for example during a coding session.
  - `session_profile` (default `session`) names that profile.
  - `workers_config` (default `/etc/cardea-host-workers.json`) names the file that holds it.
  - `render` warns and `provision` and `apply` refuse when a timeout exceeds the wall. When the file is unreadable, the check cannot run and `render` says so.
- **`product`:** the product's goals, which the product manager works to: `goals` (text) or `goals_file` (a path, relative to the host file), and optional `non_goals`. Together they are at most 8 KiB of UTF-8, non-empty, with no control characters except newlines and tabs; `load_host` refuses anything else. They come from the private host file only, never from the repository; the example's goals are placeholders to replace. With goals:
  - the PM tests the product against them and files failures and gaps. It also designs and decides new features itself, without waiting for approval, and files each one as a work item stating the user problem, the proposed behaviour and acceptance criteria. Without `product`, the PM only tests and files what fails;
  - the team lead gets the goals' first lines (at most 1 KiB) to prioritise by.
- **`email`:** with an `alias` (for example `client`), the PM is granted `email.send` to that alias at the `rate_limit` (default 3 every 7 days, enforced by the email host file). Its instructions then carry the email policy:
  - only a genuine preference question the goals do not settle ("X or Y?"), short, with the options and its own choice;
  - a reply is optional input that arrives in its mailbox on a later run, and never blocks work;
  - never status reports, summaries or anything the goals already decide.

  Without `email`, neither the grant nor the policy is rendered.
- **Mail:** each role's grants are pool addresses (`mail:<group>.<pool>`). The team lead and the PE are the exception for developers: they get each developer's own address (`mail:<group>.dev.<n>`) instead of the developer pool. Each role's instructions list exactly the addresses it holds, so no role mails an address it is not granted.
- **`repositories_config`** (default `/opt/cardea-code/repositories.json`): the host's repository registry. `status` reads it only to report a waiver of the host's branch rules, described next.

- **`email.inbound`** (optional, off by default): lets the client email the PM directly. The host file's `email` gains an `inbound` key, for example `"inbound": {"rate_limit": {"count": 10, "window_days": 1}}`. `render` then writes an `inbound` section into `email.team.json`, with the alias as the only sender and the PM pool as the destination. Merge it into the email host file. The PM's instructions also gain a line: such mail is the client's input, given as data, and the PM answers with `email.send`. The checks are those of [ADR-039](../../docs/decisions/039-external-email-operations.md): an authenticated sender, each message delivered once, a size and rate limit, and rejections recorded by sender domain and reason only.

- **Developers** work in one run until nothing is left for them: merge what is approved, rework what has changes requested, then take ready items. Each holds at most two items (`MAX_ITEMS_PER_DEVELOPER`), tries the ready items from its own offset (its member number modulo their count) and moves past a start another developer won, giving up after three refusals (`MAX_START_REFUSALS`). Each pushes its own branch per claim, `agent/<group>/<record_id>-<claim_epoch>`, so it opened every pull request it merges. An item whose claim lapses in review or approved keeps its state; developers take over in-progress items no one holds, and leave an approved item no one holds to its opener. The instructions name each developer through `$member`, which the kernel fills in per replica.

- **Transitions in the instructions:** every role step that moves an item names the exact call, for example `records.transition with collection "work" and transition "triage"`. The names come from the work collection's own table (`work_transitions`), so renaming or removing a transition fails the render. The team lead is also told that a field, priority or label saying "ready" moves nothing.

How to run it day to day is in the [runbook](../../docs/runbooks/dev-team.md).

## Repositories without host rules

On some host plans the host's rulesets and branch protection are unavailable; GitHub, for example, answers the rules API with 403 "Upgrade to GitHub Pro" for a private repository on a free plan. The host owner may then waive the host-side rules for that repository in the registry:

```json
"<alias>": {"...": "...", "host_rules": "waived", "waiver_reason": "<why, at least 8 characters>"}
```

- The default is `required`. A waiver needs a reason, and only a repository that `vcs.*` acts on can take one.
- With the waiver, `vcs.check_repository` reports `ready: true` with `host_rules: "waived"`, the reason, and the host-side checks it gave up (`waived_checks`). The `vcs.push_branch` and `vcs.merge` receipts carry `host_rules` and the reason, and `status` prints a line saying so.
- Cardea's own guards are unchanged: the agent branch prefix on every push, the content gate, and `vcs.merge`'s conditions (the opener, the PE's approval receipt at the head, the reviewer login's approval on the host, every check green, mergeable).
- What the waiver gives up: the host no longer stops a direct push to the default branch made with the repository token. The token must therefore stay with the kernel's vcs workers, and no person or agent may hold it. See the [ADR-037 amendment](../../docs/decisions/037-coding-and-vcs-operations.md).
