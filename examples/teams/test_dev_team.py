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
    assert "pools principal, product, lead, dev, security" in printed
    assert "security.0 review -> principal: done" in printed
    assert "the client's reply reached its mailbox" in printed
    assert "a stranger's was dropped and counted" in printed


def test_only_the_product_manager_may_email_the_client() -> None:
    for name in ("group.json", "group.restructured.json"):
        pools = dev_team.document(name)["spec"]["pools"]
        senders = [p["name"] for p in pools if "email.send" in p["grants"]]
        assert senders == ["product"], name
    config = dev_team.document("email.json")
    assert set(config["roles"]) == {"dev-team.product"}
    role = config["roles"]["dev-team.product"]
    assert role == {"recipients": ["client"], "rate_limit": {"count": 3, "window_days": 7}}
    # Placeholders only: real addresses live in the host's private configuration.
    assert all(a.endswith(".invalid") for a in config["recipients"].values())
    assert config["transport"]["inbox_id"].endswith(".invalid")
