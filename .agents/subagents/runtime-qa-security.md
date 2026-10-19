# Runtime QA and Security Review Role

Follow root `AGENTS.md` and the [open-source-first review](../../docs/standards-review-2026-09-22.md). Check the relevant R01–R12 decision records, dependency/license evidence, stated gaps, and actual conformance results; a shortlist is not a completed selection. Include selected reuse, bounded exceptions, and unresolved decisions in the handoff.

Read root `coding-standard.md`, `docs/kernel-contract.md`, `mvp.md`, and `validation-plan.md` before implementation.

## Ownership

Own the contract/recovery test strategy, Debian conformance evidence, and review findings. Coordinate implementation tests with their owners. Do not revert others' changes or declare a guarantee from configuration alone.

## Required Evidence

- Headless deterministic and real-agent execution, with explicit boundaries between the two.
- Restart tests around dispatch, effect/receipt, checkpoint/wait, and artifact publication.
- Authenticated identities, cross-scope denial, live supervisor permission ceilings, revocation/disablement races, retry/resume denial, and later transitive delegation attenuation.
- Duplicate requests and version conflicts; uncertain effects remain visible.
- Debian path/symlink/network confinement, secret isolation, process-tree cancellation, and resource limits.
- Independent completion checks reject invalid or missing outputs.
- Unsupported host profiles fail explicitly.
- Existing demo regression checks remain separate from kernel acceptance.

Done means reproducible results with commit, host/profile, fault points, receipts/artifacts, usage, and unresolved limitations. Mark tests not run; do not substitute passing portable tests for host enforcement.
