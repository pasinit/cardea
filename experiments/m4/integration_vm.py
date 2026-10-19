"""Upload and run M4 on the owned, persistent Debian acceptance host."""

import argparse
import base64
import importlib.util
import io
import sys
import tarfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "experiments/m2"))
import vm  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "m2_integration", REPO / "experiments/m2/integration_vm.py"
)
assert spec and spec.loader
m2 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m2)


def upload(state: Path) -> None:
    m2.upload(state)
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as archive:
        for path in (REPO / "experiments/m4").glob("*.py"):
            archive.add(path, arcname=str(path.relative_to(REPO)))
    vm.ssh(
        state,
        "base64 -d | tar -xz -C /opt/cardea-src",
        input=base64.b64encode(data.getvalue()).decode(),
    )
    requirements = vm.command(
        [
            "uv",
            "export",
            "--locked",
            "--project",
            str(REPO / "apps/api"),
            "--format",
            "requirements-txt",
            "--no-emit-project",
            "--no-hashes",
        ],
        capture_output=True,
    ).stdout
    vm.ssh(state, "cat > /tmp/cardea-m4-requirements.txt", input=requirements)
    vm.ssh(
        state,
        "/opt/cardea-venv/bin/pip install -r /tmp/cardea-m4-requirements.txt",
        timeout=300,
    )


def run(state: Path, output: Path, selection: str = "") -> None:
    upload(state)
    output.mkdir(parents=True, exist_ok=True)
    select = f" -k {selection}" if selection else ""
    vm.ssh(state, "rm -f /home/cardea-kernel/m4.json")
    try:
        vm.ssh(
            state,
            f"cd /home/cardea-kernel && runuser -u cardea-kernel -- env {m2.ENV} "
            "M4_RUN_EVIDENCE=/home/cardea-kernel/m4.json "
            "/opt/cardea-venv/bin/python -m pytest -xv -p no:cacheprovider "
            f"/opt/cardea-src/experiments/m4/test_integration.py{select} "
            "--junitxml=/home/cardea-kernel/m4.xml > /home/cardea-kernel/m4.log 2>&1",
            timeout=1200,
        )
    finally:
        for suffix in ("xml", "log", "json"):
            try:
                data = vm.ssh(state, f"cat /home/cardea-kernel/m4.{suffix}", capture_output=True)
                (output / f"debian.{suffix}").write_text(data.stdout)
            except Exception:
                pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--selection", default="")
    args = parser.parse_args()
    run(args.state, args.output, args.selection)
