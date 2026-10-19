# Installed-host integration: setup and acceptance

The production modules live in `apps/api/src/cardea/hosts` and `cardea.service.host_runtime`. These instructions describe the bounded Debian 13 profile, not a deployment to the user's Mac. Host execution is disabled by default.

The acceptance VM hosts the tested stack together: SPIRE, the Cardea daemon, DBOS, persistent storage and isolated worker processes. It is reused across jobs. An actual deployment runs these components on a persistent Debian machine; the per-job boundary is systemd/bubblewrap confinement on that machine, not a separate Debian VM.

## Disposable acceptance

Prerequisites are those in [README](README.md), plus the verified SPIRE 1.15.3 Linux amd64 musl archive used by M1. Download it from [the upstream release](https://github.com/spiffe/spire/releases/tag/v1.15.3); `integration_vm.py` checks SHA-256 `ca1a4d1155317bdd2afc7f36663828a10410c7c840e54725b90b4064b0a301c7` before extracting it in the guest.

```sh
uv run --locked --project apps/api python experiments/m2/vm.py start --state /tmp/cardea-host-acceptance
uv run --locked --project apps/api python experiments/m2/integration_vm.py \
  --state /tmp/cardea-host-acceptance --spire-archive /absolute/path/spire-1.15.3-linux-amd64-musl.tar.gz
uv run --locked --project apps/api python experiments/m2/vm.py stop --state /tmp/cardea-host-acceptance
```

Provisioning runs once on a new guest. Omit `--spire-archive` to rerun acceptance in an already prepared guest. The runner exports the locked API dependencies, installs them in a private guest virtual environment, copies source, installs the helper, then runs real SPIRE/daemon/helper tests as the unprivileged `cardea-kernel` user. It also exercises the production command builder with hostile fixtures under root-only test instrumentation. VM state, provider credentials and SSH keys stay outside Git. XML/log/JSON evidence is copied into the selected state directory, including on test failure.

## Host-owner installation requirements

The disposable installer deliberately requires the guest marker. For an actual Debian host, package and review these settings through normal host administration:

- Use dedicated locked `cardea-kernel` and `cardea-worker` accounts. Only trusted kernel infrastructure runs as `cardea-kernel`; untrusted worker code runs as `cardea-worker` inside bubblewrap.
- Install the exact `launcher.py` bytes as root-owned, non-writable `/usr/local/libexec/cardea-host`, mode 0755. Its fixed shebang uses Debian Python with isolated mode. Keep every parent directory root-owned and non-writable by the kernel/worker accounts. The service checks the installed helper's source digest against its packaged module.
- Configure root-owned `/etc/cardea-host.json` with the numeric `kernel_uid`, mode 0644. The helper validates the sudo caller against it.
- Authorize only `cardea-kernel ALL=(root) NOPASSWD: /usr/local/libexec/cardea-host ""` in a root-owned mode-0440 sudoers fragment, verified with `visudo`. The empty quoted argument list is required; do not grant arbitrary systemd, Python, shell commands or SETENV.
- Use systemd `257.13-1~deb13u1`, bubblewrap `0.12.0-1~deb13u1`, kernel `6.12.107+deb13-cloud-amd64` on x86_64, user namespaces and writable cgroup v2. Sudo acceptance used `1.9.16p2-3+deb13u2`. The helper also requires writable cgroup v2 and kernel seccomp support. Missing/changed required facilities reject the profile; upgrade support requires fresh conformance evidence.
- Initialize/provision the existing kernel normally; set `host_execution: true` in its private `ServiceConfig`. Grant the supervisor `host.sha256` with delegation authority, corresponding platform permission and the required object/task permissions. M1 default grants remain unchanged. New host work also requires an explicit `host_budgets` allocation as described below.

The helper accepts only probe/start/status/stop/cleanup messages over stdin. It never receives credentials or a caller-supplied command, path or systemd property. The root spool is `/var/lib/cardea-host`; review the cap and retention limits in the [component design](../../docs/components/installed-host-operations.md).

## Submit

Example request file (include only rights the supervisor actually holds and may delegate):

```json
{
  "schema_version": "0.2",
  "request_id": "digest-one",
  "agent_id": "agent-a",
  "namespace": "test",
  "objective": "Compute and verify the digest of declared JSON",
  "operation": "host.sha256",
  "payload": {"hello": "Debian"},
  "grants": ["host.sha256", "objects.read", "objects.write", "tasks.inspect", "tasks.cancel", "events.read"]
}
```

Use `python -m cardea.service submit-host --socket /private/kernel.sock --session /private/session.json request.json`, or the existing workload credential flags instead of `--session`. Inspect the returned task ID through the normal `inspect`, `output`, `receipt`, `events` and `cancel` commands. A repeated request ID returns the same authorized task; changed arguments conflict. Host requests reject child-task bindings, event waits and unknown operation names.

## Hash an existing stored object

Obtain the `artifact` reference from a completed task's authenticated `output` response. The host owner adds exact-version read permission to the existing provisioning document, then applies the full document as usual. Preserve its namespace permissions; the supervisor needs allow/delegate and the platform needs allow for `objects.read` in the source namespace as well as the normal host/task rights in the destination namespace.

```json
{
  "object_permissions": [{
    "principal_id": "supervisor",
    "object": {
      "schema_version": "0.1",
      "namespace": "test",
      "resource_id": "task-REPLACE.output",
      "version": 1,
      "content_digest": "REPLACE_WITH_ARTIFACT_DIGEST"
    },
    "operation": "objects.read",
    "delegate": true,
    "expires_at": "REPLACE_WITH_FUTURE_UTC_TIMESTAMP"
  }],
  "object_platform": [{
    "principal_id": "platform",
    "object": {
      "schema_version": "0.1",
      "namespace": "test",
      "resource_id": "task-REPLACE.output",
      "version": 1,
      "content_digest": "REPLACE_WITH_ARTIFACT_DIGEST"
    },
    "operation": "objects.read",
    "expires_at": "REPLACE_WITH_FUTURE_UTC_TIMESTAMP"
  }]
}
```

This is a fragment to merge into the complete provisioning document; placeholders must be replaced. Provisioning replaces all current policy. Omitting either new field removes its object grants. There are no wildcard or latest-version grants. Namespace-only rights cannot authorize this input mode, and agent requests cannot provision their own permissions.

Submit the same host request metadata/grants as above with a fresh `request_id`, omit `payload` (or use `{}`), and add `input_object` containing the exact artifact reference. Use `python -m cardea.service submit-host-object --socket /private/kernel.sock --session /private/session.json request.json`, `KernelClient.submit_host_object(HostObjectTaskRequest(...))`, or `POST /v0.2/host-object-tasks`. The worker hashes the stored bytes without JSON re-encoding. Inputs remain capped at 16 KiB. The source reference is included in the receipt; task output, inspection and cancellation use the existing routes.

The original [ADR-013 report](INTEGRATION-REPORT.md) remains historical inline-input evidence. See the [object-input report](OBJECT-INPUT-REPORT.md) and [ADR-014](../../docs/decisions/014-object-scoped-host-inputs.md) for exact-version input acceptance and limits.

## M2 allocations, copy and cleanup

Run `python -m cardea.service init --config /private/config.json` with the service stopped before starting the new daemon. This creates a pre-migration backup and applies `m2_001`; old v0.1 records and request digests remain intact. Reinstall the updated fixed helper using the host-owner requirements above. The API checks its exact source digest. Never remove `/var/lib/cardea-host/.retired.sqlite3`: it preserves retired job identities across kernel-store restores.

Add this field to the complete host-owner provisioning document (limits are cumulative, not periodically reset):

```json
"host_budgets": [{
  "principal_id": "supervisor",
  "namespace": "test",
  "tool_milliseconds": 1000000,
  "output_bytes": 10000000,
  "concurrent_operations": 4
}]
```

Missing allocations deny new host work. Each dispatch needs 10,000 ms and 65,536 bytes available; it reserves them atomically with its intent. Reapplying provisioning never resets spent credits. Known execution settles measured worker time/output; unknown consumption retains the maximum. Inspect the authenticated caller's limits, charges, active reservations and cleanup backlog with `KernelClient.request("GET", "/host-budget?namespace=test")`.

To copy declared bytes instead of hashing them, grant `host.copy` to the supervisor/platform, include it in explicit task grants, and set `operation` to `host.copy` on either existing host submission endpoint. Stored input still requires the exact version grant. The verified output has `content_base64` and `bytes`, published only under the admitted task's output name. Clients cannot choose an arbitrary destination path or overwrite another task's object.

Payload cleanup is automatic after terminal receipt commits, retried at service startup and every five seconds. It confirms stop, records a durable replay marker, then removes staging/output files. A cleanup failure preserves a visible backlog and the 256-job staging cap; receipt/tombstone metadata remains. Budget inspection and authority-filtered operation discovery help agents distinguish missing permission, exhausted capacity and installed behavior. General package installation, provider accounting and child host execution are outside M2.
