"""Owned-VM fixture and checks against installed code; never a production installer."""

import argparse
import json
import os
import pwd
import subprocess
import time
from datetime import timedelta
from pathlib import Path

import httpx
from cardea.service.client import ClientError, KernelClient
from cardea.service.models import (
    RIGHTS,
    DelegationTaskRequest,
    HostBudget,
    HostTaskRequest,
    Permission,
    Principal,
    Provisioning,
    ServiceConfig,
    now,
)

CONFIG = Path("/etc/cardea/config.json")
FIXTURE = Path("/var/lib/cardea-m5-provider")
OBSERVATIONS = Path("/var/lib/cardea-m5-client/result.json")
SUPERVISOR = "spiffe://cardea.test/supervisor"


def command(*args: str) -> str:
    return subprocess.run(args, capture_output=True, text=True, check=True, timeout=60).stdout


def private(path: Path, text: str, uid: int, gid: int) -> None:
    path.write_text(text)
    path.chmod(0o600)
    os.chown(path, uid, gid)


def bootstrap() -> None:
    # Reuse the already fitted SPIRE bootstrap. Only the process lifetime changes:
    # owned fixture units persist across a real guest reboot.
    from provider_lab import Lab

    class PersistentLab(Lab):
        def launch(self, binary, config):
            unit = f"cardea-m5-{binary}.service"
            after = "After=cardea-m5-spire-server.service\n" if binary == "spire-agent" else ""
            Path(f"/etc/systemd/system/{unit}").write_text(
                f"[Unit]\nDescription=Disposable M5 SPIRE fixture\n{after}"
                f"[Service]\nExecStart={self.bin / binary} run -config {config}\n"
                "Restart=on-failure\nRestartSec=3\nUMask=0077\n"
                "[Install]\nWantedBy=multi-user.target\n"
            )
            command("systemctl", "daemon-reload")
            command("systemctl", "enable", "--now", unit)
            return None

    assert os.getuid() == 0
    assert not CONFIG.exists(), "Bootstrap requires a fresh unconfigured installation"
    lab = PersistentLab()
    lab.temp.cleanup()
    FIXTURE.mkdir(mode=0o711)
    lab.root = FIXTURE
    lab.admin = str(FIXTURE / "server.sock")
    lab.socket = f"unix://{FIXTURE}/agent.sock"
    lab.start()
    account = pwd.getpwnam("cardea-kernel")
    lab.register("spiffe://cardea.test/runtime/a", uid=account.pw_uid, ttl=120)
    lab.register(SUPERVISOR, uid=account.pw_uid, ttl=120)
    config = ServiceConfig(
        data_dir=Path("/var/lib/cardea"),
        spiffe_socket=lab.socket,
        trust_domain="cardea.test",
        audience="cardea-kernel-fit",
        runtime_spiffe_id="spiffe://cardea.test/runtime/a",
        host_execution=True,
    )
    private(CONFIG, config.model_dump_json(), account.pw_uid, account.pw_gid)
    rights = (*RIGHTS, "tasks.delegate", "host.copy", "host.sha256")
    permissions = tuple(
        Permission(
            principal_id="supervisor",
            namespace="test",
            operation=r,
            delegate=True,
            expires_at=now() + timedelta(hours=8),
        )
        for r in rights
    )
    document = Provisioning(
        principals=(
            Principal(
                principal_id="runtime-a",
                kind="service",
                auth_kind="spiffe",
                issuer="cardea.test",
                subject=config.runtime_spiffe_id,
            ),
            Principal(
                principal_id="supervisor",
                kind="service",
                auth_kind="spiffe",
                issuer="cardea.test",
                subject=SUPERVISOR,
            ),
            Principal(principal_id="agent-a", kind="agent", runtime_id="runtime-a"),
            Principal(principal_id="agent-b", kind="agent", runtime_id="runtime-a"),
        ),
        permissions=permissions,
        platform=tuple(p.model_copy(update={"principal_id": "platform"}) for p in permissions),
        host_budgets=(
            HostBudget(
                principal_id="supervisor",
                namespace="test",
                tool_milliseconds=1000000,
                output_bytes=10000000,
            ),
        ),
    )
    OBSERVATIONS.parent.mkdir(mode=0o700)
    os.chown(OBSERVATIONS.parent, account.pw_uid, account.pw_gid)
    policy = OBSERVATIONS.parent / "policy.json"
    private(policy, document.model_dump_json(), account.pw_uid, account.pw_gid)
    for args in (("init",), ("provision", str(policy))):
        command(
            "runuser",
            "-u",
            "cardea-kernel",
            "--",
            "/usr/bin/cardea",
            *args,
            "--config",
            str(CONFIG),
        )
    override = Path("/etc/systemd/system/cardea-kernel.service.d")
    override.mkdir()
    (override / "fixture.conf").write_text(
        "[Unit]\nAfter=cardea-m5-spire-agent.service\nRequires=cardea-m5-spire-agent.service\n"
    )
    command("systemctl", "daemon-reload")
    command("systemctl", "enable", "--now", "cardea-kernel.service")
    lab.client.close()
    print(json.dumps({"fixture": "real SPIRE 1.15.3", "package_initialized": True}))


