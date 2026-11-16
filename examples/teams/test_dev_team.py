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
    assert "was refused APPROVE" in printed and "the gate refused the push" in printed
    assert "pools principal, product, lead, dev, security" in printed
    assert "security.0 review -> principal: done" in printed
