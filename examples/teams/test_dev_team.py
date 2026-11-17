"""The milestone 1 dress rehearsal keeps working: `make teams-test`."""

from pathlib import Path

import dev_team
import pytest


def test_the_dev_team_runs_on_the_real_operation_packages(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    outcome = dev_team.main(tmp_path)
    assert outcome["final_state"] == "done"
    # The checks ran: every run receipted and costed, one gate refusal, no double claim.
    assert outcome["checks"]["gate_refusals"] == 1
    assert outcome["checks"]["runs"] >= 13 and outcome["checks"]["claims"] >= 3
    spend = {a["account"]: a for a in outcome["overview"]["spend"]["accounts"]}
    assert spend["group"]["used_usd"] > 0
    printed = capsys.readouterr().out
    assert "approved it on the host" in printed and "the gate refused the push" in printed
    assert "security.0 was refused APPROVE" in printed
    # Merging (ADR-037 amendment 2026-11-17): the opener merges an approved green change;
    # pending or red CI, another developer, the principal, no approval and a stale approval
    # are each refused with nothing merged.
    for line in (
        "while CI was pending, then red",
        "could not merge #1: dev.0 opened it",
        "the principal could not merge",
        "dev.0 merged its approved #1",
        "before any approval",
        "the approval was stale, no merge",
        "dev.1 merged #",
    ):
        assert line in printed, line
    assert "pools principal, product, lead, dev, security" in printed
    assert "security.0 review -> principal: done" in printed
