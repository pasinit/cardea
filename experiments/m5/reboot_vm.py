"""Run installed-package acceptance on an explicitly owned disposable Debian VM."""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "m2"))
import vm  # noqa: E402

ENV = "PYTHONPATH=/usr/lib/cardea:/opt/m5-fixture SPIRE_BIN=/opt/spire-1.15.3/bin"
PYTHON = "/opt/venvs/cardea-kernel/bin/python"
FIXTURE = "/opt/m5-fixture/installed_acceptance.py"


def run(state: Path) -> None:
    vm.ssh(state, 'test "$(cat /etc/cardea-disposable-m2-vm)" = yes')
    vm.ssh(state, "test ! -e /etc/cardea/config.json && test ! -e /var/lib/cardea/kernel.sqlite3")
    # The package must have been installed inactive, without policy or enrollment.
    assert (
        vm.ssh(
            state, "systemctl is-enabled cardea-kernel.service || true", capture_output=True
        ).stdout.strip()
        == "disabled"
    )
    assert (
        vm.ssh(
            state, "systemctl is-active cardea-kernel.service || true", capture_output=True
        ).stdout.strip()
        == "inactive"
    )
    for account in ("cardea-worker", "cardea-fit"):
        vm.ssh(
            state,
            f"! runuser -u {account} -- sudo -n /usr/libexec/cardea-host </dev/null",
            capture_output=True,
        )
    vm.ssh(
        state,
        "mkdir -p /tmp/cardea-m5-untrusted/cardea && "
        "printf '%s\\n' 'raise RuntimeError(\"untrusted import\")' "
        "> /tmp/cardea-m5-untrusted/cardea/__init__.py && "
        "cd /tmp/cardea-m5-untrusted && runuser -u cardea-kernel -- "
        "env PYTHONPATH=/tmp/cardea-m5-untrusted cardea --help >/root/m5-cli-help.log",
    )
    vm.ssh(
        state, f"env {ENV} {PYTHON} {FIXTURE} bootstrap > /root/m5-bootstrap.log 2>&1", timeout=180
    )
    vm.ssh(state, "! runuser -u cardea-worker -- cat /etc/cardea/config.json", capture_output=True)
    vm.ssh(
        state,
        f"runuser -u cardea-kernel -- env {ENV} {PYTHON} {FIXTURE} before "
        "> /root/m5-before.log 2>&1",
        timeout=240,
    )
    boot = vm.ssh(state, "cat /proc/sys/kernel/random/boot_id", capture_output=True).stdout.strip()
    try:
        vm.ssh(state, "systemctl reboot", timeout=20)
    except subprocess.CalledProcessError as error:
        if error.returncode != 255:  # SSH may close before delivering the command status.
            raise
    deadline = time.monotonic() + 180
    while True:
        try:
            current = vm.ssh(
                state, "cat /proc/sys/kernel/random/boot_id", capture_output=True
            ).stdout.strip()
            if current != boot:
                break
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass
        if time.monotonic() > deadline:
            raise TimeoutError("Guest did not return with a different boot ID")
        time.sleep(2)
    vm.ssh(
        state,
        f"runuser -u cardea-kernel -- env {ENV} {PYTHON} {FIXTURE} after > /root/m5-after.log 2>&1",
        timeout=240,
    )
    evidence = state / "evidence"
    evidence.mkdir(exist_ok=True)
    for remote, name in (
        ("/var/lib/cardea-m5-client/result.json", "reboot.json"),
        ("/root/m5-bootstrap.log", "bootstrap.log"),
        ("/root/m5-before.log", "before.log"),
        ("/root/m5-after.log", "after.log"),
    ):
        (evidence / name).write_text(vm.ssh(state, f"cat {remote}", capture_output=True).stdout)
    print(
        json.dumps(
            {
                "installed_inactive": True,
                "worker_and_unenrolled_account_denied": True,
                "worker_cannot_read_configuration": True,
                "ignores_untrusted_import_path": True,
                "reboot_acceptance": "passed",
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    run(parser.parse_args().state.resolve())
