# Kernel API and Client Role

Follow root `AGENTS.md` and the [open-source-first review](../../docs/standards-review-2026-09-22.md). R01/R02/R03/R12: evaluate standard authentication, wire/client tooling, and digest compatibility before new transport code. Include selected reuse, bounded exceptions, and unresolved decisions in the handoff.

Read root `coding-standard.md`, `design.md`, and `docs/kernel-contract.md` before implementation.

## Ownership

Own authenticated transport adapters, request/response schemas, generated API contracts, and structured CLI/client behavior. Coordinate with kernel owners on schemas; keep authority and lifecycle logic outside handlers. Do not edit other owners' areas or revert their changes.

## Responsibilities

- Bind every request to an authenticated principal and task; never trust an actor ID in the body.
- Expose only implemented discovery, tasks, objects, operations, events, and authority APIs.
- Return stable errors, bounded responses, version information, operation IDs, and receipts.
- Put agent and supervisor clients through the same authorization path.
- Preserve legacy routes and OpenAPI accuracy in the separate demo app; never mount its unauthenticated routes into the kernel daemon.

Done means typed contracts and tests for authentication, denial, duplicates, errors, and actual implemented routes. Proposed names in the design are not existing endpoints.
