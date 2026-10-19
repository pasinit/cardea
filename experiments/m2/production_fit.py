"""Run hostile fixtures against the production command builder in the disposable VM."""

import json
import subprocess
import sys
import time
from pathlib import Path

import host_fit as fit
from cardea.hosts import launcher


def command(unit: str, code: str, **overrides: str) -> list[str]:
    # Root-only test instrumentation, never installed or exposed through the helper.
    launcher.ROOT = fit.ROOT
    job = unit.removeprefix("cardea-fit-").removesuffix(".service")
    args = launcher.command(job, time.time() + 30)
    args[args.index(launcher.WORKER)] = code
    args[args.index(str(fit.ROOT / job / "input"))] = str(fit.ROOT / "input")
    args[args.index(f"--unit=cardea-host-{job}.service")] = f"--unit={unit}"
    # Only the experiment disables successful-unit retention.
    for key, value in overrides.items() | {("RemainAfterExit", "no")}:
        args = [a for a in args if not a.startswith(f"--property={key}=")]
        args.insert(2, f"--property={key}={value}")
    return args


if __name__ == "__main__":
    assert sys.argv[1:] == []
    denied = subprocess.run(
        ["runuser", "-u", "cardea-worker", "--", "sudo", "-n", "/usr/libexec/cardea-host"],
        input='{"action":"probe"}',
        text=True,
        capture_output=True,
    )
    assert denied.returncode != 0
    fit.command = command
    fit.main()
    Path("/root/production-fit-result.json").write_text(
        json.dumps(
            {
                "profile": launcher.PROFILE,
                "checks": fit.RESULTS,
                "passed": True,
                "instrumentation": "Hostile worker fixture; result retention disabled",
            },
            indent=2,
        )
        + "\n"
    )
