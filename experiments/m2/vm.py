"""Provision an owned disposable QEMU guest without granting Docker host privileges."""

import argparse
import hashlib
import io
import json
import os
import platform
import shutil
import subprocess
import tarfile
import time
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
# Debian cloud build 20260914-2601 for each guest architecture, pinned from its SHA512SUMS.
# amd64 is the guest the recorded M2-M5 evidence used. arm64 is a development profile: no host
# fit has passed on it yet (packaging/README.md).
IMAGES = {
    "amd64": (
        "https://cloud.debian.org/images/cloud/trixie/20260914-2601/"
        "debian-13-genericcloud-amd64-20260914-2601.qcow2",
        "95e110dfcdbd0ed8a82a75ed9579802f9950cabf51a810dcc6388e81bc778188713878b9f2"
        "8d583a0ea602fbf48b35996ae9ad37f584166d8fbd6489df248f53",
    ),
    "arm64": (
        "https://cloud.debian.org/images/cloud/trixie/20260914-2601/"
        "debian-13-genericcloud-arm64-20260914-2601.qcow2",
        "9eeabc75e74a682fcaff785f6c1f6896b486159397201f51f58298a8f95990f6debb8058b6"
        "969ca95cf6823f6e8058dd6f8589ba940a194b2780cd7f95771d69",
    ),
}
IMAGE_URL, IMAGE_SHA512 = IMAGES["amd64"]
# The emulator image and the QEMU machine for each guest architecture.
EMULATORS = {
    "amd64": ("Dockerfile.vm", "cardea-m2-vm:fit", ["-machine", "q35,accel=tcg"]),
    "arm64": (
        "Dockerfile.vm-arm64",
        "cardea-m2-vm-arm64:fit",
        ["-machine", "virt,accel=tcg", "-bios", "/usr/share/qemu-efi-aarch64/QEMU_EFI.fd"],
    ),
}


def command(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, check=True, timeout=kwargs.pop("timeout", 60), **kwargs)


