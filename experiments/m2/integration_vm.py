"""Copy current sources and run the installed-host acceptance in an owned M2 VM."""

import argparse
import base64
import hashlib
import io
import subprocess
import tarfile
from pathlib import Path

import vm

REPO = Path(__file__).resolve().parents[2]
SPIRE_SHA256 = "ca1a4d1155317bdd2afc7f36663828a10410c7c840e54725b90b4064b0a301c7"
ENV = (
    "PYTHONPATH=/opt/cardea-src/apps/api/src:/opt/cardea-src/experiments/m1:"
    "/opt/cardea-src/experiments/spire SPIRE_BIN=/opt/spire-1.15.3/bin"
)


def upload(state: Path) -> None:
    buffer = io.BytesIO()
    with tarfile.open(
        fileobj=buffer, mode="w:gz", format=tarfile.USTAR_FORMAT
    ) as archive:
        for directory in (
            "apps/api/src",
            "experiments/m1",
            "experiments/m2",
            "experiments/spire",
        ):
            for path in (REPO / directory).rglob("*"):
                if (
                    path.is_file()
                    and (
                        path.suffix in {".py", ".sh"}
                        or path.name == "incident-tool-schema.json"
                    )
                    and ".venv" not in path.parts
                ):
                    archive.add(path, arcname=str(path.relative_to(REPO)))
    vm.ssh(
        state,
        "mkdir -p /opt/cardea-src && base64 -d | tar -xz -C /opt/cardea-src",
        input=base64.b64encode(buffer.getvalue()).decode(),
        timeout=60,
    )
    vm.ssh(state, "sh /opt/cardea-src/experiments/m2/install-guest-host.sh")


def prepare(state: Path, spire_archive: Path) -> None:
    blob = spire_archive.read_bytes()
    if hashlib.sha256(blob).hexdigest() != SPIRE_SHA256:
        raise ValueError("SPIRE release digest mismatch")
    vm.ssh(
        state,
        "test $(cat /etc/cardea-disposable-m2-vm) = yes && "
        "apt-get update -qq && apt-get install -y --no-install-recommends "
        "python3-venv=3.13.5-1 sudo=1.9.16p2-3+deb13u2 && "
        "useradd --system --no-create-home --user-group --shell /usr/sbin/nologin cardea-worker && "
        "useradd --create-home --user-group --shell /bin/bash cardea-kernel && "
        "python3 -m venv /opt/cardea-venv",
        timeout=180,
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
    vm.ssh(state, "cat > /tmp/cardea-requirements.txt", input=requirements)
    vm.ssh(
        state,
        "/opt/cardea-venv/bin/pip install -r /tmp/cardea-requirements.txt",
        timeout=300,
    )
    vm.ssh(
        state,
        "base64 -d | tar -xz -C /opt",
        input=base64.b64encode(blob).decode(),
        timeout=120,
    )


def acceptance(state: Path) -> None:
    upload(state)
    try:
        vm.ssh(
            state,
            f"cd /home/cardea-kernel && runuser -u cardea-kernel -- env {ENV} "
            "/opt/cardea-venv/bin/python "
            "-m pytest -x -v -p no:cacheprovider "
            "/opt/cardea-src/experiments/m2/test_integration.py "
            "--junitxml=/home/cardea-kernel/m2-integration.xml "
            "> /home/cardea-kernel/m2-integration.log 2>&1",
            timeout=900,
        )
        vm.ssh(
            state,
            f"env {ENV} /opt/cardea-venv/bin/python "
            "/opt/cardea-src/experiments/m2/production_fit.py "
            "> /root/production-fit.log 2>&1",
            timeout=180,
        )
    finally:
        for remote, local in (
            ("/home/cardea-kernel/m2-integration.xml", "integration.xml"),
            ("/home/cardea-kernel/m2-integration.log", "integration.log"),
            ("/root/production-fit-result.json", "production-fit.json"),
            ("/root/production-fit.log", "production-fit.log"),
        ):
            try:
                result = vm.ssh(state, f"cat {remote}", capture_output=True)
            except subprocess.CalledProcessError:
                continue
            (state / local).write_text(result.stdout)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--spire-archive", type=Path)
    args = parser.parse_args()
    if args.spire_archive:
        prepare(args.state, args.spire_archive)
    acceptance(args.state)
