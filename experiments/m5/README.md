# M5 installed-package acceptance

The fixture uses one fresh persistent Debian 13 machine. It installs the actual package, reuses real SPIRE 1.15.3 with disposable identities, runs native async A2A delegation and confined workers, revokes a supervisor permission, reboots the whole guest, then verifies persisted artifacts and denial before running a new permitted worker. No model API key or live paid request is needed.

Build/package instructions are in [packaging](../../packaging/README.md). Use the existing owned QEMU harness:

```sh
python3 experiments/m2/vm.py start --state /tmp/cardea-m5-vm
```

An optional `--image` cached Debian cloud image is still SHA-512 checked. Build the package from `make m5-stage` inputs on that guest and install it with apt. Keep installation inactive and unconfigured for the acceptance script; it asserts those conditions. It also requires the `cardea-fit` account created by the VM harness as an unenrolled negative case.

Extract the pinned official `spire-1.15.3-linux-amd64-musl.tar.gz` into `/opt` after verifying SHA-256 `ca1a4d1155317bdd2afc7f36663828a10410c7c840e54725b90b4064b0a301c7`. The reused provider fixture additionally verifies both binary digests. Put exactly these test files in `/opt/m5-fixture` (not in the package):

- `experiments/m5/installed_acceptance.py`
- `experiments/m1/provider_lab.py`
- `experiments/spire/binding.py`

Then, from the workspace host:

```sh
python3 experiments/m5/reboot_vm.py --state /tmp/cardea-m5-vm
```

The guest must have the disposable-VM sentinel. The fixture creates test-only persistent SPIRE systemd units and a private test policy. It calls the installed `/usr/bin/cardea` and imports from `/usr/lib/cardea`, never the workspace source checkout. `result.json` and the three phase logs are copied to the state directory's `evidence` folder on success. On failure inspect the phase log under guest `/root`; do not collect identity-provider configuration, keys, tokens or arbitrary journals as public evidence.

After collecting results, stop the owned guest with `experiments/m2/vm.py stop --state ...`. Confirm its container mount belongs to that state directory before cleanup. Do not use this fixture to enroll a real deployment.

The initial `reboot_vm.py` scenario is preserved as revision 1 evidence. The full M5 operational sequence is in `complete_vm.py` and [M5-REPORT.md](M5-REPORT.md).

For that sequence, retain the enrolled revision 1 machine and original result. Refresh an expired disposable SPIRE node enrollment explicitly through SPIRE; never reset the Cardea store to hide an enrollment failure. Add `operational_acceptance.py` and `interrupt_daemon.py` to `/opt/m5-fixture`. Run the `baseline` phase as `cardea-kernel` with installed imports (`PYTHONPATH=/usr/lib/cardea:/opt/m5-fixture`) and the private package Python. Keep revision 1's `.deb` at `/opt/cardea-kernel_0.1.0-1_amd64.deb` and build final revision 2 to `/opt/cardea-kernel_0.1.0-2_amd64.deb`.

```sh
python3 experiments/m5/complete_vm.py --state /tmp/cardea-m5-vm
```

This asserts the sentinel and baseline, installs revision 2, tests pre-migration refusal, migrates, installs revision 1 to verify refusal of the newer schema, then reinstalls revision 2. A temporary test-only service override holds execution after a real host start. The driver removes the override for the next boot, reboots the machine, checks uncertain-effect recovery and resumes a durable wait. It then tests post-snapshot spending/revocation, restore, historical reads, sealed-scope denial and fresh A2A execution. Installed maintenance checks exercise corruption, interrupted restore, age/count pruning and diagnostics. It verifies retained host tombstones, package contents, unit syntax and dependency consistency.

The fixture is deliberately sequential and does not reset previous failed runs automatically. Inspect the bounded phase logs, preserve failure evidence and resume only the unfinished phases after correcting a fixture problem. `--after-reboot` or `--after-restore` explicitly resume those retained checkpoints, checking the installed version and removal of the barrier override. Artifacts are under `evidence/completion`, separate from revision 1 evidence. The driver stops the daemon at completion; finish by powering off the verified owned guest and removing its outer container, retaining its disk and evidence. No model secret or paid model call is used.
