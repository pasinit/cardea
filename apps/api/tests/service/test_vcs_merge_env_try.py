"""`vcs.merge` with the required `env.try` check (`merge.env_try`, ADR-038).

The host has no check runs, so the kernel's own record of a passing `env.try` on exactly the
head is the merge check. The grant to run it is configuration: a role without it is refused,
naming the grant. `env.try` here is an in-process stand-in that passes or fails on demand;
the real worker is covered in test_env_try.py.
"""

# ruff: noqa: F401, F811
# Fixtures are imported from test_vcs_merge, and some are overridden here.

from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
import test_vcs_merge as base
from cardea_env_ops import descriptors as env_descriptors
from cardea_vcs_ops import Vcs, in_process_descriptors
from cardea_vcs_ops.config import MergeConfig, VcsConfig
from cardea_vcs_ops.credentials import PersonalAccessTokens
from cardea_vcs_ops.fake import FakeGitHub
from test_agent_runs import FakeSession
from test_vcs_merge import (
    DEV_OPS,
    PRINCIPAL_OPS,
    act,
    approve,
    head_of,
    host,
    merge,
    open_pull,
    pool,
    refused,
    session,
    tokens_config,
    write_tokens,
)
from test_vcs_ops import Auth, FakeClient, gate_config, text

from cardea.service import agent_run_runtime
from cardea.service import kernel as handlers
from cardea.service.auth import Authentication
from cardea.service.database import Database
from cardea.service.groups import apply
from cardea.service.harnesses import IMPLICIT_ID, Catalog, Harnesses, ModelEntry, implicit_entry
from cardea.service.kernel import Kernel, provision
from cardea.service.models import AgentGroup
from cardea.service.operations import EvidenceQuery, OperationContext, Registry

# Binds or connects over TCP loopback: not run by `make check-test`.
pytestmark = pytest.mark.loopback

TRIED = (*DEV_OPS, "env.try")


class Tries:
    """The stand-in `env.try`: what it passes, and the commits it ran."""

    def __init__(self) -> None:
        self.passed = True
        self.ran: list[str] = []

    async def __call__(
        self, arguments: dict[str, Any], context: OperationContext
    ) -> dict[str, Any]:
        self.ran.append(str(arguments["commit"]))
        return {
            "repo": arguments["repo"],
            "commit": arguments["commit"],
            "cached": False,
            "passed": self.passed,
            "scenarios": [
                {"name": name, "outcome": "pass" if self.passed else "fail"}
                for name in arguments["scenarios"]
            ],
        }


@pytest.fixture
def tries() -> Tries:
    return Tries()


@pytest.fixture
def vcs(host: FakeGitHub, tmp_path: Path) -> Vcs:
    config = VcsConfig(
        credentials=tokens_config(),
        allow_approve=("eng.principal",),
        merge=MergeConfig(reviewer_login=base.OWNER, env_try=("try",)),
    )
    tokens = PersonalAccessTokens(tokens_config(), directory=write_tokens(tmp_path / "creds"))
    return Vcs(config, tokens, transport=host.transport(), registry=host.registry())


