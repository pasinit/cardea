# Debian acceptance scripts (ADR-034, ADR-035, ADR-023 amendment)

These scripts close the "no script yet" gaps (and the ADR-037 question 8 registry check) of the [Debian acceptance checklist](../../docs/acceptance/debian-adr-034-037.md). An operator runs them on a Debian 13 dev host. **None of them has been run yet.** Until one has passed on the host and its evidence is committed, the records it covers stay "portable tests only". A first run may well show bugs in a script itself; fix the script, and record what changed next to the evidence.

`make acceptance-check` lints and type-checks them, and `apps/api/tests/service/test_debian_acceptance_scripts.py` checks that each one parses and answers `--help`. Neither of those runs them.

## Rules every script follows

- **Its own store only.** Each script works in one directory, by default `/var/tmp/cardea-acceptance/<name>`. It creates the directory and marks it with a `.cardea-acceptance` sentinel naming the script. It refuses a directory without that sentinel, and it refuses `/`, `/var/lib/cardea`, `/etc` and the home directory. So it can never open the host's store.
- **Idempotent.** Running a script again with the same directory empties the directory and starts over. The script refuses while a process from the earlier run is still alive.
- **Its own processes only.** Each process a script starts is recorded in `pids.json`. The script signals only those PIDs, never a process found by name. A "kill" is SIGKILL of the daemon it started.
- **Free ports.** The TLS listener binds a free port on loopback, unless `--listen-host` names another address.
- **Evidence.** Each script writes `evidence.json` (or the file given with `--out`). It holds every check as true or false, the observations behind each check, and the host, Python and commit it ran on. The script exits 0 only when every check passed.

## The kernel under test

The kernel code is the installed package's. Each script imports `/usr/lib/cardea` when the Debian package is installed. Otherwise it uses a `cardea` the interpreter can already import (the small-host kit's environment under `/opt/cardea`). Run each script with that installation's interpreter:

```sh
PY=/opt/venvs/cardea-kernel/bin/python      # the Debian package
PY=/opt/cardea/apps/api/.venv/bin/python    # the small-host kit
```

Scripts that need a daemon start the installed kernel's real `serve()` through `launcher.py`, with two stand-ins:
- the UNENFORCED development identity of [`experiments/try`](../try/README.md) for SPIFFE tokens. It refuses root and host execution. When a script configures an `oidc` block, OIDC tokens are checked by the kernel's own OIDC code (discovery, then RFC 7662 introspection) against the test issuer in `oidc_issuer.py`;
- the scripted harness in `plan.py`: each agent-run attempt plays an action the script queued for its agent (tool calls over the attempt's real MCP endpoint, then succeed, fail or crash), instead of a model.

Everything else is the production path: DBOS dispatch and schedules, the SQLite store, admission and the live ceiling, receipts and the journal. SPIFFE identities are therefore not real ones; the M1 and M5 acceptance cover those (see the checklist's regression section). OIDC validation is the kernel's real code, against a local test issuer.

## The scripts

| Script | Covers | Runs as | Time |
|---|---|---|---|
| `upgrade.py --source SNAPSHOT` | An M5 store (`m5_001`, a `cardea backup`) upgraded to the head by the installed `cardea init`. Checks row counts, that the schema equals a fresh store's, `cardea verify`, and that every append-only trigger refuses a change. `--synthesize` builds an empty `m5_001` store instead. | any user | minutes |
| `sudo sh long_call.sh` | The ADR-023 amendment with `code.session` under the real helper and `coding` profile. Checks lease renewal over a 20-minute call; a SIGKILLed service whose worker outlives it and is then reaped, leaving `outcome_unknown` and no process; a 9 MiB artifact read back by digest and kept across backup and restore; an oversized artifact; and the timeout limits. | root, then `cardea-kernel` | about 45 min |
| `sudo sh env_try.sh` | ADR-038 `env.try` under the real helper and `try` profile, on a fixture with a good, a bad and a hostile commit. Checks pass and fail with exit status, log tail and an artifact object; nothing left on the host; the same inputs returning the first receipt; three forced reruns and a refused fourth; every hostile attempt (store, helper spool, credentials, kernel loopback, DNS, metadata address, unlisted host, filling the disk) failing; a scenario time limit; and an identical call in progress refused. | root, then `cardea-kernel` | minutes |
| `groups.py` | ADR-034 acceptance tests 3, 4, 5 and 10 with real daemon kills and restarts. `--tests` picks some of them. | any non-root user | about 40 min |
| `teams.py` | The two worked examples (`examples/teams`) against the daemon. Each agent step is a queued run. It also restructures the software team by manifest. The desk approver is an OIDC human at a test issuer, so its approval exercises the kernel's OIDC validation, and bad tokens are refused. | any non-root user | minutes |
| `registry_egress.py` | ADR-037 question 8: the `coding` profile's package registries are reachable over TLS through its proxy; an arbitrary host, plain HTTP, other ports and 127.0.0.1 are refused; and the installed squid rules match the helper's rendering of the profile. | any user that can read the helper and squid configuration | seconds |
| `events_tls.py` | ADR-035 through the live TLS 1.3 listener with `curl` as the sender: admission, duplicates across a restart, refusals, no plaintext and no TLS 1.2. With `--external-wait`, it also checks a delivery sent by `curl` from another machine. | any non-root user | minutes |

Examples:

```sh
$PY experiments/debian-acceptance/upgrade.py --source /private/m5-backup.sqlite3
sudo sh experiments/debian-acceptance/long_call.sh
sudo sh experiments/debian-acceptance/env_try.sh
$PY experiments/debian-acceptance/groups.py
$PY experiments/debian-acceptance/teams.py
$PY experiments/debian-acceptance/events_tls.py --listen-host 0.0.0.0 --server-name dev-host.example --external-wait 600
```

## What they do not cover

- **The daemon under systemd.** Every daemon here is a child process of its script. Restart and reboot through `systemctl` are covered by the M5 acceptance.
- **`long_call.py`'s service** is the kernel's operation runtime in a child process (`OperationRuntime.execute`, the step the daemon's workflow runs). It is not the daemon, because the development identity cannot enable host execution.
- **A real OIDC provider.** `teams.py`'s desk approver is an OIDC human at `oidc_issuer.py`, a local stand-in with no login, whose tokens the kernel introspects. A provider such as Keycloak is still not part of these runs.
- **A third-party webhook sender library.** `events_tls.py` signs with its own HMAC code and sends with `curl`. Neither is a hosted sender.
