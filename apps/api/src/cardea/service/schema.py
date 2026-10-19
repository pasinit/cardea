"""Export the authenticated contract without starting a daemon or contacting providers."""

import json
from typing import cast

from cardea.service.app import create_app
from cardea.service.kernel import Kernel


def schema() -> dict[str, object]:
    # Route/schema construction performs no calls on the kernel; handlers bind it lazily.
    return create_app(cast(Kernel, None)).openapi()


if __name__ == "__main__":
    print(json.dumps(schema(), indent=2, sort_keys=True))