def docker(*args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    # Caller may select their Docker host/config; no credential helper workaround is global.
    return command(["docker", *args], **kwargs)


def ssh(state: Path, *args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    info = json.loads((state / "vm.json").read_text())
    return command(
        [
            "ssh",
            "-i",
            str(state / "key"),
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "ConnectTimeout=3",
            "-o",
            f"UserKnownHostsFile={state / 'known_hosts'}",
            "-p",
            str(info["port"]),
            "root@127.0.0.1",
            *args,
        ],
        **kwargs,
    )


def start(state: Path, image: Path | None = None, guest: str = "amd64") -> None:
    image_url, image_sha512 = IMAGES[guest]
    dockerfile, tag, machine = EMULATORS[guest]
    state.mkdir(mode=0o700, parents=True, exist_ok=False)
    try:
        base = state / "base.qcow2"
        # Pin content independently of the versioned download URL: mismatch fails closed.
        if image is not None:
            shutil.copyfile(image, base)
        else:
            with urllib.request.urlopen(image_url, timeout=60) as source, base.open("wb") as target:
                shutil.copyfileobj(source, target)
        with base.open("rb") as source:
            assert hashlib.file_digest(source, "sha512").hexdigest() == image_sha512
        base.chmod(0o444)
        command(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(state / "key")])
        key = (state / "key.pub").read_text().strip()
        (state / "user-data").write_text(
            "#cloud-config\nhostname: cardea-m2-fit\ndisable_root: false\nssh_pwauth: false\n"
            "users:\n  - name: root\n    lock_passwd: true\n    ssh_authorized_keys:\n"
            f"      - {key}\nwrite_files:\n  - path: /etc/cardea-disposable-m2-vm\n"
            '    content: "yes"\n'
        )
        (state / "meta-data").write_text("instance-id: cardea-m2-fit\n")
        arch = "arm64" if platform.machine() in {"arm64", "aarch64"} else "amd64"
        docker(
            "build",
            "--platform",
            f"linux/{arch}",
            "-f",
            str(HERE / dockerfile),
            "-t",
            tag,
            str(HERE),
            timeout=600,
        )
        docker(
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            "-v",
            f"{state}:/vm",
            tag,
            "-c",
            "genisoimage -quiet -output /vm/seed.iso -volid cidata -joliet -rock "
            "/vm/user-data /vm/meta-data && qemu-img create -f qcow2 -F qcow2 "
            "-b /vm/base.qcow2 /vm/guest.qcow2 8G",
        )
        container = docker(
            "run",
            "-d",
            "--cpus",
            "2",
            "--memory",
            "3g",
            "--pids-limit",
            "128",
            "-p",
            "127.0.0.1::2222",
            "-v",
            f"{state}:/vm",
            tag,
            *machine,
            "-cpu",
            "max",
            "-smp",
            "2",
            "-m",
            "1536",
            "-display",
            "none",
            "-serial",
            "file:/vm/serial.log",
            "-monitor",
            "none",
            "-drive",
            "file=/vm/guest.qcow2,if=virtio,format=qcow2",
            "-drive",
            # The arm64 virt machine has no IDE bus; cloud-init finds the seed by its label.
            "file=/vm/seed.iso,media=cdrom,readonly=on"
            if guest == "amd64"
            else "file=/vm/seed.iso,if=virtio,format=raw,readonly=on",
            "-nic",
            "user,model=virtio-net-pci,hostfwd=tcp::2222-:22",
            capture_output=True,
        ).stdout.strip()
        (state / "vm.json").write_text(json.dumps({"container": container}))
        binding = docker("port", container, "2222", capture_output=True).stdout.strip()
        info = {
            "container": container,
            "port": int(binding.rsplit(":", 1)[1]),
            "guest": guest,
            "image_sha512": image_sha512,
            "image_url": image_url,
        }
        (state / "vm.json").write_text(json.dumps(info, indent=2) + "\n")
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            try:
                ssh(state, "test -f /etc/cardea-disposable-m2-vm", capture_output=True)
                break
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                time.sleep(2)
        else:
            raise TimeoutError("Guest did not become ready; inspect serial.log")
        ssh(
            state,
            "apt-get update -qq && apt-get install -y --no-install-recommends "
            "bubblewrap=0.12.0-1~deb13u1 && "
            "useradd --system --no-create-home --user-group --shell /usr/sbin/nologin cardea-fit",
            timeout=180,
        )
        print(json.dumps(info, indent=2))
    except BaseException:
        if (state / "vm.json").exists():
            stop(state)
        raise


def stop(state: Path) -> None:
    info = json.loads((state / "vm.json").read_text())
    docker("rm", "-f", info["container"])


def fit(state: Path) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name in ("systemd_profile.py", "host_fit.py"):
            archive.add(HERE / name, arcname=name)
    info = json.loads((state / "vm.json").read_text())
    ssh_args = [
        "ssh",
        "-i",
        str(state / "key"),
        "-o",
        "BatchMode=yes",
        "-o",
        f"UserKnownHostsFile={state / 'known_hosts'}",
        "-p",
        str(info["port"]),
        "root@127.0.0.1",
    ]
    subprocess.run(
        [*ssh_args, "tar -xf - -C /root"], input=buffer.getvalue(), check=True, timeout=30
    )
    try:
        ssh(state, "python3 /root/host_fit.py", timeout=180)
    finally:
        result = ssh(state, "cat /root/host-fit-result.json", capture_output=True).stdout
        (state / "result.json").write_text(result)
    print("Evidence:", state / "result.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "fit", "stop"])
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument(
        "--image", type=Path, help="Optional cached cloud image; digest still checked"
    )
    parser.add_argument(
        "--guest",
        choices=sorted(IMAGES),
        default="amd64",
        help="guest architecture for start (arm64 is a development profile)",
    )
    args = parser.parse_args()
    os.umask(0o077)
    start(args.state.resolve(), args.image, args.guest) if args.action == "start" else {
        "fit": fit,
        "stop": stop,
    }[args.action](args.state.resolve())
