"""Second same-UID process: report only attested names, never credentials."""

import json
import sys

from spiffe.workloadapi.workload_api_client import WorkloadApiClient

from binding import AUDIENCE

client = WorkloadApiClient(socket_path=sys.argv[1], default_timeout=2)
try:
    print(json.dumps(sorted(str(s.spiffe_id) for s in client.fetch_jwt_svids({AUDIENCE}))))
finally:
    client.close()
