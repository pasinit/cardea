#!/bin/sh
# Runs inside a disposable debian:13 container: `docker run --rm -v "$PWD:/fit:ro" -v OUT:/out
# debian:13 sh /fit/debian.sh`. Installs from the lock file with the system Python 3.13 and a
# uv-managed CPython 3.12, then runs the fit tests and records evidence for each.
set -eu
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends python3 python3-venv ca-certificates >/dev/null
python3 -m venv /opt/uv
/opt/uv/bin/pip install -q uv==0.9.5
cp -r /fit /work
cd /work
rm -rf .venv
arch="$(uname -m)"
for py in /usr/bin/python3 3.12; do
  UV_PROJECT_ENVIRONMENT="/tmp/env-$(basename "$py")" /opt/uv/bin/uv sync --locked -q -p "$py"
  UV_PROJECT_ENVIRONMENT="/tmp/env-$(basename "$py")" /opt/uv/bin/uv run --locked -p "$py" \
    pytest -q -p no:cacheprovider
  UV_PROJECT_ENVIRONMENT="/tmp/env-$(basename "$py")" /opt/uv/bin/uv run --locked -p "$py" \
    python -I fit.py --out "/out/debian13-$arch-$(basename "$py").json" >/dev/null
done
