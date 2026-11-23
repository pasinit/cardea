# Team kit: an agent dev team on a host

This kit builds a generic, configuration-driven dev team from [`examples/teams/dev-team`](../../examples/teams/README.md). It has five roles: a product manager, a team lead, developers, a principal engineer and a UX designer. They work on one repository through `code.session`, `vcs.*`, `env.try` and the kernel's records, mail and spend ceilings.

- `teamkit.py`: the `render`, `provision`, `apply`, `status`, `pause`, `resume` and `stop` commands.
- `dashboard.py`, `dashboard.html`, `dashboard.js`: the read-only team dashboard, below.
- `dashboard_settings.html`, `dashboard_settings.js`: the settings page of the teams dashboard (`/settings`), below.
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
- **`repositories_config`** (default `/opt/cardea-code/repositories.json`): the host's repository registry. `status` reads it only to report a waiver of the host's branch rules and whether merging needs an up-to-date branch, described next.

- **UX designer** (`ux` pool): owns the design of every page, files UX items with screenshots, and reviews every pull request that changes the UI by rendering the head at phone (390 px) and desktop width and comparing it with the approved mock version (recorded as a `decisions` item). It submits a review starting `UX review: APPROVED` or `UX review: CHANGES REQUESTED` (a `vcs.review` with the reviewer credential; it cannot move items). The principal engineer may not approve a UI pull request without an approving UX review on its current head. `env_try.ux_scenarios` (default: `scenarios`) are the scenarios it renders with.
- **`env_try`** (optional): `scenarios` (default `["try"]`) are the `env.try` scenarios the roles run. `merge_scenarios` (default `["try"]`; `[]` turns the check off) are the ones that must pass on a pull request's head before `vcs.merge`. `render` writes them into the vcs descriptor's `merge.env_try`, and the kernel then needs a successful `env.try` receipt for the full head SHA in the call's repository (it stands in for the host's check runs when there are none; failing or pending host runs still refuse). A developer runs `env.try` with the head commit and passes the same SHA as `head_sha` to `vcs.merge`; a new push needs a new run. See [ADR-037](../../docs/decisions/037-coding-and-vcs-operations.md) and [ADR-038](../../docs/decisions/038-throwaway-test-environments.md).
- **`email.inbound`** (optional, off by default): lets the client email the PM directly. The host file's `email` gains an `inbound` key, for example `"inbound": {"rate_limit": {"count": 10, "window_days": 1}}`. `render` then writes an `inbound` section into `email.team.json`, with the alias as the only sender and the PM pool as the destination. Merge it into the email host file. The PM's instructions also gain a line: such mail is the client's input, given as data, and the PM answers with `email.send`. The checks are those of [ADR-039](../../docs/decisions/039-external-email-operations.md): an authenticated sender, each message delivered once, a size and rate limit, and rejections recorded by sender domain and reason only.

## Dashboard

`dashboard_teams.py --teams TEAMS.json` (see `teams.json.example`) serves a read-only page on `http://127.0.0.1:8766/` with a tab per team, each read through its own host file. An unreachable team shows an error code in its tab and the others still render. Each team is a tab showing its state (running, stalled, paused or idle) and a mail view: the team's messages with their full text, newest first, filtered by member and by item (`collection/record-id`), paged with "Load older". The text is read through `GET /v0.2/groups/{name}/status/mail/text`, which only the group's supervisor may call (checked live in the kernel on every read), and the page shows all of it as plain text, never as HTML or links; it works at phone width. It has no message box, binds to loopback only (`--bind` with anything else is refused), and the supervisor credentials stay in this process. `--fixtures dev=A.json aerarium=B.json` serves recorded data.

