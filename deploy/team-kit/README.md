# Team kit: an agent dev team on a host

This kit builds a generic, configuration-driven dev team from [`examples/teams/dev-team`](../../examples/teams/README.md). It has four roles: a product manager, a team lead, developers and a principal engineer. They work on one repository through `code.session`, `vcs.*`, `env.try` and the kernel's records, mail and spend ceilings.

- `teamkit.py`: the `render`, `provision`, `apply`, `status`, `pause`, `resume` and `stop` commands.
- `team.host.example.json`: the shape of the private host file. `repository` is the vcs alias the team is granted (`repo:<alias>`), and `default_branch` (default `main`) is its default branch. Both are written into the instructions of every role that uses `vcs.*`, `code.session` or `env.try`, so no role has to guess them. The repository holds no host-specific value. Copy the file outside the repository, for example to `/etc/cardea/team.host.json`, and fill it in.

Further host-file fields:
- **`run_timeouts`:** overrides each role's run timeout (defaults: pm 3600 s, tl 900 s, dev 3600 s, pe 1800 s). A run timeout must not exceed the `wall_seconds` of the confined session profile the team's sessions run under. Otherwise systemd kills the session mid-run, for example during a coding session.
  - `session_profile` (default `session`) names that profile.
  - `workers_config` (default `/etc/cardea-host-workers.json`) names the file that holds it.
  - `render` warns and `provision` and `apply` refuse when a timeout exceeds the wall. When the file is unreadable, the check cannot run and `render` says so.
- **Mail:** each role's grants are pool addresses (`mail:<group>.<pool>`). Its instructions list exactly those addresses, as `{"pool": "<group>.<pool>"}`, so no role mails a member it is not granted.

How to run it day to day is in the [runbook](../../docs/runbooks/dev-team.md).
