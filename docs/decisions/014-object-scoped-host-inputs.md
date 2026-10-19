# ADR-014 — Object-scoped host inputs

Date: 2026-09-24. Owner: kernel/host integration. Milestone: M2.
Status: implemented and verified for the bounded stored-input profile. M2 remained in progress at this decision. Subsequently completed for the fixed Debian profile; see the [M2 acceptance report](../../experiments/m2/M2-REPORT.md) and [ADR-015](015-m2-accounting-retention-and-acceptance.md).

## Requirement

An authenticated supervisor can submit the installed `host.sha256` operation with one existing immutable object version as input. The task explicitly names namespace, object ID, version and content digest. Admission, dispatch, monitoring and publication require current supervisor read and delegation rights plus platform permission for that exact version, in addition to the existing namespace/operation ceiling. A namespace grant alone cannot authorize reading another task's stored object. Workers receive bounded bytes, never a caller path, database handle or credential. Interrupted intents retain ADR-013's no-relaunch semantics.

## Candidates and fit

- Reuse locked Casbin 1.43.0 (Apache-2.0) and the exact ACL model selected in ADR-010. [PyCasbin](https://github.com/apache/casbin-pycasbin) remains an active public project; its [model documentation](https://casbin.apache.org/docs/abac/) supports object attributes and explicit matchers. Exact immutable object identities only require its existing equality matcher. A local fit on 2026-09-24 passed the permitted tuple and denied namespace, object and version substitutions and namespace-only matching. No dependency upgrade is needed; ADR-010 records the pinned license inventory. This is not a vulnerability audit.
- [OpenFGA](https://openfga.dev/docs/modeling/getting-started) remains a maintained Apache-2.0 relationship authorization candidate from ADR-010. This step requires no relationship graph; adding its separately persisted decision boundary would require solving policy/SQLite commit ordering already provided by the selected in-process composition. No new OpenFGA release was installed or benchmarked for this step.
- OPA remains the richer policy-language candidate evaluated in ADR-010. No policy language requirement has arisen beyond exact equality and the existing live ceiling. Neither alternative is rejected for future broader policy needs.
- Reuse existing `ObjectVersion`, SQLite immutable versions and digest verification, Pydantic, RFC 8785, and the unchanged systemd/bubblewrap helper from ADR-013. A file/URI resolver would introduce path and race handling with no requirement: these inputs already reside in the object store.

## Decision and compatibility

Compose the existing mechanisms. New provisioning fields declare exact object read policies, represented internally as tagged canonical JSON tuples in Casbin's existing object column (the SQLite `policies.namespace` column). Namespace identifiers cannot contain JSON syntax, so these keys cannot collide with namespace grants. This is internal policy data, not a new external identifier standard or wildcard language. Policy replacement, expiry, revision and transactional ordering remain unchanged; omitted object policies revoke all such grants. No schema migration or database engine is added.

Add a separate `/v0.2/host-object-tasks` request with `input_object` and an empty reserved `payload`, preserving the existing inline request schema, digests and workflow identity. The request's immutable `input_object` is its sole explicit source grant. Source namespace read rights and the exact-version grant both intersect supervisor allow/delegate and platform allow. Root tasks only; reject child execution until shared budgets and transitive resource delegation are implemented. Persist the reference in the operation intent and independently hash its validated bytes. Include source-policy expiry in the worker deadline. The fixed input/output/resource limits and task-owned output path remain unchanged.

## Verification and revisit

Require exact-version success, namespace/object/version substitution denials, missing/expired/revoked supervisor/delegate/platform rights at every boundary, grant replacement, unchanged request digests, corrupt/oversized input rejection, independently checked output, and no relaunch on interruption. Run authenticated object-input acceptance and stale-authority denial on the Debian host. Record results before upgrading status. General resource discovery, host filesystem handles, output grants, relationships, child/shared budgets and spool retention remain outside this bounded step. Revisit when those requirements need more than exact immutable-object ACLs.

## Executed acceptance

The [acceptance report](../../experiments/m2/OBJECT-INPUT-REPORT.md) records 58 portable host tests (35 new), 15 authenticated Debian cases (8 new), 46 M1 real-provider regressions, and 202 backend / 4 frontend tests, with passing lint and type checks. Exact namespace/object/version/digest substitutions, live grant withdrawal, expiry, integrity and size bounds, pinned versions, compatibility and crash fencing pass. The privileged launcher is unchanged from ADR-013; only its already supported bounded byte input is reused. Source hashes, environment inventory and evidence are recorded separately from the historical inline-input acceptance.
