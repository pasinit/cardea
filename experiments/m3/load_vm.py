"""Install the separate load package and run its matched M3 campaign on an owned VM."""

import argparse
import base64
import io
import json
import sys
import tarfile
from pathlib import Path
from uuid import uuid4

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "experiments/m2"))
import vm  # noqa: E402

ENV = (
    "PYTHONPATH=/opt/cardea-src/packages/cardea-load/src "
    "SPIRE_BIN=/opt/spire-1.15.3/bin"
)


def prepare(state: Path) -> None:
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as archive:
        paths = [REPO / "apps/api/uv.lock", REPO / "packages/cardea-load/uv.lock"]
        paths += list((REPO / "packages/cardea-load/src").rglob("*.py"))
        paths += list((REPO / "experiments/m3").glob("*.py"))
        for path in paths:
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
            str(REPO / "packages/cardea-load"),
            "--no-emit-project",
            "--no-hashes",
            "--no-dev",
        ],
        capture_output=True,
    ).stdout
    vm.ssh(state, "cat > /tmp/m3-load-requirements.txt", input=requirements)
    vm.ssh(
        state,
        "python3 -m venv /opt/cardea-load-venv && "
        "/opt/cardea-load-venv/bin/pip install -r /tmp/m3-load-requirements.txt",
        timeout=180,
    )


def campaign(state: Path, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=False)
    remote = f"/home/cardea-kernel/m3-load-{uuid4().hex}"
    (output / "remote.json").write_text(json.dumps({"directory": remote}))
    command = (
        f"runuser -u cardea-kernel -- env {ENV} "
        "/opt/cardea-load-venv/bin/python -m cardea_load.cli "
    )
    try:
        vm.ssh(
            state,
            f"test ! -e {remote} && install -d -m 700 "
            f"-o cardea-kernel -g cardea-kernel {remote}",
        )
        vm.ssh(
            state,
            command + "agent-run --kind reference --repo /opt/cardea-src "
            "--api-python /opt/cardea-venv/bin/python "
            f"--output {remote}/reference",
            timeout=1500,
        )
        vm.ssh(
            state,
            command + f"agent-baseline --run {remote}/reference/run.json "
            f"--output {remote}/baseline.json",
        )
        vm.ssh(
            state,
            command + "agent-run --kind comparison --repo /opt/cardea-src "
            "--api-python /opt/cardea-venv/bin/python "
            f"--output {remote}/comparison",
            timeout=900,
        )
        vm.ssh(
            state,
            command + f"agent-compare --baseline {remote}/baseline.json "
            f"--run {remote}/comparison/run.json --output {remote}/comparison.json",
        )
    finally:
        for name in (
            "reference/run.json",
            "comparison/run.json",
            "baseline.json",
            "comparison.json",
        ):
            try:
                content = vm.ssh(
                    state, f"cat {remote}/{name}", capture_output=True
                ).stdout
            except Exception:
                continue
            destination = output / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--prepare", action="store_true")
    args = parser.parse_args()
    if args.prepare:
        prepare(args.state)
    if args.output:
        campaign(args.state, args.output)
