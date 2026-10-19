# Try Cardea without SPIRE

```sh
make install
make try
```

This starts the real kernel daemon (DBOS dispatch, SQLite store, authority ceilings,
receipts, verification) in a temporary directory, then:

1. a supervisor submits a task that checkpoints and waits for a signal;
2. a second identity with no rights in the namespace is refused;
3. the daemon is killed with `SIGKILL` and restarted from the same store;
4. the supervisor resumes the task, which finishes and passes independent verification,
   with its acknowledged receipt unchanged across the crash.

It takes a few seconds on macOS or Linux and needs no Go, Java, VM or model key.

## What is not real here

Identity is **unenforced**. `devauth.py` replaces the SPIFFE Workload API with HMAC tokens
signed by a key in the temporary store; anyone who can read that key can act as any
provisioned principal. The provider lives only in this directory, which the Debian package
does not ship, and is injected by `daemon.py`; the installed `cardea serve` command cannot
select it. It also refuses non-`*.invalid` trust domains, host/agent/OIDC configuration and
running as root.

Host-confined operations and the incident agent need the supported Debian 13 profile; see
[packaging](../../packaging/README.md). For authenticated acceptance with real SPIRE and
Keycloak, see [M1](../m1/README.md).
