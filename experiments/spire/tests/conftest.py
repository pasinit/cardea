"""All services and proof material are owned by this fixture and removed on exit."""

from collections.abc import Iterator

import pytest

from lab import Lab


@pytest.fixture(scope="module")
def lab() -> Iterator[Lab]:
    instance = Lab()
    try:
        instance.start()
        yield instance
    finally:
        instance.close()
