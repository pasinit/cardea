"""The real daemon end to end under the development identity, plus that identity's guards."""

import base64
import json
from pathlib import Path

import pytest
from cardea.service.database import Fault
from cardea.service.models import ServiceConfig
from devauth import DevAuthentication, issue
from try_ import run


def config(tmp_path: Path, **changes: object) -> ServiceConfig:
    tmp_path.chmod(0o700)
    values: dict[str, object] = {
        "data_dir": tmp_path,
        "spiffe_socket": "unix:///nonexistent",
        "trust_domain": "try.invalid",
        "runtime_spiffe_id": "spiffe://try.invalid/runtime/kernel",
    }
    return ServiceConfig.model_validate(values | changes)


def test_crash_recovery_and_authority_through_the_real_daemon() -> None:
    result = run(say=lambda line: None)
    assert result["state"] == "succeeded"
    assert result["receipt_preserved_across_crash"] and result["outsider_denied"]


@pytest.mark.parametrize(
    "changes",
    [{"trust_domain": "example.org"}, {"host_execution": True}],
)
def test_development_identity_refuses_unsafe_configurations(
    tmp_path: Path, changes: dict[str, object]
) -> None:
    with pytest.raises(Fault, match="unsafe_development|Use a reserved|cannot enable"):
        DevAuthentication(config(tmp_path, **changes))


def test_tokens_are_bound_to_key_audience_domain_and_expiry(tmp_path: Path) -> None:
    settings = config(tmp_path)
    auth = DevAuthentication(settings)
    subject = "spiffe://try.invalid/supervisor/alice"
    assert auth.validate(issue(settings, subject)).subject == subject
    body, signature = issue(settings, subject).split(".")
    forged = json.loads(base64.urlsafe_b64decode(body)) | {"sub": "spiffe://try.invalid/admin"}
    rejected = [
        base64.urlsafe_b64encode(json.dumps(forged).encode()).decode() + "." + signature,
        issue(settings, subject, seconds=-1),
        issue(settings, "spiffe://example.org/supervisor/alice"),
        issue(settings.model_copy(update={"audience": "other"}), subject),
    ]
    for token in rejected:
        with pytest.raises(Fault, match="could not be validated"):
            auth.validate(token)
    with pytest.raises(Fault):
        auth.validate(issue(settings, subject), kind="oidc")
