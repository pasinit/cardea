#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
# Send only public source and dependency lock files; exclude local state and credentials.
COPYFILE_DISABLE=1 tar --no-xattrs -cf - apps/api/pyproject.toml apps/api/uv.lock apps/api/src apps/api/tests \
  experiments/m1/Dockerfile experiments/m1/pytest.ini experiments/m1/daemon_probe.py \
  experiments/m1/harness.py experiments/m1/provider_lab.py experiments/m1/oidc_lab.py \
  experiments/m1/tests experiments/spire/binding.py \
  | docker build --platform linux/amd64 -f experiments/m1/Dockerfile -t cardea-m1-acceptance:local -
