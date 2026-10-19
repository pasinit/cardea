# Security

## Reporting

Report suspected vulnerabilities privately through GitHub's
[security advisory form](https://github.com/pasinit/cardea/security/advisories/new).
Do not open a public issue. Expect an acknowledgement within seven days.

## Trust model

Cardea is pre-release software. It is supported only on the single-host Debian 13
profile described in [packaging/README.md](packaging/README.md); every other host is
a development environment with no isolation guarantee.

Trusted: the host administrator, the Cardea kernel service, installed operation
implementations, and the configured model broker.

Untrusted: task input, model output, tool results, and worker processes. None of
these can grant authority. A task's effective permissions never exceed what its
accountable supervisor currently holds and may delegate, intersected with platform
policy; revocation is checked at admission and dispatch.

Workers run under systemd + bubblewrap with no network, no access to kernel storage
or provider credentials, and bounded CPU, memory, processes, output and wall time.

## Out of scope

- Tamper resistance against a host administrator. The audit log is append-only to
  applications, not cryptographically protected.
- Prompt injection inside model reasoning. Cardea bounds what an agent can *do*
  through authority ceilings and confined operations; it does not detect or filter
  injected instructions.
- Exactly-once external effects. Interrupted effects are recorded as
  `outcome_unknown` and require reconciliation; they are never silently retried.
- `make try` (`experiments/try/`), which uses an unenforced development identity and is
  not shipped in the Debian package.
- The legacy escalation demo on the `archive/legacy-escalation-demo` branch, which
  uses simulated state and has no authentication.

See [design.md](design.md#trust-and-failure-model) for the full trust and failure model.
