"""`GET /v0.2/livez` and `GET /v0.2/readyz`: each readiness check passing and failing."""

import json
import os
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from fastapi.testclient import TestClient
from test_team_status import submit
from test_worked_examples import (
    ENG_POOLS,
    FakeClient,
    configure,
    example_collections,
    example_group,
    setup,
)

from cardea.service import health
from cardea.service.app import create_app
from cardea.service.client import ClientError, KernelClient
from cardea.service.confined import Workers
from cardea.service.kernel import Kernel
from cardea.service.models import now

__all__ = ["setup"]


class Client(FakeClient):
    """The fake DBOS client, with the `check_connection` readiness calls."""

    def __init__(self, reachable: bool = True) -> None:
        self.reachable = reachable

    def check_connection(self) -> None:
        if not self.reachable:
            raise ConnectionError("system database is unreachable")


class Helper:
    """The host helper's `profiles` action; `delay` makes it slow."""

    def __init__(self, delay: float = 0.0, answer: Any = None) -> None:
        self.delay, self.calls = delay, 0
        self.answer = answer if answer is not None else {"profiles": {"p": {}}, "workers": {}}

    def profiles(self) -> Any:
        self.calls += 1
        time.sleep(self.delay)
        return self.answer


def mirror(root: Path, alias: str, age_seconds: float = 0.0) -> Path:
    """A bare mirror's on-disk shape: HEAD, one loose ref and FETCH_HEAD."""
    source = root / f"{alias}.git"
    (source / "refs/heads").mkdir(parents=True)
    (source / "HEAD").write_text("ref: refs/heads/main\n")
    for path in (source / "refs/heads/main", source / "FETCH_HEAD"):
        path.write_text("0" * 40 + "\n")
        at = time.time() - age_seconds
        os.utime(path, (at, at))
    return source


def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **sources: Path) -> None:
    path = tmp_path / "repositories.json"
    document = {"repositories": {alias: {"source": str(s)} for alias, s in sources.items()}}
    path.write_text(json.dumps(document))
    path.chmod(0o600)
    monkeypatch.setenv("CARDEA_REPOSITORIES", str(path))


