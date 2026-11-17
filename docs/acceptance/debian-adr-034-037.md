# Debian acceptance: ADR-034, ADR-035, ADR-036 and the ADR-023 amendment

This is what an operator must run on the supported Debian host before these records can say **verified on Debian**. As of 2026-11-16 none of it has run: every item below passes portable tests only (macOS and CI), and their status lines say so.

Each item names the script that exists, or says **gap** when no script exists yet. The scripts in [`experiments/debian-acceptance`](../../experiments/debian-acceptance/README.md) were written for this checklist and have not run yet; they use their own store, free ports and only the processes they start, and each writes an `evidence.json`. A record moves to verified only when every item for it has passed on the host, and its evidence (commands, versions, results) is committed beside the record, as for the [M2](../../experiments/m2/M2-REPORT.md) and [M5](../../experiments/m5/M5-REPORT.md) reports.

## The host

- [ ] **A disposable Debian 13 amd64 machine** with a Debian 13 6.12 LTS kernel, as ADR-028 requires. `python3 experiments/m2/vm.py start --state <dir>` starts the owned QEMU guest the M2 and M5 fixtures use ([experiments/m2/README.md](../../experiments/m2/README.md)). Never use a machine that holds real data.
- [ ] **The package built from the commit under test.** `make m5-stage M5_STAGE=<new dir>`, then build and install it on the guest as in [packaging](../../packaging/README.md). Record the commit and the package version.
- [ ] **Migrations on the host.** `cardea init` on a fresh store reaches `m18_env_runs`. `cardea init` on an M5 store (`m5_001`) migrates it through every later revision, and `cardea verify` passes afterwards. Today only the `m4_001` to `m5_001` upgrade is verified there. **Script:** `upgrade.py --source <M5 backup>` ([README](../../experiments/debian-acceptance/README.md)) upgrades a copy with the installed `cardea init` and checks row counts, the schema against a fresh store, `cardea verify` and the append-only triggers.
- [ ] **Identities.** Real SPIRE 1.15.3 with disposable identities, as the M5 fixture sets up (`experiments/m5/installed_acceptance.py` with `experiments/m1/provider_lab.py` and `experiments/spire/binding.py`). ADR-036 also needs one OIDC human identity. `experiments/debian-acceptance/oidc_issuer.py` is a throwaway test issuer for that: discovery, JWKS and RFC 7662 introspection on a random loopback port, with tokens signed by `joserfc`, which the pinned `authlib` already depends on.

## Regression first

Run these first. They already exist, and nothing below means anything if they fail on the commit under test.

- [ ] `make m1-test` and `make m2-host-fit M2_STATE=<dir>` (the M1 and M2 fixtures).
- [ ] `python3 experiments/m5/complete_vm.py --state <dir>`: the M5 upgrade, reboot and restore sequence.
- [ ] **ADR-028 probes:** `CARDEA_CONFINED_DEBIAN=1 pytest apps/api/tests/hosts/test_confined_debian.py` as root on the guest, with the helper installed. They cover a memory-cap kill, `none` denying all connections and DNS, the allow-list reaching only the proxy and only listed hosts, and no view of the store, the helper configuration or credentials. They have never run. Every confined item below depends on them.
- [ ] `make try` on the guest: the real daemon, killed with `SIGKILL` and restarted from the same store.

## ADR-023 amendment: long confined operations and artifacts

The portable tests are in `apps/api/tests/service/test_code_session.py`. On the host they must run under the real helper and systemd.

