# Host and Runtime Adapters Role

Follow root `AGENTS.md` and the [open-source-first review](../../docs/standards-review-2026-09-22.md). R07/R08/R09: compare maintained host launchers, event adapters, and provider/runtime SDKs before custom machinery. Include selected reuse, bounded exceptions, and unresolved decisions in the handoff.

Read root `coding-standard.md`, `design.md`, and `docs/kernel-contract.md` before implementation.

## Ownership

Own scoped host/runtime adapters and their conformance tests when assigned. The old connector role now covers the boundary between the kernel and host/model effects. Do not implement enterprise connectors as the first target. Coordinate interfaces and preserve other contributors' edits.

## Responsibilities

- Report host enforcement capabilities and reject unsupported execution profiles.
- Implement Debian worker confinement, process supervision, descendant termination, resource limits, and controlled artifact publication.
- Keep host-specific calls outside the portable kernel.
- Implement deterministic/model runtime adapters through the same typed operation contract.
- Keep provider credentials in the trusted broker and all worker I/O under declared authority.
- Record actual model/tool usage and explicit provider failures.

Done means real Debian enforcement evidence and deterministic adapter contract tests. macOS unit tests, service configuration text, or connector mocks cannot establish Linux confinement.
