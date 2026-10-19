# Decision Record Template

Create one focused record under `docs/decisions/` before implementing a new mechanism or extending an existing bespoke one. A single record may cover tightly coupled choices. Pure documentation corrections and behavior-preserving fixes can reference an existing decision.

- **ID, date, owner, milestone:** responsible role and affected components.
- **Status:** proposed / selected / implemented / verified / superseded; distinguish design selection from execution evidence.
- **Requirement:** observable behavior, trust boundary, recovery semantics, and acceptance tests.
- **Candidates:** relevant standards and open-source implementations, primary sources, evaluated versions, license, maintenance/security status, and deployment needs. If none exist, record searches and scope.
- **Fit evidence:** what each candidate satisfies, fails, or still needs tested; report unrun tests explicitly.
- **Decision:** reuse/configure/compose/adapt; otherwise exact shortcomings justifying the smallest custom mechanism. Do not label an untested candidate unsuitable.
- **Integration:** authority, state ownership, failure handling, compatibility/migration, operational cost, and dependencies.
- **Verification:** acceptance results and environment; link artifacts where available.
- **Revisit trigger:** conditions that invalidate the choice or allow an exception to be removed.

Link the record from the affected component and roadmap milestone. Apply the [open-source-first policy](standards-first.md). A comparison table without evaluated versions and relevant fit evidence is a shortlist, not a completed selection.