@pytest.fixture
def kernel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session: FakeSession,
    vcs: Vcs,
    tries: Tries,
) -> Iterator[Kernel]:
    monkeypatch.setattr(handlers, "DBOSClient", FakeClient)
    monkeypatch.setattr(agent_run_runtime, "POLL_SECONDS", 0.05)
    monkeypatch.setattr(base, "PRINCIPAL_OPS", (*PRINCIPAL_OPS, "env.try"))
    env = tuple(
        d.model_copy(
            update={"cardea": d.cardea.model_copy(update={"executor": None, "handler": d.name})}
        )
        for d in env_descriptors()
    )
    registry = Registry((*in_process_descriptors(), *env), {**vcs.handlers(), "env.try": tries})
    db = Database(tmp_path / "data")
    db.migrate()
    provision(db, base.provisioning(), registry)
    entry = implicit_entry("model-a").model_copy(update={"models": {"model-a": ModelEntry()}})
    harnesses = Harnesses(
        {IMPLICIT_ID: session}, Catalog({IMPLICIT_ID: entry}, IMPLICIT_ID, implicit=IMPLICIT_ID)
    )
    instance = Kernel(db, cast(Authentication, Auth()), operations=registry, harnesses=harnesses)
    instance.content_gate = gate_config(tmp_path)
    group = {
        "apiVersion": "cardea/v1alpha1",
        "kind": "AgentGroup",
        "metadata": {"name": "eng", "namespace": "eng"},
        "spec": {
            "supervisor": "supervisor",
            # `dev` holds env.try by host configuration; `principal` has no grant for it.
            "pools": [pool("dev", 2, TRIED), pool("principal", 1, PRINCIPAL_OPS)],
        },
    }
    apply(
        instance,
        instance.auth.validate("supervisor"),
        "eng",
        AgentGroup.model_validate(group),
        False,
    )
    try:
        yield instance
    finally:
        db.close()


def try_head(kernel: Kernel, session: FakeSession, member: str, commit: str) -> Any:
    _, (result,) = act(
        kernel,
        session,
        member,
        [("env-try", {"repo": "cardea", "commit": commit, "scenarios": ["try"]})],
    )
    return result


def approved(kernel: Kernel, session: FakeSession, host: FakeGitHub, tmp_path: Path) -> int:
    number = open_pull(kernel, session, tmp_path, "eng.dev.0")
    review = approve(kernel, session, number, host)
    assert not review.is_error, text(review)
    return number


@pytest.mark.loopback
def test_a_passing_check_on_the_head_merges(
    kernel: Kernel,
    session: FakeSession,
    host: FakeGitHub,
    tries: Tries,
    tmp_path: Path,
) -> None:
    number = approved(kernel, session, host, tmp_path)
    head = head_of(host, number)
    assert len(head) == 40
    refused(merge(kernel, session, "eng.dev.0", number), "merge_checks_missing")
    tried = try_head(kernel, session, "eng.dev.0", head)
    assert not tried.is_error, text(tried)
    merged = merge(kernel, session, "eng.dev.0", number)
    assert not merged.is_error, text(merged)
    assert merged.structured_content["head_sha"] == head
    assert merged.structured_content["check_receipt"]
    assert tries.ran == [head]


@pytest.mark.loopback
def test_a_failing_check_does_not_merge(
    kernel: Kernel,
    session: FakeSession,
    host: FakeGitHub,
    tries: Tries,
    tmp_path: Path,
) -> None:
    number = approved(kernel, session, host, tmp_path)
    tries.passed = False
    assert not try_head(kernel, session, "eng.dev.0", head_of(host, number)).is_error
    refused(merge(kernel, session, "eng.dev.0", number), "merge_checks_missing")
    assert host.calls.count("merge") == 0


@pytest.mark.loopback
def test_a_changed_head_is_refused_again(
    kernel: Kernel,
    session: FakeSession,
    host: FakeGitHub,
    tries: Tries,
    tmp_path: Path,
) -> None:
    number = approved(kernel, session, host, tmp_path)
    old = head_of(host, number)
    assert not try_head(kernel, session, "eng.dev.0", old).is_error
    new = base.push_directly(host, number, tmp_path)
    assert new != old
    # The approval is stale as well; approve the new head, so only the check is missing.
    assert not approve(kernel, session, number, host).is_error
    refused(merge(kernel, session, "eng.dev.0", number), "merge_checks_missing")
    assert not try_head(kernel, session, "eng.dev.0", new).is_error
    merged = merge(kernel, session, "eng.dev.0", number)
    assert not merged.is_error, text(merged)
    assert merged.structured_content["head_sha"] == new


