"""The getting-started walkthrough keeps working: `make teams-test`."""

from pathlib import Path

import walkthrough


def test_both_teams_run_from_configuration(tmp_path: Path) -> None:
    assert walkthrough.main(tmp_path) == {"software-team": "done", "trading-desk": "reconciled"}
