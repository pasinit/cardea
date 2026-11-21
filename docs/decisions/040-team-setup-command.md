# ADR-040: One Setup Command from a Fresh Debian Host to a Running Team

- **Date / owner / milestone:** 2026-11-21; team-kit maintainers; agent teams (ADR-034, ADR-037, ADR-039).
- **Status:** implemented; portable tests only. The reuse evaluation below is **provisional**: it was written without network access, so versions are not checked and licences are unconfirmed. The steps ran against a simulated host, never on Debian. `KernelProbe` (the default `vcs.check_repository` call) and the SPIRE tarball install have not run against a real kernel or release.
- **Requirement:** `teamsetup.py plan` (dry run) and `apply` take a host with Debian packages only to a running dev team.
  - Every step has `check` (already done?), `action` and `verify`, and every step prints its verify result.
  - State is recorded, so a run that fails or is killed resumes at the step it stopped on. A repeated run on a converged host changes nothing, including the state file.
  - Secret handling (tokens, email credentials) sits behind a small interface, so it can be built separately. This change does not prompt for or store secrets.
- **Candidates (provisional; policy: `docs/standards-first.md`):** no network was available, so versions are "not checked" and licences are recalled, not confirmed. Source URLs were not fetched.

  | Candidate | Primary source | Licence | Version |
  |---|---|---|---|
  | Ansible (`ansible-core`) | https://docs.ansible.com/ansible/latest/ ; https://github.com/ansible/ansible | GPL-3.0-or-later (unconfirmed) | not checked |
  | cloud-init | https://cloudinit.readthedocs.io/ ; https://github.com/canonical/cloud-init | Apache-2.0 or GPL-3.0 (unconfirmed) | not checked |
  | Puppet / OpenVox | https://github.com/OpenVoxProject ; https://voxpupuli.org/openvox/ | Apache-2.0 (unconfirmed) | not checked |
  | Shell script following the runbooks | none (this repository's runbooks) | n/a | n/a |

  Fit against the requirements. Entries are from the tools' documented models, not from running them here.

  | Requirement | Ansible | cloud-init | Puppet / OpenVox | Shell script |
  |---|---|---|---|---|
  | check / action / verify per step | modules check and act; `assert` or `command` tasks verify | no check or verify; modules run per boot frequency | resources check and act; verify is not a step | written by hand |
  | idempotent | yes for modules; `command` tasks need `creates` or `changed_when` | per-instance or per-boot, not per-step | yes for resources | written by hand |
  | resumable after a kill | reruns from the top; `--start-at-task` is manual | no | reruns the catalog | needs state, written by hand |
  | dry-run plan | `--check --diff`; `command` tasks are skipped unless `check_mode: false` | none | `--noop` | written by hand |
  | prints each step's verify | per-task output; verify must be its own task | no | no | written by hand |
  | bare Debian (packages only) | needs Python on the host; runs from the host itself with `ansible-core` installed | built in to some images, not a step runner | needs an agent package | needs nothing |

- **Why not reuse (provisional):** Ansible is the only candidate that could express every row. Fourteen steps are Cardea's own checks (the supervisor's `vcs.check_repository`, the kernel group, grant expiry, the helper probe), which Ansible would only wrap as `command` tasks, so its modules would add little and `--check` would skip them. The pytest simulated-host tests drive `System` and `Probe` directly and cannot drive Ansible. Both points are an untested estimate, not a measurement. cloud-init configures a machine at first boot and has no re-run, check or verify model. Puppet and OpenVox need an agent package and a catalog language for the same wrapped checks. A shell script needs the state, resume and plan logic written again, without tests.
- **Decision:**
  - A small runner and a step interface (`deploy/team-kit/teamsetup.py`) that reuses `teamkit.py` for `render`, `provision` and `apply`. The kit gained two optional parameters (`runner`, `connect`) so setup can inject effects.
  - **Node attestation is x509pop**, replacing the runbook's join tokens for this path. A join token is single-use, so a re-runnable setup cannot regenerate it. x509pop attests with a reusable certificate: the setup makes a local CA and a leaf whose CN is the node name, with `agent_path_template` `/{{ .PluginName }}/{{ .Subject.CommonName }}`. The node ID is `spiffe://<trust domain>/spire/agent/x509pop/<node>`. ADR-030 rejected x509pop for needing a separate PKI; here the PKI is one local CA on the same host, whose key stays on that host (it is not kept offline). The leaf is reissued when it expires within 30 days.
  - **State** (`/var/lib/cardea-setup/state.json`) is written before an action (`started`) and after verify (`done` or `failed`). It reports and carries fingerprints; the step's own `check` decides whether to act, so drift is repaired.
  - **Secrets:** `SecretStore.status(name)` and `.provide(name)`. The shipped `FileSecretStore` checks presence and mode `0600` and its `provide` only names the file to create.
- **Runbook gaps as checks:** the runbook's "What the host needs first" and the acceptance notes become step checks. The module docstring maps each item to its step. Tokens expiring within a week and grants expiring within two days fail their step.
- **Limits:** it does not write the host file, the host owner's workers file (it fails and names what is missing) or the base provisioning document once it exists. It does not install the repository host's rules; `repository-ready` fails with the report's missing checks. The Telos sections of the small-host guide are out of scope.
- **Verification:** `apps/api/tests/service/test_team_setup.py`: a fresh host converges; a second run makes no effect and leaves every file untouched; `plan` writes nothing; a kill at five points of the run, then a rerun, completes; a failing step stops the run and the rerun resumes there; each step prints its verify.
- **Open before final:** confirm versions and licences against the primary sources, and prototype Ansible `--check` for the `kernel`, `tokens` and `repository-ready` steps to test the estimate above. If it holds, record it here; if not, revisit the decision.
- **Revisit trigger:** a first run on a real Debian host (record it here), or a maintained tool that can express these checks.
