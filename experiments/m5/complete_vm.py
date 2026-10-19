"""Upgrade/reboot/restore acceptance on the previously enrolled disposable M5 machine."""

import argparse
import json
import shlex
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "m2"))
import vm  # noqa: E402

RUN = "runuser -u cardea-kernel -- "
CLI = RUN + "/usr/bin/cardea"
CONFIG = " --config /etc/cardea/config.json"
PYTHON = "/opt/venvs/cardea-kernel/bin/python"
PHASE = (
    RUN
    + f"env PYTHONPATH=/usr/lib/cardea:/opt/m5-fixture {PYTHON} "
    + "/opt/m5-fixture/operational_acceptance.py "
)


def run(
    state: Path, *, after_reboot: bool = False, after_restore: bool = False
) -> None:
    def command(value: str, timeout: int = 300) -> str:
        return vm.ssh(state, value, capture_output=True, timeout=timeout).stdout

    def phase(name: str) -> None:
        command(PHASE + name + f" > /root/m5-{name}.log 2>&1")
        print(f"{name}: passed", flush=True)

    command('test "$(cat /etc/cardea-disposable-m2-vm)" = yes')
    if not after_reboot and not after_restore:
        assert command("dpkg-query -W -f='${Version}' cardea-kernel") == "0.1.0-1"
        assert (
            command(
                "test -s /var/lib/cardea-m5-client/completion.json && echo ready"
            ).strip()
            == "ready"
        )
        command(
            "dpkg -i /opt/cardea-kernel_0.1.0-2_amd64.deb > /root/m5-upgrade.log 2>&1"
        )
        assert (
            command("systemctl is-active cardea-kernel || true").strip() == "inactive"
        )
        refused = command(
            CLI + " serve" + CONFIG + " > /root/m5-new-schema-refusal.log 2>&1; echo $?"
        )
        assert refused.strip() == "1"
        assert (
            command("cat /root/m5-new-schema-refusal.log").strip()
            == "unsupported_schema"
        )
        command(CLI + " init" + CONFIG + " > /root/m5-migrate.log 2>&1")
        phase("upgraded")
        command(
            "dpkg -i /opt/cardea-kernel_0.1.0-1_amd64.deb > /root/m5-downgrade.log 2>&1"
        )
        refused = command(
            CLI + " serve" + CONFIG + " > /root/m5-old-schema-refusal.log 2>&1; echo $?"
        )
        assert refused.strip() == "1"
        assert (
            command("cat /root/m5-old-schema-refusal.log").strip()
            == "unsupported_schema"
        )
        command(
            "dpkg -i /opt/cardea-kernel_0.1.0-2_amd64.deb > /root/m5-return-to-supported.log 2>&1"
        )
        phase("upgraded")
        snippet = (
            "import sys;sys.path.insert(0,'/usr/lib/cardea');"
            "exec(compile(open('/opt/m5-fixture/interrupt_daemon.py').read(),"
            "'/opt/m5-fixture/interrupt_daemon.py','exec'))"
        )
        unit = f'[Service]\nExecStart=\nExecStart={PYTHON} -I -c "{snippet}"\n'
        command(
            "printf %s "
            + shlex.quote(unit)
            + " > /etc/systemd/system/cardea-kernel.service.d/interruption.conf"
        )
        command("systemctl daemon-reload && systemctl start cardea-kernel")
        phase("before-reboot")
        # The running process keeps the barrier; the next boot uses only the installed daemon.
        command(
            "rm /etc/systemd/system/cardea-kernel.service.d/interruption.conf "
            "&& systemctl daemon-reload"
        )
        boot = command("cat /proc/sys/kernel/random/boot_id").strip()
        try:
            command("systemctl reboot", timeout=20)
        except subprocess.CalledProcessError as error:
            if error.returncode != 255:
                raise
        deadline = time.monotonic() + 240
        while True:
            try:
                if (
                    command("cat /proc/sys/kernel/random/boot_id", timeout=10).strip()
                    != boot
                ):
                    break
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError("Guest did not reboot")
            time.sleep(2)
    else:
        assert command("dpkg-query -W -f='${Version}' cardea-kernel") == "0.1.0-2"
        command(
            "test ! -e /etc/systemd/system/cardea-kernel.service.d/interruption.conf"
        )
    if not after_restore:
        phase("after-reboot")
        command("systemctl stop cardea-kernel")
        phase("snapshot")
        command("systemctl start cardea-kernel")
        phase("after-snapshot")
        command("systemctl stop cardea-kernel")
        phase("restore")
        command("systemctl start cardea-kernel")
    phase("restored-work")
    command(CLI + " diagnostics" + CONFIG + " > /root/m5-diagnostics.log 2>&1")
    command("systemctl stop cardea-kernel")
    phase("maintenance")
    command(CLI + " verify" + CONFIG + " > /root/m5-verify.log 2>&1")
    command(CLI + " prune-backups" + CONFIG + " > /root/m5-retention-dry-run.log 2>&1")
    command("dpkg -V cardea-kernel > /root/m5-package-verification.log")
    command(
        "systemd-analyze verify /usr/lib/systemd/system/cardea-kernel.service "
        "> /root/m5-unit-verification.log 2>&1"
    )
    command(PYTHON + " -m pip check > /root/m5-dependencies.log")
    tombstones = (
        "import json,sqlite3;from pathlib import Path;"
        "r=json.loads(Path('/var/lib/cardea-m5-client/completion.json').read_text());"
        "c=sqlite3.connect('/var/lib/cardea-host/.retired.sqlite3');"
        "jobs={row[0] for row in c.execute('SELECT job FROM retired')};"
        "assert all(r[key].removeprefix('task-') in jobs "
        "for key in ('host_task','post_snapshot_task'));"
        "print(json.dumps({'interrupted_job_retired':True,"
        "'post_snapshot_tombstone_preserved':True,'retired_count':len(jobs)}))"
    )
    command("python3 -c " + shlex.quote(tombstones) + " > /root/m5-tombstones.log")
    evidence = state / "evidence" / "completion"
    evidence.mkdir(parents=True, exist_ok=True)
    for name in (
        "baseline",
        "upgraded",
        "upgrade",
        "new-schema-refusal",
        "migrate",
        "downgrade",
        "old-schema-refusal",
        "return-to-supported",
        "before-reboot",
        "after-reboot",
        "snapshot",
        "after-snapshot",
        "restore",
        "restored-work",
        "diagnostics",
        "verify",
        "retention-dry-run",
        "package-verification",
        "unit-verification",
        "dependencies",
        "maintenance",
        "tombstones",
    ):
        (evidence / f"{name}.log").write_text(command(f"cat /root/m5-{name}.log"))
    (evidence / "result.json").write_text(
        command("cat /var/lib/cardea-m5-client/completion.json")
    )
    (evidence / "package.sha256").write_text(
        command("sha256sum /opt/cardea-kernel_0.1.0-2_amd64.deb")
    )
    print(
        json.dumps(
            {
                "upgrade": True,
                "downgrade_refused": True,
                "reboot": True,
                "restore": True,
                "artifact": "/opt/cardea-kernel_0.1.0-2_amd64.deb",
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument(
        "--after-reboot",
        action="store_true",
        help="Resume the retained post-reboot fixture",
    )
    parser.add_argument(
        "--after-restore", action="store_true", help="Resume retained restored state"
    )
    args = parser.parse_args()
    run(
        args.state.resolve(),
        after_reboot=args.after_reboot,
        after_restore=args.after_restore,
    )
