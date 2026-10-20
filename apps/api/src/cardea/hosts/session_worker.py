"""Inside a confined worker: run one Claude Agent SDK session (ADR-028).

Installed by the host owner as a worker command, for example
``/opt/cardea/venv/bin/python3 -I -m cardea.hosts.session_worker``, under a profile with
``model_api_only`` networking and a credential. systemd loads that single credential into
the unit; bubblewrap exposes it at ``$CARDEA_CREDENTIAL``. ``CARDEA_CREDENTIAL_KIND``
(worker configuration) says whether it is an ``api_key`` or an ``oauth_token``. The session
request arrives on stdin and its result leaves on stdout as ``{"output": {...}}``.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

from cardea.runtime.agent_session import ClaudeAgentSession
from cardea.runtime.harness import HarnessRequest

MAX_INPUT = 131072


def main() -> int:
    document = json.loads(sys.stdin.buffer.read(MAX_INPUT + 1)[:MAX_INPUT])
    credential = Path(os.environ["CARDEA_CREDENTIAL"]).read_text().strip()
    kind = os.environ.get("CARDEA_CREDENTIAL_KIND", "oauth_token")
    if kind == "api_key":
        session = ClaudeAgentSession(model=document["model"], api_key=credential)
    elif kind == "oauth_token":
        session = ClaudeAgentSession(model=document["model"], oauth_token=credential)
    else:
        print("unknown credential kind", file=sys.stderr)
        return 2
    result = asyncio.run(session.run(HarnessRequest.model_validate(document["request"])))
    print(json.dumps({"output": result.model_dump(mode="json")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