@pytest.mark.loopback
def test_a_check_of_another_commit_or_scenario_does_not_count(
    kernel: Kernel,
    session: FakeSession,
    host: FakeGitHub,
    tries: Tries,
    tmp_path: Path,
) -> None:
    number = approved(kernel, session, host, tmp_path)
    _, (other,) = act(
        kernel,
        session,
        "eng.dev.0",
        [("env-try", {"repo": "cardea", "commit": "0" * 40, "scenarios": ["try"]})],
    )
    assert not other.is_error, text(other)
    _, (wrong,) = act(
        kernel,
        session,
        "eng.dev.0",
        [
            (
                "env-try",
                {"repo": "cardea", "commit": head_of(host, number), "scenarios": ["teams"]},
            )
        ],
    )
    assert not wrong.is_error, text(wrong)
    refused(merge(kernel, session, "eng.dev.0", number), "merge_checks_missing")


@pytest.mark.loopback
def test_a_role_without_the_grant_is_refused_naming_it(
    kernel: Kernel,
    session: FakeSession,
    host: FakeGitHub,
    tries: Tries,
    tmp_path: Path,
) -> None:
    number = approved(kernel, session, host, tmp_path)
    head = head_of(host, number)
    # The principal pool does not hold env.try, so the tool is not exposed to it.
    denied = try_head(kernel, session, "eng.principal.0", head)
    assert denied.is_error, text(denied)
    assert tries.ran == []
    # The merge says which grant makes the check possible.
    missing = merge(kernel, session, "eng.dev.0", number)
    refused(missing, "merge_checks_missing")
    assert "env.try" in text(missing) and "repo:cardea" in text(missing)
    assert head in text(missing)


@pytest.mark.loopback
def test_host_check_runs_do_not_stand_in_for_the_required_check(
    kernel: Kernel,
    session: FakeSession,
    host: FakeGitHub,
    tmp_path: Path,
) -> None:
    # Green host check runs do not replace the required env.try check.
    number = approved(kernel, session, host, tmp_path)
    host.repositories["acme/cardea"].pass_checks(head_of(host, number), "lint")
    refused(merge(kernel, session, "eng.dev.0", number), "merge_checks_missing")


def merge_at(kernel: Kernel, session: FakeSession, member: str, number: int, head_sha: str) -> Any:
    _, (result,) = act(
        kernel,
        session,
        member,
        [("vcs-merge", {"repository": "cardea", "number": number, "head_sha": head_sha})],
    )
    return result


@pytest.mark.loopback
def test_a_pinned_head_finds_the_check_of_that_head(
    kernel: Kernel,
    session: FakeSession,
    host: FakeGitHub,
    tmp_path: Path,
) -> None:
    number = approved(kernel, session, host, tmp_path)
    head = head_of(host, number)
    refused(merge_at(kernel, session, "eng.dev.0", number, head), "merge_checks_missing")
    assert not try_head(kernel, session, "eng.dev.0", head).is_error
    merged = merge_at(kernel, session, "eng.dev.0", number, head)
    assert not merged.is_error, text(merged)
    assert merged.structured_content["check_receipt"]


@pytest.mark.loopback
def test_a_pinned_stale_head_is_refused_even_with_its_check(
    kernel: Kernel,
    session: FakeSession,
    host: FakeGitHub,
    tmp_path: Path,
) -> None:
    number = approved(kernel, session, host, tmp_path)
    old = head_of(host, number)
    assert not try_head(kernel, session, "eng.dev.0", old).is_error
    base.push_directly(host, number, tmp_path)
    refused(merge_at(kernel, session, "eng.dev.0", number, old), "merge_head_moved")
    assert host.calls.count("merge") == 0


def test_only_the_tries_evidence_is_optional() -> None:
    # Every other evidence query still requires the arguments it matches on.
    assert EvidenceQuery(name="x", operation="y.z", match={"arguments/a": "/a"}).optional is False
    queries = {
        query.name: query.optional
        for descriptor in in_process_descriptors()
        for query in descriptor.cardea.evidence or ()
    }
    assert queries.pop("tries") is True
    assert queries and not any(queries.values())
