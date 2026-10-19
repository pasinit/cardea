from collections.abc import Iterator

import pytest
from provider_lab import Lab, eventually

from harness import OUTSIDER, SUPERVISOR, ServiceLab


@pytest.fixture(scope="session")
def provider() -> Iterator[Lab]:
    instance = Lab()
    try:
        instance.start()
        instance.register(SUPERVISOR)
        instance.register(OUTSIDER)
        eventually(lambda: instance.token(SUPERVISOR))
        yield instance
    finally:
        instance.close()


@pytest.fixture
def service(provider: Lab) -> Iterator[ServiceLab]:
    instance = ServiceLab(provider)
    try:
        yield instance
    finally:
        instance.close()

@pytest.fixture(scope="session")
def oidc_provider():
    from oidc_lab import KeycloakLab
    lab = KeycloakLab()
    try:
        lab.start()
        yield lab
    finally:
        lab.close()
