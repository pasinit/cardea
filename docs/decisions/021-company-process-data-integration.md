# ADR-021: Cross-Team Process and Data Integration

**Date / owner / milestones:** 2026-09-25; architecture and integration maintainers; M6–M9.

**Status:** broadened product requirement set by the project owner, without external customer evidence. Architecture proposed; technical candidates shortlisted, not selected or tested. Supersedes ADR-020's single-team entry sequence, retaining its reuse research.

**Requirement:** the first product must connect a real company process across departments and systems. Agents need scoped discovery of related records, current state/events, process responsibility and permitted actions, with recovery and live supervisor ceilings. See the [component design](../components/company-integration.md).

## Candidates and primary evidence

Sources and public repository metadata checked 2026-09-25. Versions are observed labels, not evaluated runtime pins. Flowable, Open Workflow Specification and CloudEvents repositories were unarchived and had September 2026 pushes; this is a maintenance signal only. Security/advisory and dependency checks remain open.

| Requirement | Candidate and observed license/version | Fit still to establish |
|---|---|---|
| Existing business-process models and human/system steps | [OMG BPMN 2.0.2](https://www.omg.org/spec/BPMN/2.0.2/); [Flowable engine](https://github.com/flowable/flowable-engine), Apache-2.0, release `flowable-8.0.0` | Supported model subset, Java deployment cost, human-task authority and existing-engine integration; explicit ownership alongside DBOS |
| Portable event/service workflow definitions | [Open Workflow Specification](https://github.com/open-workflow-specification/specification), formerly Serverless Workflow; Apache-2.0, `v1.0.3` | Compare its model and maintained implementations with the actual company process; verify version support, human handoffs and recovery. A specification is not an installed engine |
| Standard external change envelope | [CloudEvents](https://github.com/cloudevents/spec), Apache-2.0, `ce@v1.0.2` | Preserve source IDs/schema and add tested durable ingestion, authorization, deduplication and replay; the envelope does not supply them |
| Database change capture where needed | [Debezium architecture](https://debezium.io/documentation/reference/stable/architecture.html) and [repository](https://github.com/debezium/debezium) | Compare with source webhooks and incremental API reads. Database CDC may not preserve application ACLs. Exact version, artifact license and maintenance review open; GitHub latest-release lookup returned 404, not evidence of abandonment |
| APIs, agent tools and record schemas | Existing OpenAPI/JSON Schema contracts, native MCP, source SDKs and ADR-020 gateway candidates | Actual systems and adapters not selected; test source ACL enforcement, delegated credentials, schema evolution and write reconciliation |

## Direction and custom-scope limit

Integrate systems of record and existing process engines; copy only required state/evidence. Preserve the selected kernel/DBOS execution foundation. Compare reference/query adapters and incremental synchronization before selecting wholesale replication, a new graph database or search engine. Compare process-engine composition and existing application/runtime facilities before inventing a process DSL.

No candidate is rejected by this desk review. Potential Cardea-specific scope is the binding between external records/process instances and existing task authority, receipts, events and verifiers. Reuse standard schemas and maintained parsers/connectors wherever fit allows. Business-specific mappings belong in versioned adapters; do not map the entire kernel to MCP/A2A.

## Fit tests and verification

Before implementation, select actual company systems and process sources, pin the relevant artifacts and record license/security checks. Exercise source revocation including cached/derived data, backfill plus incremental updates, duplicate/out-of-order events, external process handoffs, definition upgrades and restart after an uncertain write. Demonstrate one authoritative execution owner per transition and no supervisor-ceiling bypass through cross-team linkage.

Only primary-source research and documentation review were performed here. No connector, process importer, schema migration or customer integration has been implemented. Revisit if existing automation solves the complete process more effectively, integration destroys source authority semantics, or execution ownership cannot be made unambiguous.
