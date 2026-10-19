"""Two-process kernel persistence smoke test using only temporary fictional data."""

import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


def run_smoke() -> dict[str, object]:
    with TemporaryDirectory(prefix="cardea-kernel-") as directory:
        command = [sys.executable, "-m", "cardea.kernel.cli", "--directory", directory]
        environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2]))
        before = subprocess.run(
            [*command, "demo-start", "--task-id", "restart-proof"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
            env=environment,
        )
        checkpointed = json.loads(before.stdout)
        assert checkpointed["state"] == "waiting"
        assert (Path(directory) / "kernel.sqlite3").is_file()
        after = subprocess.run(
            [*command, "demo-resume", "restart-proof"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
            env=environment,
        )
        completed = json.loads(after.stdout)
        assert completed["state"] == "succeeded"
        assert completed["checkpoint"] == checkpointed["checkpoint"]
        return {
            "first_process": checkpointed["state"],
            "second_process": completed["state"],
            "checkpoint_preserved": True,
            "verified_artifacts": completed["verification"]["artifacts"],
            "model_calls": 0,
            "temporary_store_removed_on_exit": True,
        }


if __name__ == "__main__":
    print(json.dumps(run_smoke(), indent=2, sort_keys=True))
