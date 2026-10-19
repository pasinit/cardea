# Packages

For new work, follow [open source first](../docs/standards-first.md) and the [decision register](../docs/standards-review-2026-09-22.md). Reuse maintained libraries and standard contracts before custom infrastructure; keep generated contracts faithful to implemented APIs.

Shared generated contracts, schemas, fixtures, or utilities may live here once more than one app needs them.

Do not create shared packages prematurely. Keep code inside `apps/api` until reuse is real.

The M1 authenticated daemon exports `kernel-openapi.json` with `make kernel-openapi`. In its `/v0.2` HTTP resource API, embedded task/receipt records retain their v0.1 version and digest semantics.

The explicitly requested [cardea-load](cardea-load/README.md) package independently installs and runs M1 load campaigns, creates measured immutable baselines, compares subsequent runs and renders result reports. It reuses Vegeta and real M1 fixtures; it is validation tooling, not a new shared application abstraction.