- [ ] **A long call survives.** A confined fake operation with a 20-minute timeout under a profile with `wall_seconds` ≥ 1200. Its lease is renewed every minute, and it settles `succeeded` with one receipt. **Script:** `sudo sh experiments/debian-acceptance/long_call.sh`, phase 1: `code.session` with a sleeping harness under the real `coding` profile.
- [ ] **A lost lease stops the process tree.** Stop the daemon with `SIGKILL` 10 minutes into such a call. The worker sees the lost lease and its whole process tree is gone (check the transient unit and `ps`). After restart the call is `outcome_unknown` and is not retried. **Script:** `long_call.sh`, phase 3. It kills the service process that runs the call (the kernel's operation runtime in a child of the script, not the systemd daemon), then checks that the worker outlived it, was reaped by the restarted service and left no process.
- [ ] **Limits refused at admission.** A descriptor timeout above the installed profile's `wall_seconds`, or above 7200 s, is refused with `409 operation_timeout_exceeds_profile`. **Script:** `long_call.sh`, phase 5, against the installed helper's profile.
- [ ] **Artifacts.** A worker returns a 10 MiB artifact. It is stored as task-owned objects of at most 1 MiB, read back by digest, and kept across `cardea backup`, `cardea restore` and `cardea verify`. An artifact above the 10 MiB limit stores nothing and reconciles as not applied. **Script:** `long_call.sh`, phases 2 and 4 (a 9 MiB bundle, and one of about 10.3 MiB, which still fits the profile's `output_kb`).
- [ ] **Gates cannot forge results or change the change** (`code.session`, ADR-037). A gate cannot write the worker's result through `/proc` or read its memory. A detached `setsid` grandchild is killed before it can edit the change. A gate that kills the worker leaves no result and no stored object. A gate that edits the checkout fails the call with `gate_changed_checkout`. **Script:** `sudo sh experiments/debian-acceptance/coding_run.sh gate_isolation.py`. It has not run.
- [ ] **The loopback fence (ADR-028).** From inside a `coding` worker, a connection to 127.0.0.1 at the kernel's port fails, while the per-attempt tool endpoint on the helper's dedicated address (`127.0.11.1`) answers. The unit's `IPAddressAllow` names only the proxy and `127.0.11.1/32` (`systemctl show cardea-host-<job>.service -p IPAddressAllow` while a call runs). **Script:** `long_call.sh`, phase 6. Pass `-- --kernel-port <port>` to probe the daemon's own listener; otherwise the script opens a stand-in on 127.0.0.1.
- [ ] **Registries for `code.session`'s gates** (ADR-037 question 8). The `coding` profile's `registries` are reachable over TLS through its proxy, an arbitrary host is not, plain HTTP and other ports to a registry are refused, and 127.0.0.1 stays unreachable. **Script:** `experiments/debian-acceptance/registry_egress.py`. It also checks that `/etc/squid/squid.conf` carries the profile's rules as the helper renders them. With the kit's strict option (`deploy/small-host/cardea/strict-cache/`), run it with `--strict`: the profile lists no registry and `pypi.org` is refused. It has not run.
- [ ] **`code.session` on the `coding` profile.** Not part of the amendment, but its first long confined user. Run `sudo sh experiments/code-session/debian.sh <token file> <out dir>` on a host with the small-host kit's `coding` profile ([experiments/code-session/README.md](../../experiments/code-session/README.md)). It passes when every check in `evidence.json` is true, and `proxy.txt` shows only the model API reachable. It spends real model usage (at most 1 USD). This script exists but has never run. It does not cover a call long enough to need a lease renewal.

## ADR-034: groups, shared records and mailboxes

ADR-034 requires acceptance tests 3–5 to run on Debian with a real daemon restart. The rest must run once through the installed daemon. Portable tests are listed in the ADR's [Verification](../decisions/034-agent-groups-and-shared-records.md#verification).

- [ ] **Worked examples through the installed daemon** (acceptance tests 1 and 2). Apply `examples/teams/software-team` and `examples/teams/trading-desk` over the Unix socket. Run one work item through its flow, then restructure by changing only the manifest. `make teams` runs this walkthrough in process. **Script:** `teams.py` runs both against a daemon, each agent step a queued run of the scripted harness. **Gap:** its identities are the development identity, not SPIFFE.
- [ ] **Test 3, no double claim.** Twenty concurrent claims from two members: exactly one wins. `SIGKILL` the daemon after the claim commits: after restart there is still one holder. Let the lease expire: a new epoch, and `claim_lost` for the old holder. **Script:** `groups.py --tests 3`.
- [ ] **Test 4, idle and failed holders.** A weekly schedule and a short lease, with a daemon restart in between: an idle holder keeps its epoch through follow-up wakes, a failing holder loses the claim within lease + 60 s, and follow-ups stop after `max_followups`. **Script:** `groups.py --tests 4`, with a 300 s lease by default (`--lease-seconds 3600` for the ADR's 1 h).
- [ ] **Test 5, message once across restart.** Send a message, then `SIGKILL` the daemon before delivery. The next run receives it, fails, and the following run receives it again (`deliveries = 2`) and succeeds. Pool mail is consumed by one member, group mail by every member. A message is dead-lettered after `max_deliveries`, and the sender and supervisor are notified. **Script:** `groups.py --tests 5`.
- [ ] **Test 6, ceiling.** Revoke one grant from the supervisor with `cardea provision`. The next run of each pool that uses it is refused, and a running one stops at its next call.
- [ ] **Test 10, journal triggers across a restart.** A new record wakes the subscribed member exactly once, even when the daemon is killed at a random point of the minute tick, and a burst coalesces. **Script:** `groups.py --tests 10`.
- [ ] **Draining.** Scale a pool down while a member's run is in progress, then reboot the host. The drain resumes and the member is disabled after `drain_timeout_seconds`.
- [ ] **Spend.** Test 8 through real runs: an agent harness with a fake model server that reports cost, a group ceiling of two runs' `max_cost_usd`, and the third concurrent admission refused with `429 budget_exhausted`. A windowed expectation's miss is mailed after the window closes.
- [ ] **Overview.** `GET /v0.2/groups/{name}/overview` over the TLS listener with a SPIFFE client certificate.

Tests 7 (guards and evidence) and 9 (context bounds) involve no host behaviour; their portable tests are enough once the items above pass.

## ADR-035: event triggers

Portable tests: `apps/api/tests/service/test_event_triggers.py`. None of these has run.

- [ ] **A signed delivery through the live TLS listener** of the installed daemon, from a sender off the host, with the trigger installed by provisioning and its secret file at mode `0600`. **Script:** `events_tls.py`: `curl` over TLS 1.3 to a free port, refusals, duplicates across a daemon kill, no plaintext and no TLS 1.2; `--listen-host 0.0.0.0 --external-wait 600` prints a signed `curl` command to run from another machine.
- [ ] **A third-party sender.** One real Standard Webhooks sender (a library or a hosted service's test delivery), not Cardea's own signer. **Gap:** `events_tls.py` signs with its own HMAC code, independent of the kernel's library, but that is still not a hosted sender.
- [ ] **Redelivery across a restart.** The same event, delivered again after `systemctl restart cardea-kernel`, returns the same task.
- [ ] **A proxy in front of the listener.** When `client_ca_file` is set, the sender's TLS is terminated at a proxy, as [operations](../operations.md#event-triggers) recommends.
- [ ] **Snapshot restore.** After `cardea restore`, deliveries are refused until the host owner provisions again.

## ADR-036: approval waits (slices 1–4)

ADR-036 slice 6 defines the host acceptance: a real OIDC human approver and a SPIFFE agent. Portable tests: `test_approval_waits.py`, `test_approval_expiry.py`, `test_agent_approvals.py` and `test_approvals.py`.

- [ ] **A human approves.** An operation task waits. A person with an OIDC identity and the `approve` mode lists it with `GET /v0.2/approvals`, approves it with the right `intent_digest`, and the effect runs once as `apr-01`. **Script:** `teams.py`, trading desk. The desk approver is a human principal with an OIDC identity at the run's test issuer. The daemon validates its tokens with the kernel's own OIDC path (discovery, then introspection), not the development identity. The approver lists and approves a held `approve` transition, and the decision is recorded under its principal. Wrong-audience, expired, scope-less, unknown-subject and forged tokens are refused. **Gap:** the issuer is a local stand-in, not a real provider; the agents use the development SPIFFE identity; and the effect is a held record transition in an agent run, not an operation task's `apr-01`.
- [ ] **A wait across a reboot.** Reboot the host while a task waits. It is still waiting afterwards, and a decision made after the reboot runs it.
- [ ] **Expiry on the real schedule.** A request expires within about a minute of `expires_at`. A request that expired while the daemon was stopped expires on the first tick after it starts.
- [ ] **An agent run waits and continues.** With a SPIFFE agent and a real harness against a fake model server, a held call returns `approval_pending`, the attempt parks, and after approval the continuation attempt runs `apr-01` and the run succeeds.
- [ ] **Four eyes and refusals** over the real authentication: `self_approval` for the requesting agent, `four_eyes_required` for the supervisor under `distinct_from_supervisor`, and `approver_denied` after a revocation.
- [ ] **Snapshot restore** voids open requests (`snapshot_restored`).

Slice 5 (per-grant rules) is not implemented, so it has nothing to verify.

## ADR-038: throwaway test environments (`env.try`)

None of these has run. **Script:** `sudo sh experiments/debian-acceptance/env_try.sh`, on a host with the small-host kit's `try` profile, `env-try` worker and squid `try` port (deploy/small-host/README.md, section 4, step 10).

- [ ] **Pass and fail** (acceptance test 1). A good commit passes `try` and `teams`. A bad one fails `try` with its exit status, a bounded log tail and its artifact stored as an object. No `cardea-host-*` unit is left. Phase 1.
- [ ] **Same receipt and reruns** (test 2). The same commit and scenarios, in any order, return the first receipt (`cached: true`) without starting a unit. Three forced reruns run, and a fourth in a day is refused with `env_rerun_limit`. Phase 2.
- [ ] **Confinement** (test 3). A hostile commit's scenario reaches none of: the store, the helper spool, the credentials directory, the kernel on 127.0.0.1, DNS, the metadata address, a host the proxy does not list. Filling the disk fails the scenario, not the host. Phase 3.
- [ ] **Caps** (test 4). A scenario past its time limit reports `timeout`, and the next scenario still runs. Phase 4. Memory and task caps are the profile's systemd properties; a memory cap kill fails the call as `worker_memory_limit` (ADR-028 probes).
- [ ] **The result cannot be forged** (lead review). Each of these must fail to change the reported result:
  - a scenario writing a forged result to `/proc/<worker>/fd/*`, or reading `/proc/<worker>/mem`;
  - a `setsid` background process, which must be killed before the next scenario and leave nothing on the host;
  - a scenario that kills the worker, which must leave the call failed with no result.

  The arm64 dev VM can run this now. Phase 6.
- [ ] **Concurrency** (test 6). An identical call while the first runs is refused with `env_run_in_progress` and starts nothing. Phase 5.
- [ ] **The toolchain image.** `make try-toolchain` on the host builds a Debian 13 minbase image with `mmdebstrap`, unpacks it read-only under its SHA-256 in `/opt/cardea-try/toolchain/` and pins it in the repository registry. The next call has a new run key. Enable `cardea-try-toolchain.timer` for the weekly refresh.

## ADR-039: external email

Portable tests: `test_email_ops.py` (both adapters against in-process fakes) and the dev-team rehearsal. None of the steps below has run.

- [ ] **A live send and reply.** On a host with the private `/etc/cardea/email.json` and its key or password file (mode `0600`, owned by `cardea-kernel`), one email reaches the alias's address, a human reply from that address is accepted by `email.poll_replies`, and the reconciler finds the send by its effect key. With AgentMail, a repeated send with the same idempotency key sends nothing more. **Script:** `runuser -u cardea-kernel -- /opt/venvs/cardea-kernel/bin/python experiments/debian-acceptance/email_live.py --alias client`. It sends one real email and waits up to 15 minutes for the reply. Its evidence holds no address or text.
- [ ] **Through the daemon.** The `email.send` descriptor installed under `operations_dir` and the package in the service's environment. A product-manager run sends one question. The reply reaches that member's mailbox within one poll interval plus the 30-second loop, and the member's next run receives it as an untrusted `mailbox.inbox` block. A reply from another address is dropped and counted (`operation.reply_dropped`), and its text appears nowhere in the store or the journal. **Gap:** no script yet.
- [ ] **Credentials stay in the operation.** After the run above, the API key or password appears in no table, no journald line of `cardea-kernel`, no receipt and no agent prompt (`grep -r` over a `cardea backup` and `journalctl -u cardea-kernel`).
- [ ] **No inbound endpoint.** The host has no listener for mail: replies come only from polling (`ss -ltnp` shows only the kernel's configured listeners).

## Recording the result

- [ ] Commit the evidence (commands, package and dependency versions, kernel version, results) under `experiments/`, as the M2 and M5 reports do.
- [ ] Change each record's **Verified on Debian** line and its row in the [ADR index](../decisions/README.md), only for the items that passed.
