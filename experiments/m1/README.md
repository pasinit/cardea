# Authenticated M1 acceptance

These tests execute the production `cardea.service` daemon and HTTPX client with real SPIRE and Keycloak processes. All identities, secrets and data are disposable fixtures. No model, external effect, user account, or UI participates in task execution. See [ADR-010](../../docs/decisions/010-m1-authenticated-kernel.md) and [service quickstart](../../docs/m1-quickstart.md).

## Native macOS

Requirements: Python 3.12, uv, Java 21, and Go for the pinned SPIRE source build. From the repository root:

```sh
experiments/spire/build-spire.sh
curl -fsSL --retry 2 https://github.com/keycloak/keycloak/releases/download/26.7.4/keycloak-26.7.4.tar.gz -o /tmp/cardea-keycloak-26.7.4.tar.gz
printf '%s\n' '04823c336b797a7e18889a44262a7a64e6bf624cbbcc518c85e2622176ff2eee  /tmp/cardea-keycloak-26.7.4.tar.gz' | shasum -a 256 -c -
mkdir -p /tmp/cardea-m1-keycloak
tar -xzf /tmp/cardea-keycloak-26.7.4.tar.gz -C /tmp/cardea-m1-keycloak
make m1-test
make m1-demo
```

`SPIRE_BIN` and `KEYCLOAK_HOME` may select those same pinned builds in other locations. The fixture verifies native SPIRE's embedded clean VCS revision, or the exact official Linux amd64 binary digests. Keycloak test state is copied privately and removed afterward; the code-flow fixture emulates browser handling of secure loopback cookies because HTTPX does not treat HTTP loopback as a secure context. This exception exists only in fixture browser code, not production authentication.

## Debian 13 amd64

With Docker running:

```sh
experiments/m1/debian-build.sh
mkdir -p /tmp/cardea-m1-evidence
docker run --rm --platform linux/amd64 \
  --name cardea-m1-acceptance-run \
  -v /tmp/cardea-m1-evidence:/evidence \
  cardea-m1-acceptance:local
```

The build sends only API sources/tests, dependency locks, and required fixtures. It pins the Python/Debian base image digest, SPIRE 1.15.3 and Keycloak 26.7.4 release archive hashes, and uv 0.11.25. Python libraries come from `apps/api/uv.lock`; Debian packages are obtained from the Debian 13 repositories, with measured versions in the evidence. This reproduces the supported userspace acceptance environment, not a byte-for-byte Debian package snapshot. No host home, credential directory, provider state, or kernel database is mounted. The only host mount receives JUnit evidence.

The official SPIRE Linux binaries carry the expected upstream revision but `vcs.modified=true` release metadata. They are verified against the upstream release archive checksum and independently recorded binary hashes; they are not presented as the clean native source build used on macOS. The original ADR-009 experiment remains unchanged; `provider_lab.py` reuses that fixture with this explicit release-verification adaptation.

## Checks and evidence

The service suite covers authenticated restart/receipt preservation, real OIDC login and refresh, state/nonce/PKCE/code replay denial, audience/provider failures, persistent principal disablement, request conflicts, cross-principal denial, all eight shared commit crash boundaries, admission/dispatch/resume authority, ancestor attenuation, pre-dispatch revocation/expiry/cancellation, terminal/deadline invariants, early events, event pagination, corruption rejection, exclusive ownership, SIGTERM, and whole-store restore.

Portable service tests under `apps/api/tests/service` cover populated v0.1 preservation, interrupted/unknown migrations, private files/restore, JCS compatibility, and OpenAPI/error conformance; `make test`, `make lint`, and `make typecheck` include them. `make kernel-openapi` regenerates the separate authenticated API document.

Pass/fail cases, versions, runtime/build IDs and source hashes are recorded in [evidence](evidence/summary.json). Provider key rotation/bootstrap coverage is retained in [ADR-009](../../docs/decisions/009-spire-bootstrap-fit.md). The Authlib 1.8.0 HTTPX integration emits an upstream deprecation warning; acceptance uses the locked compatible HTTPX version. Dependency license checks are direct-composition evidence, not a transitive vulnerability certification.

No claim of M2 confinement follows from running as a trusted account inside a container. Real model execution, external input ingestion, arbitrary tool dispatch, parent budget accounting, automated history retention, multi-host failover and reboot/package installation remain later gates.