`dashboard_teams.py --teams TEAMS.json` also serves `http://127.0.0.1:8766/settings`, a page to edit a team's settings (run and spend limits, pause, schedules, developers, model, product goals). It shows the settings in force, sends only what you changed to `POST /api/settings/preview`, and shows what would change; nothing is applied until you press Confirm, which sends the preview's token to `POST /api/settings/apply` (`teamsettings_apply.py`: render, provision, apply, audit). Cancel drops the token and sends nothing; a wrong or stale token is refused with 409. Only the team's supervisor gets through: each call first reads the group overview as the supervisor, so the kernel's live check refuses anyone else, and the apply's writes are checked again. Credentials and the kernel socket are not settings and are never sent to the page; errors are codes, not lower-layer messages. Fixture mode (`--fixtures`) has no host file, so the page has nothing to edit there. The read-only page at `/` is unchanged and has no controls.

`teamkit.py dashboard --host HOST.json [--port 8765]` serves one page on `http://127.0.0.1:8765/`. It renders the group overview (`GET /v0.2/groups/{name}/overview`): members and their runs, work by state and claim, mail, approvals, spend and run ceilings and the latest events. It refreshes every 15 seconds. `dashboard.py --fixture dashboard.fixture.json` serves recorded data with no kernel, to look at the page.

- **Authority:** the server calls the kernel with the supervisor's own credential, the one `teamkit.py` uses. It has no login or session of its own. It listens on the loopback interface only and refuses a Host or Origin that is not that address.
- **One write:** the message box sends one text message (at most 4000 characters) to a role's pool address, `<group>.<role>`. The kernel checks that the supervisor holds `mail:<group>.<role>`; `provision` grants it for every role.
- **Once:** the page sends one client key per message and reuses it on every resubmit of the same text, so a double click, a retry after a failure or a second tab delivers once. The same key with other text is refused with `request_conflict`.
- **No build step:** plain HTML and browser JavaScript. Text from the kernel is only ever shown as text.
- **Developers** work in one run until nothing is left for them: merge what is approved, rework what has changes requested, then take ready items. Each holds at most two items (`MAX_ITEMS_PER_DEVELOPER`), tries the ready items from its own offset (its member number modulo their count) and moves past a start another developer won, giving up after three refusals (`MAX_START_REFUSALS`). Each pushes its own branch per claim, `agent/<group>/<record_id>-<claim_epoch>`, so it opened every pull request it merges. An item whose claim lapses in review or approved keeps its state; developers take over in-progress items no one holds, and leave an approved item no one holds to its opener. The instructions name each developer through `$member`, which the kernel fills in per replica.

- **Transitions in the instructions:** every role step that moves an item names the exact call, for example `records.transition with collection "work" and transition "triage"`. The names come from the work collection's own table (`work_transitions`), so renaming or removing a transition fails the render. The team lead is also told that a field, priority or label saying "ready" moves nothing.

## Changing team settings

`teamsettings_apply.py --host HOST.json --settings CHANGE.json` previews a settings change (`teamsettings.py` names the settings) and prints a confirmation token; it changes nothing. Run it again with `--confirm TOKEN` to apply exactly what was previewed (a moved host file or a different change makes the token stale).

- **Path:** the new host file is rendered and validated first, the supervisor's standing is read live from the kernel, then the host file is replaced, `provision` runs and `apply` puts the group as the supervisor, where the kernel checks authority on every write. A refusal puts the old host file back (byte for byte) and provisions the old team again. Pause and resume go through `pause` and `resume`; while paused, other settings wait for a resume.
- **Audit:** `<group>.settings-audit.jsonl` beside the provisioning file (mode 0600) gets a `started` line before any change (no entry, no change) and an `applied` or `refused` line after: who (the supervisor, or the caller's name), when, and each field's before and after. Goals are recorded by size and digest.
- **Idempotent:** a change already in effect changes and records nothing.
- **Secrets:** outside the settings; a document naming one is refused, and failures are reported by exception type and kernel error code only.

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
- A waiver also means no host rule requires branches to be up to date, so two changes that are each green can break the default branch once both merge (two migrations with the same revision, for example). Set `"require_up_to_date": true` on the alias: `vcs.merge` then refuses a pull request whose head does not contain the base's latest commit (`merge_behind_base`), and the developer brings the base in, pushes, resubmits (`resubmit` transition) and merges once it is approved and green again. `status` prints whether the guard is on, and flags a waiver with the guard off.
