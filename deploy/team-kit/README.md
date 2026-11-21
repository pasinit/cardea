# Team kit: an agent dev team on a host

This kit builds a generic, configuration-driven dev team from [`examples/teams/dev-team`](../../examples/teams/README.md). It has four roles: a product manager, a team lead, developers and a principal engineer. They work on one repository through `code.session`, `vcs.*`, `env.try` and the kernel's records, mail and spend ceilings.

- `teamkit.py`: the `render`, `provision`, `apply`, `status`, `pause`, `resume` and `stop` commands.
- `team.host.example.json`: the shape of the private host file. `repository` is the vcs alias the team is granted (`repo:<alias>`), and `default_branch` (default `main`) is its default branch. Both are written into the instructions of every role that uses `vcs.*`, `code.session` or `env.try`, so no role has to guess them. The repository holds no host-specific value. Copy the file outside the repository, for example to `/etc/cardea/team.host.json`, and fill it in.

How to run it day to day is in the [runbook](../../docs/runbooks/dev-team.md).