def wait(check, timeout=90):
    deadline = time.monotonic() + timeout
    while True:
        try:
            return check()
        except (AssertionError, ClientError, OSError, httpx.TransportError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.5)


def acceptance(phase: str) -> None:
    assert os.getuid() == pwd.getpwnam("cardea-kernel").pw_uid
    config = ServiceConfig.model_validate_json(CONFIG.read_bytes())
    client = KernelClient.for_workload(
        config.data_dir / "kernel.sock", config.spiffe_socket, SUPERVISOR, config.audience
    )
    request = DelegationTaskRequest.model_validate(
        {
            "request_id": "m5-installed-delegation",
            "agent_id": "agent-a",
            "namespace": "test",
            "objective": "Verify packaged async A2A and confined host effects",
            "grants": [*RIGHTS, "tasks.delegate", "host.sha256", "host.copy"],
            "children": [
                {"agent_id": "agent-b", "operation": "host.sha256", "payload": {"m5": 1}},
                {"agent_id": "agent-b", "operation": "host.copy", "payload": {"m5": 2}},
            ],
        }
    )
    try:
        description = wait(lambda: client.request("GET", "/system"))
        assert description["host_execution"] and description["delegation_execution"]
        if phase == "before":
            task = client.request("POST", "/delegation-tasks", json=request.model_dump(mode="json"))
            task_id = task["task"]["task_id"]

            def complete():
                task = client.inspect(task_id).task
                assert task.state.value == "succeeded"
                assert task.verification is not None and task.verification.passed

            wait(complete, 120)
            output = client.request("GET", f"/tasks/{task_id}/output")
            children = client.request("GET", f"/tasks/{task_id}/children")
            # Persist a real supervisor revocation before the machine reboot.
            policy = OBSERVATIONS.parent / "policy.json"
            document = Provisioning.model_validate_json(policy.read_bytes())
            document = document.model_copy(
                update={
                    "permissions": tuple(
                        p for p in document.permissions if p.operation != "host.copy"
                    )
                }
            )
            policy.write_text(document.model_dump_json())
            command("/usr/bin/cardea", "provision", str(policy), "--config", str(CONFIG))
            result = {
                "task_id": task_id,
                "output": output,
                "children": children,
                "receipt": client.request("GET", f"/tasks/{task_id}/receipt"),
                "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            }
            OBSERVATIONS.write_text(json.dumps(result, indent=2) + "\n")
        else:
            result = json.loads(OBSERVATIONS.read_text())
            assert Path("/proc/sys/kernel/random/boot_id").read_text().strip() != result["boot_id"]
            assert client.inspect(result["task_id"]).task.state.value == "succeeded"
            assert client.request("GET", f"/tasks/{result['task_id']}/output") == result["output"]
            assert (
                client.request("GET", f"/tasks/{result['task_id']}/children") == result["children"]
            )
            assert (
                client.request("GET", f"/tasks/{result['task_id']}/receipt") == result["receipt"]
            )
            # A new post-boot worker succeeds under the remaining authority.
            host = HostTaskRequest.model_validate(
                {
                    "request_id": "m5-after-reboot",
                    "agent_id": "agent-a",
                    "namespace": "test",
                    "objective": "Post-reboot host execution",
                    "payload": {"boot": 2},
                    "operation": "host.sha256",
                    "grants": [*RIGHTS, "host.sha256"],
                }
            )
            task_id = client.request("POST", "/host-tasks", json=host.model_dump(mode="json"))[
                "task"
            ]["task_id"]

            def succeeded():
                task = client.inspect(task_id).task
                assert task.state.value == "succeeded"
                assert task.verification is not None and task.verification.passed

            wait(succeeded, 90)
            result.update(
                reboot_verified=True,
                post_reboot_worker=task_id,
                kernel=command("uname", "-r").strip(),
            )
        try:
            client.request(
                "POST",
                "/delegation-tasks",
                json=request.model_copy(update={"request_id": f"m5-revoked-{phase}"}).model_dump(
                    mode="json"
                ),
            )
        except ClientError as error:
            assert error.status == 403
        else:
            raise AssertionError("Revoked supervisor operation was accepted")
        result[f"{phase}_revocation_denied"] = True
        OBSERVATIONS.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps({"phase": phase, "passed": True, "task_id": result["task_id"]}))
    finally:
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("bootstrap", "before", "after"))
    args = parser.parse_args()
    assert Path("/etc/cardea-disposable-m2-vm").read_text().strip() == "yes"
    bootstrap() if args.phase == "bootstrap" else acceptance(args.phase)
