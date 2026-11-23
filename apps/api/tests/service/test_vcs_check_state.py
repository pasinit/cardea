"""`check_state` summarises a head's check runs for `vcs.read_change`."""

import pytest
from cardea_vcs_ops.ops import check_state


def run(conclusion: str | None, status: str = "completed") -> dict[str, str | None]:
    return {"status": status, "conclusion": conclusion}


@pytest.mark.parametrize(
    ("runs", "expected"),
    [
        ([], "none"),
        ([run(None, "in_progress"), run("success")], "pending"),
        ([run("success"), run("neutral"), run("skipped")], "passing"),
        ([run("success"), run("failure")], "failing"),
        ([run("cancelled")], "failing"),
    ],
)
def test_check_state(runs: list[dict[str, str | None]], expected: str) -> None:
    assert check_state(runs) == expected
