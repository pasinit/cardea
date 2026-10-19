"""Run M3 in an already prepared, owned Debian M2 acceptance host."""

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
        for path in (REPO / "experiments/m3").glob("*.py"):
            archive.add(path, arcname=str(path.relative_to(REPO)))
    vm.ssh(
        state,
        "base64 -d | tar -xz -C /opt/cardea-src",
        input=base64.b64encode(data.getvalue()).decode(),
    )


def run(state: Path, live_key: Path | None = None) -> None:
    upload(state)
    env = m2.ENV
    select = ""
    if live_key:
        from cardea.service.database import private_file

        value = private_file(live_key)
        vm.ssh(
            state,
            "install -o cardea-kernel -g cardea-kernel -m 600 /dev/null "
            "/home/cardea-kernel/model.key && cat > /home/cardea-kernel/model.key",
            input=value.decode(),
        )
        env += " M3_LIVE_KEY_FILE=/home/cardea-kernel/model.key"
        select = " -k test_restart_and_input_during_downtime"
    mode = "live" if live_key else "fixture"
    try:
        vm.ssh(
            state,
            f"cd /home/cardea-kernel && runuser -u cardea-kernel -- env {env} "
            f"M3_RUN_EVIDENCE=/home/cardea-kernel/m3-{mode}.json "
            "/opt/cardea-venv/bin/python -m pytest -x -v -p no:cacheprovider "
            f"/opt/cardea-src/experiments/m3/test_integration.py{select} "
            f"--junitxml=/home/cardea-kernel/m3-{mode}.xml "
            f"> /home/cardea-kernel/m3-{mode}.log 2>&1",
            timeout=900,
        )
    finally:
        for suffix in ("xml", "log", "json"):
            try:
                result = vm.ssh(
                    state,
                    f"cat /home/cardea-kernel/m3-{mode}.{suffix}",
                    capture_output=True,
                )
                (state / f"m3-{mode}.{suffix}").write_text(result.stdout)
            except Exception:
                pass
        if live_key:
            vm.ssh(state, "rm -f /home/cardea-kernel/model.key")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--key-file", type=Path)
    args = parser.parse_args()
    run(args.state, args.key_file)
