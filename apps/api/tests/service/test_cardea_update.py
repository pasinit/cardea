"""The unattended update's plan: which changes are hot, which need a drained restart and
which wait for the owner's approval."""

import importlib.util
from pathlib import Path
from typing import Any

SCRIPT = Path(__file__).parents[4] / "deploy/small-host/cardea/cardea-update.py"


def module() -> Any:
    spec = importlib.util.spec_from_file_location("cardea_update", SCRIPT)
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def test_team_kit_dashboard_and_worker_code_are_hot() -> None:
    kinds = module().classify(
        [
            "deploy/team-kit/teamkit.py",
            "deploy/team-kit/dashboard_teams.js",
            "packages/cardea-vcs-ops/src/cardea_vcs_ops/ops.py",
            "apps/api/src/cardea/hosts/operation_worker.py",
            "docs/operations.md",
        ]
    )
    assert kinds == {"root": [], "restart": []}


def test_kernel_code_and_descriptors_need_a_restart() -> None:
    kinds = module().classify(
        [
            "apps/api/src/cardea/service/confined.py",
            "apps/api/uv.lock",
            "packages/cardea-vcs-ops/src/cardea_vcs_ops/descriptors/vcs.merge.json",
        ]
    )
    assert kinds["root"] == []
    assert len(kinds["restart"]) == 3


def test_what_runs_as_root_waits_for_the_owner() -> None:
    update = module()
    kinds = update.classify(
        [update.LAUNCHER, "deploy/small-host/cardea/cardea-update.py", update.WORKERS_SOURCE]
    )
    # New worker entries are synced by the update itself; the helper and this script are not.
    assert kinds["root"] == [update.LAUNCHER, "deploy/small-host/cardea/cardea-update.py"]
    assert kinds["restart"] == [update.LAUNCHER]
