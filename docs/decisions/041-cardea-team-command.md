# ADR-041: `cardea team` for operators, through a narrow sudoers rule

- **Date / owner / milestone:** 2026-11-23; team-kit maintainers; agent teams (ADR-034, ADR-040).
- **Status:** implemented; portable tests only (`test_team_cli.py`, `test_team_package.py`). Not yet run on an installed Debian package: the `visudo -cf` check in the postinst, the sudo call and the man page rendering are untested on a host.
- **Requirement:** an operator on a host installed from the package runs the team kit's commands (status, pause, resume, stop, apply, render, provision, settings, set, mail) as a plain user, with the kernel's own code. The commands must act as the team supervisor, whose host file, kernel socket and identity belong to `cardea-kernel`.
- **Candidates and why not reuse:**
  - `sudo` with a sudoers rule (already how the package's `cardea-host` helper works): reused. The rule allows one root-owned helper, as `cardea-kernel`, with no arguments.
  - Polkit, a setuid binary or a socket-activated service: each adds a component for what one sudoers line expresses. Not evaluated further.
  - Giving the operator group access to the kernel socket or host file: rejected; the host file names the supervisor's identity and credentials, and the socket would then admit any local caller as that supervisor.
- **Decision:**
  - `cardea.service.team_cli` (`cardea team ...`) parses and confirms, then relays. A plain user's request goes to `sudo -n -u cardea-kernel /usr/libexec/cardea-team` as JSON on standard input, so the sudoers rule can forbid arguments. The helper script is a fixed `exec` of the package's Python with `-I`.
  - The helper reads only `/etc/cardea/team.host.json`. `--host` is honoured only for a caller that already is `cardea-kernel`, and refused through the helper. Through the helper, `render --out` is limited to `/var/lib/cardea/team`.
  - The kit's modules ship under `/usr/lib/cardea/teamkit`. The CLI sets `teamkit.PROVISIONING_DEFAULTS` so the kit's own records (provisioning document, pause record, settings audit) go to `/var/lib/cardea/team`, which `cardea-kernel` can write; `/etc/cardea` is root's. A host file's own `provisioning` section wins. Where the host file is in a directory the account cannot write, `teamsettings_apply.py` rewrites it in place.
  - `set` is two helper requests: a preview (the dashboard's validation and diff) and, after the operator confirms, an apply carrying the preview's token, so what is applied is what was shown. Every attempt is audited with the operator's login (`SUDO_USER`).
  - The supervisor gets a `mail:<group>.dev.<n>` grant per developer, so `mail dev.N` can address one developer.
  - The new `email_rate_limit` setting maps to `email.rate_limit` with the kit's own bounds (`count` 1 to 1000, `window_days` 1 to 90) and is on the dashboard Settings page too.
- **Authority:** the CLI decides nothing. The sudoers rule only chooses who may start the helper; the kernel checks the supervisor's live authority for every write, as for the dashboard.
- **Limits:** anyone in `cardea-operators` can act as the supervisor through the kit and can make `cardea-kernel` write under `/var/lib/cardea/team`. Keep the group to the team's operators. Error output names only the kit's own refusals, or an exception type and kernel code, never a lower layer's message.
- **Revisit trigger:** a first run on a real Debian host; a need for per-operator authority below the supervisor's.
