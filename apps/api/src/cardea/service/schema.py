"""Export the authenticated contract without starting a daemon or contacting providers.

`make kernel-openapi` writes it to packages/kernel-openapi.json; a test fails while that file
is stale.
"""

import json
from pathlib import Path
from typing import cast

from cardea.service.app import create_app
from cardea.service.kernel import Kernel

# apps/api/src/cardea/service/schema.py -> repository root.
DOCUMENT = Path(__file__).resolve().parents[5] / "packages" / "kernel-openapi.json"


def schema() -> dict[str, object]:
    # Route/schema construction performs no calls on the kernel; handlers bind it lazily.
    return create_app(cast(Kernel, None)).openapi()


def render() -> str:
    return json.dumps(schema(), indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    print(render(), end="")
