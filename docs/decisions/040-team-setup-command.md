# ADR-040: One Setup Command from a Fresh Debian Host to a Running Team

- **Date / owner / milestone:** 2026-11-21; team-kit maintainers; agent teams (ADR-034, ADR-037, ADR-039).
- **Status:** implemented; portable tests only. The candidate evaluation is a documentation reading without versions (see below), so the build decision is provisional. The steps ran against a simulated host, never on Debian. `KernelProbe` (the default `vcs.check_repository` call) and the SPIRE tarball install have not run against a real kernel or release.
- **Requirement:** `teamsetup.py plan` (dry run) and `apply` take a host with Debian packages only to a running dev team.
  - Every step has `check` (already done?), `action` and `verify`, and every step prints its verify result. `plan` prints each step's verify too, done or not; the repository probe submits a kernel task, so `plan` shows its last recorded result and never runs it.
  - State is recorded, so a run that fails or is killed resumes at the step it stopped on. A repeated run on a converged host changes nothing, including the state file.
  - Secret handling (tokens, email credentials) sits behind a small interface, so it can be built separately. This change does not prompt for or store secrets.
- **Candidates** (policy: `docs/standards-first.md`). No standard covers host convergence, so the candidates are maintained tools. **This session had no network** (TLS verification failed and the proxy refused), so no version, licence or support status below was read from a primary source in this change. Licences are from memory and the versions are not recorded; both are marked and must be confirmed from the linked source before this record is called selected.

  | Candidate | Primary source | Licence (unconfirmed) | Version evaluated |
  |---|---|---|---|
  | Ansible (`ansible-core`, Debian package `ansible-core`) | <https://docs.ansible.com/>, <https://github.com/ansible/ansible> | GPL-3.0-or-later | not checked |
  | cloud-init | <https://docs.cloud-init.io/>, <https://github.com/canonical/cloud-init> | Apache-2.0 or GPL-3.0 | not checked |
  | Puppet (`puppet apply`; OpenVox is a fork) | <https://help.puppet.com/>, <https://github.com/puppetlabs/puppet> | Apache-2.0 for the source; binary packaging terms to confirm | not checked |
  | Shell script following the runbooks | none | n/a | n/a |

- **Fit evidence** (from each tool's documented behaviour; nothing was installed or run, so every row is a reading, not a test):

  | Requirement | Ansible | cloud-init | Puppet | Shell script |
  |---|---|---|---|---|
  | check / action / verify per step | Task state plus a registered verify task; each Cardea check is a `command` task with `changed_when` | No: modules run, with no check or verify phase | Resource state; `unless`/`onlyif` on `exec`; verify is not a phase | Written by hand |
  | Idempotent | Yes for built-in modules; `command` tasks only as far as their guards go | Per instance by default (runs at first boot); not a model for re-running a converged host | Yes for built-in resources; `exec` as for Ansible | Only as written |
  | Resumable after a kill | Re-run converges; no recorded stopping point (`--start-at-task` is manual) | Not designed for it | Re-run converges; no recorded stopping point | Only as written |
  | Dry-run plan | `--check --diff`, but `command` tasks are skipped in check mode unless marked `check_mode: false` | None | `--noop` | Only as written |
  | Print each step's verify | Task output, in Ansible's format | Log | Report | As written |
  | Needs on a bare Debian host | Python and the package's dependencies (Jinja2, PyYAML, cryptography) | Ships in many images; not for an existing host | Ruby agent | bash |

- **Why not reuse, and what is not shown:**
  - Fit: Ansible is the one candidate that could express every row. cloud-init is a first-boot tool and cannot re-run on a converged host. Puppet's model is a catalogue compiled for a long-lived agent, which is more than one host needs, and it needs a Ruby runtime. A shell script is the thing the runner replaces.
  - The deciding gap is concrete but untested. Fourteen steps are Cardea's own checks: the supervisor's `vcs.check_repository` through the kit's client, kernel group membership, grant and token expiry, the helper probe, x509pop certificate renewal. In Ansible each becomes a `command` task that calls Python (`teamkit.py`) and parses its output, with the logic in the commands and the YAML only sequencing them. That adds Ansible as a dependency without removing any Cardea code, and the portable tests (a simulated host in pytest, no Debian) cannot drive Ansible modules; testing Ansible would need containers or a VM.
  - Not shown: no Ansible prototype was written, so "the logic would live in the commands" is an estimate, not a measurement. This record therefore does not say Ansible fails; it says the runner is a bounded exception, and that exception is the first thing to re-test (see the revisit trigger).
- **Decision:**
  - A small runner and a step interface (`deploy/team-kit/teamsetup.py`) that reuses `teamkit.py` for `render`, `provision` and `apply`. The kit gained two optional parameters (`runner`, `connect`) so setup can inject effects.
  - **Node attestation is x509pop**, replacing the runbook's join tokens for this path. A join token is single-use, so a re-runnable setup cannot regenerate it. x509pop attests with a reusable certificate: the setup makes a local CA and a leaf whose CN is the node name, with `agent_path_template` `/{{ .PluginName }}/{{ .Subject.CommonName }}`. The node ID is `spiffe://<trust domain>/spire/agent/x509pop/<node>`. ADR-030 rejected x509pop for needing a separate PKI; here the PKI is one local CA on the same host, whose key stays on that host (it is not kept offline). The leaf is reissued when it expires within 30 days.
  - **State** (`/var/lib/cardea-setup/state.json`) is written before an action (`started`) and after verify (`done` or `failed`). It reports and carries fingerprints; the step's own `check` decides whether to act, so drift is repaired.
  - **Secrets:** `SecretStore.status(name)` and `.provide(name)`. The shipped `FileSecretStore` checks presence and mode `0600` and its `provide` only names the file to create.
- **Runbook gaps as checks:** the runbook's "What the host needs first" and the acceptance notes become step checks. The module docstring maps each item to its step. Tokens expiring within a week and grants expiring within two days fail their step.
- **Limits:** it does not write the host file, the host owner's workers file (it fails and names what is missing) or the base provisioning document once it exists. It does not install the repository host's rules; `repository-ready` fails with the report's missing checks. The Telos sections of the small-host guide are out of scope.
- **Verification:** `apps/api/tests/service/test_team_setup.py`: a fresh host converges; a second run makes no effect and leaves every file untouched; `plan` writes nothing, runs no probe and prints every verify; a kill at five points of the run, then a rerun, completes; a failing step stops the run and the rerun resumes there; each step prints its verify.
- **Revisit trigger:** a first run on a real Debian host (record it here). **Open before this record is final:** confirm the versions and licences in the candidate table from the linked sources, and prototype the Ansible `--check` run of three steps (`kernel`, `tokens`, `repository-ready`) to test the estimate above; replace the runner if that prototype is smaller than the Python it would sit beside.