@pytest.fixture
def ready(
    setup: tuple[Kernel, Any, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Kernel:
    """Both worked-example teams applied, DBOS launched, a fresh mirror, a helper."""
    kernel = setup[0]
    for namespace, supervisor, name in (
        ("eng", "sup-eng", "software-team"),
        ("desk", "sup-desk", "trading-desk"),
    ):
        configure(kernel, namespace, supervisor, example_collections(name), example_group(name))
    kernel.workflows_launched = True
    kernel.client = cast(Any, Client())
    kernel.workers = Workers(cast(Any, Helper()))
    registry(tmp_path, monkeypatch, app=mirror(tmp_path / "mirrors", "app"))
    return kernel


def readyz(kernel: Kernel, who: str | None = "sup-eng", **params: Any) -> Any:
    client = TestClient(create_app(kernel))
    headers = {"Authorization": f"Bearer {who}", "X-Cardea-Auth": "spiffe"} if who else {}
    return client.get("/v0.2/readyz", params=params, headers=headers)


def checks(response: Any) -> dict[str, dict[str, Any]]:
    return {check["name"]: check for check in response.json()["checks"]}


def test_livez_answers_without_a_credential_or_any_state() -> None:
    # No kernel at all: liveness must not reach the store or anything else.
    response = TestClient(create_app(cast(Kernel, None))).get("/v0.2/livez")
    assert response.status_code == 200
    assert response.json() == {"schema_version": "0.2", "status": "ok"}


def test_readyz_requires_a_credential(ready: Kernel) -> None:
    assert readyz(ready, who=None).status_code == 401


def test_a_healthy_kernel_is_ready(ready: Kernel) -> None:
    response = readyz(ready)
    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["ready"] is True and body["as_of"]
    found = checks(response)
    assert {
        "database",
        "workflow_engine",
        "host_helper",
        "repository:app",
        "supervision",
        "group:eng/eng-team",
    } <= set(found)
    assert "groups:other" not in found
    assert found["group:eng/eng-team"]["detail"] == "supervisor sup-eng enabled"
    # The desk's group is outside sup-eng's namespaces: neither named nor counted as paused.
    assert not any("desk" in name for name in found)


def test_the_helper_is_called_afresh_each_time(ready: Kernel) -> None:
    helper = cast(Helper, cast(Workers, ready.workers).host)
    ready.workers.installed()  # type: ignore[union-attr]
    readyz(ready)
    readyz(ready)
    assert helper.calls == 3


def test_dbos_not_launched_or_unreachable_is_not_ready(ready: Kernel) -> None:
    ready.workflows_launched = False
    response = readyz(ready)
    assert response.status_code == 503
    assert response.json()["ready"] is False
    assert checks(response)["workflow_engine"] == {
        "name": "workflow_engine",
        "ok": False,
        "detail": "DBOS is not launched in this process",
    }
    ready.workflows_launched = True
    ready.client = cast(Any, Client(reachable=False))
    assert checks(readyz(ready))["workflow_engine"]["detail"] == "failed: ConnectionError"


def test_a_failing_store_fails_the_database_check(ready: Kernel) -> None:
    class Broken:
        def connect(self) -> Any:
            raise OSError("disk unavailable")

    engine = ready.database.engine
    try:
        ready.database.engine = cast(Any, Broken())
        found = {c["name"]: c for c in health.readiness(ready, "sup-eng")["checks"]}
    finally:
        ready.database.engine = engine
    assert found["database"] == {"name": "database", "ok": False, "detail": "failed: OSError"}
    assert checks(readyz(ready))["database"]["ok"] is True


def test_a_slow_check_fails_and_never_hangs(ready: Kernel) -> None:
    ready.workers = Workers(cast(Any, Helper(delay=2.0)))
    started = time.monotonic()
    result = health.readiness(ready, "sup-eng", seconds=0.3)
    assert time.monotonic() - started < 1.5
    found = {c["name"]: c for c in result["checks"]}
    assert found["host_helper"] == {
        "name": "host_helper",
        "ok": False,
        "detail": "timed out after 0.3 s",
    }
    assert result["ready"] is False
    assert found["database"]["ok"] is True


def test_a_malformed_helper_answer_or_no_host(ready: Kernel) -> None:
    ready.workers = Workers(cast(Any, Helper(answer={"error": "refused"})))
    assert checks(readyz(ready))["host_helper"]["ok"] is False
    ready.workers = None
    assert checks(readyz(ready))["host_helper"] == {
        "name": "host_helper",
        "ok": True,
        "detail": "no confined host configured",
    }


def test_stale_or_unreadable_mirrors_fail(
    ready: Kernel, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stale = mirror(tmp_path / "old", "stale", age_seconds=3600)
    registry(
        tmp_path,
        monkeypatch,
        fresh=mirror(tmp_path / "new", "fresh"),
        stale=stale,
        absent=tmp_path / "absent.git",
    )
    found = checks(readyz(ready))
    assert found["repository:fresh"]["ok"] is True
    assert found["repository:stale"]["ok"] is False
    assert "(limit 900 s)" in found["repository:stale"]["detail"]
    assert found["repository:absent"] == {
        "name": "repository:absent",
        "ok": False,
        "detail": "source is not a readable git repository",
    }
    # The allowed age is the caller's to choose, within bounds.
    assert checks(readyz(ready, mirror_max_age_seconds=7200))["repository:stale"]["ok"] is True
    assert readyz(ready, mirror_max_age_seconds=5).status_code == 422


def test_a_newer_fetch_marker_counts_even_when_refs_did_not_move(
    ready: Kernel, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = mirror(tmp_path / "m", "quiet", age_seconds=7200)
    os.utime(source / "FETCH_HEAD")  # fetched just now, nothing new
    registry(tmp_path, monkeypatch, quiet=source)
    assert checks(readyz(ready))["repository:quiet"]["ok"] is True


def test_no_registry_is_ready(ready: Kernel, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CARDEA_REPOSITORIES")
    found = checks(readyz(ready))
    assert found["repositories"]["ok"] is True
    assert not any(name.startswith("repository:") for name in found)


def strand(kernel: Kernel, task_id: str, expired_seconds: int) -> None:
    """Leave a task `running` on a lease that expired `expired_seconds` ago, as a member
    whose run the kernel stopped driving looks."""
    with kernel.database.engine.begin() as conn:
        snapshot = json.loads(
            conn.exec_driver_sql(
                "SELECT snapshot FROM tasks WHERE task_id=?", (task_id,)
            ).scalar_one()
        )
        expires = now() - timedelta(seconds=expired_seconds)
        started = expires - timedelta(minutes=5)
        snapshot.update(
            state="running",
            created_at=(started - timedelta(minutes=1)).isoformat(),
            updated_at=started.isoformat(),
            lease={
                "lease_id": f"lease-{task_id}",
                "worker_id": "worker-a",
                "acquired_at": started.isoformat(),
                "expires_at": expires.isoformat(),
            },
        )
        conn.exec_driver_sql(
            "UPDATE tasks SET state='running', snapshot=? WHERE task_id=?",
            (json.dumps(snapshot), task_id),
        )


def test_a_task_running_on_an_expired_lease_is_reported(ready: Kernel) -> None:
    grants = ENG_POOLS["dev"][1]
    stuck = submit(ready, "sup-eng", "eng", "eng-team.dev.0", grants)
    recent = submit(ready, "sup-eng", "eng", "eng-team.dev.1", grants)
    strand(ready, stuck, expired_seconds=3600)
    strand(ready, recent, expired_seconds=60)  # inside the grace period
    response = readyz(ready)
    assert response.status_code == 503
    detail = checks(response)["supervision"]["detail"]
    assert stuck in detail and "agent eng-team.dev.0" in detail
    assert recent not in detail
    # Another team's supervisor learns only that one exists, not which.
    other = checks(readyz(ready, who="sup-desk"))["supervision"]
    assert other["ok"] is False
    assert other["detail"] == "1 such task(s) in namespaces you cannot inspect"
    assert stuck not in other["detail"]
    # A longer grace period accepts it.
    assert checks(readyz(ready, lease_grace_seconds=7200))["supervision"]["ok"] is True


def test_a_paused_group_fails_unless_allowed(ready: Kernel) -> None:
    with ready.database.engine.begin() as conn:
        conn.exec_driver_sql("UPDATE principals SET disabled=1 WHERE principal_id='sup-desk'")
    # sup-eng cannot inspect the desk: the paused group is counted, not named.
    response = readyz(ready)
    assert response.status_code == 503
    found = checks(response)
    assert found["groups:other"] == {
        "name": "groups:other",
        "ok": False,
        "detail": "1 paused group(s) in namespaces you cannot inspect",
    }
    assert readyz(ready, allow_paused="true").status_code == 200
    # A caller who may inspect the desk sees it by name.
    with ready.database.engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO policies VALUES ('sup-eng','desk','tasks.inspect','allow',?)",
            (time.time() + 3600,),
        )
    found = checks(readyz(ready))
    assert found["group:desk/desk"] == {"name": "group:desk/desk", "ok": False, "detail": "paused"}
    assert checks(readyz(ready, allow_paused="true"))["group:desk/desk"] == {
        "name": "group:desk/desk",
        "ok": True,
        "detail": "paused (allowed)",
    }
    # The paused supervisor itself cannot authenticate: the host-side check reports that.
    denied = readyz(ready, who="sup-desk")
    assert denied.status_code == 403 and denied.json()["code"] == "principal_disabled"


def test_the_client_returns_an_accepted_error_body_and_raises_others() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v0.2/readyz":
            return httpx.Response(503, json={"ready": False, "checks": []})
        return httpx.Response(403, json={"code": "principal_disabled"})

    client = KernelClient(Path("/nonexistent.sock"), lambda: ("spiffe", "example"))
    client.http = httpx.Client(transport=httpx.MockTransport(answer), base_url="http://cardea")
    assert client.request("GET", "/readyz", accept=(503,)) == {"ready": False, "checks": []}
    with pytest.raises(ClientError) as raised:
        client.request("GET", "/readyz")
    assert raised.value.status == 503
    with pytest.raises(ClientError) as refused:
        client.request("GET", "/system", accept=(503,))
    assert refused.value.code == "principal_disabled"
