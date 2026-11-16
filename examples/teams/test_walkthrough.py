"""The getting-started walkthrough keeps working: `make teams-test`."""

from pathlib import Path

import pytest
import walkthrough


def test_both_teams_run_from_configuration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert walkthrough.main(tmp_path) == {"software-team": "done", "trading-desk": "reconciled"}
    printed = capsys.readouterr().out
    # The supervisor's overview of the software team at the end of its run.
    assert "Overview of eng-team (generation 1" in printed
    assert "collection work: done 1; 0 claimed" in printed
    assert "pool principal" in printed and "journal #" in printed
