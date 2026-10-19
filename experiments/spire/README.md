# SPIRE Bootstrap Fit Experiment

Real SPIRE 1.15.3 server/agent processes and py-spiffe 0.3.1 Workload API calls, with fictional identities and disposable keys. This is an isolated M1 evaluation; production kernel dependencies and schemas are unchanged.

On macOS with Go, Git, Python 3.12 and uv, from the repository root:

```bash
experiments/spire/build-spire.sh
make spire-fit
```

The build script verifies upstream commit `2f7861ae3923caf1f57eb087fc2928d58c0fb1d2` and builds its unmodified source. Go automatically downloads the required 1.26.6 toolchain and modules. This can take several minutes and substantial cache space. No system SPIRE service is installed. `SPIRE_SOURCE_DIR` can change the build directory; set `SPIRE_BIN` to its `bin` directory when running tests. The default is `/tmp/cardea-spire-1.15.3/bin`. Tests fail if binaries are missing or their embedded source revision differs; they do not silently skip provider tests.

```bash
uv run --project experiments/spire --locked pytest experiments/spire/tests --junitxml=/tmp/cardea-spire-fit.xml
```

Each suite starts a server on a free loopback port and an agent with a Unix socket, in a private temporary directory. It supplies an explicit trusted CA bundle and a mode-0600 one-time join-token file, uses built-in SQLite/disk key storage and Unix UID selectors, and removes processes and proof material afterward. The harness never prints issued tokens or keys. Temporary signing keys for malformed proofs are trusted only through an explicitly registered fictional federation; there is no custom Cardea issuer.

`binding.py` exercises a small application boundary: bounded provider calls, sanitized denial, configured trust-domain allowlisting, exact principal mapping and resolution of provisioned task/agent/supervisor/ancestor/lease relationships. These relationships and disablement are **in-memory fixtures**, not authenticated human admission, persisted provenance or a policy engine. Returning a binding grants no operation permission. Production still needs the live authority intersection and versioned storage.

The suite explicitly demonstrates limitations: same-UID processes can obtain the same registered SVIDs; one runtime can use all tasks assigned to it; removing a registration does not immediately invalidate issued JWTs; server outage differs from agent validation outage. Independently untrusted agents require separate host/identity boundaries. Native signing-key rotation and service restart are exercised, but full CA rotation/revocation, Debian confinement, OIDC login and production backup/restore are untested.

See [ADR-009](../../docs/decisions/009-spire-bootstrap-fit.md) for the results, sources and remaining gates. `make test` retains the portable baseline; this suite launches real services and runs separately.
