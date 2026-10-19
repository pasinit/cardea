import time
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import pytest

from cardea.service.auth import Authentication
from cardea.service.client import ClientError, KernelClient
from cardea.service.database import Fault
from cardea.service.login import session_credentials, store_session
from cardea.service.models import Principal, Signal
from harness import ServiceLab
from test_service import request


@pytest.fixture
def human_service(provider, oidc_provider):
    service = ServiceLab(provider, oidc=oidc_provider.config())
    human = Principal(principal_id="human", kind="human", auth_kind="oidc",
        issuer=oidc_provider.issuer, subject=oidc_provider.subject)
    document = service.document.model_copy(update={
        "principals": (*service.document.principals, human),
        "permissions": (*service.document.permissions, *(p.model_copy(update={"principal_id": "human"})
             for p in service.document.permissions if p.principal_id == "supervisor"))})
    service.apply(document)
    service.document = document
    session = oidc_provider.login()
    path = service.root / "session.json"
    store_session(path, session)
    client = KernelClient(service.data / "kernel.sock", lambda: session_credentials(path))
    try:
        yield service, client, session, path
    finally:
        client.close()
        service.close()


def test_real_oidc_login_refresh_and_persisted_supervisor(human_service):
    service, client, session, path = human_service
    first = service.start()
    task_id = client.submit(request(wait_for_event=True)).task.task_id
    def waiting():
        assert client.inspect(task_id).task.state.value == "waiting"
    from harness import wait_for
    wait_for(waiting)
    assert client.inspect(task_id).supervisor_id == "human"
    service.stop(first)
    service.start()
    session["token"]["expires_at"] = time.time() - 1
    store_session(path, session)
    assert client.inspect(task_id).supervisor_id == "human"  # Real refresh grant, then introspection.
    client.control(task_id, Signal(request_id="human-resume"))
    wait_for(lambda: assert_succeeded(client, task_id))
    assert path.stat().st_mode & 0o077 == 0


def assert_succeeded(client, task_id):
    assert client.inspect(task_id).task.state.value == "succeeded"


def test_id_token_is_not_an_api_credential(human_service):
    service, _, session, _ = human_service
    service.start()
    client = KernelClient(service.data / "kernel.sock", lambda: ("oidc", session["token"]["id_token"]))
    try:
        with pytest.raises(ClientError) as denied:
            client.request("GET", "/system")
        assert denied.value.status == 401
    finally:
        client.close()


@pytest.mark.parametrize("corruption", ["state", "nonce", "pkce", "replay"])
def test_oidc_protocol_binding(oidc_provider, corruption):
    flow = oidc_provider.flow()
    try:
        callback = oidc_provider.authorize(flow)
        if corruption == "state":
            parts = urlparse(callback)
            query = parse_qs(parts.query)
            query["state"] = ["wrong-state"]
            callback = urlunparse(parts._replace(query=urlencode(query, doseq=True)))
        elif corruption == "nonce":
            flow.nonce = "wrong-nonce"
        elif corruption == "pkce":
            flow.verifier = "wrong-verifier-" * 4
        else:
            assert flow.complete(callback)["subject"] == oidc_provider.subject
        with pytest.raises(Fault) as denied:
            flow.complete(callback)
        assert denied.value.code == "login_failed"
    finally:
        flow.close()


def test_human_disable_survives_restart(human_service):
    service, client, _, _ = human_service
    first = service.start()
    task_id = client.submit(request(wait_for_event=True)).task.task_id
    service.apply(service.document.model_copy(update={"principals": tuple(
        p.model_copy(update={"disabled": True}) if p.principal_id == "human" else p
        for p in service.document.principals)}))
    service.stop(first)
    service.start()
    with pytest.raises(ClientError) as denied:
        client.inspect(task_id)
    assert denied.value.code == "principal_disabled"


def test_wrong_oidc_audience_and_unavailable_provider(human_service):
    service, _, session, _ = human_service
    wrong = service.config.model_copy(update={"oidc": service.config.oidc.model_copy(update={"audience": "other-service"})})
    authentication = Authentication(wrong)
    try:
        with pytest.raises(Fault, match="could not be validated"):
            authentication.validate(session["token"]["access_token"], "oidc")
        assert authentication.oidc_metadata
        authentication.oidc_metadata["introspection_endpoint"] = "http://127.0.0.1:1/unavailable"
        started = time.monotonic()
        with pytest.raises(Fault):
            authentication.validate(session["token"]["access_token"], "oidc")
        assert time.monotonic() - started < 4
    finally:
        authentication.close()
