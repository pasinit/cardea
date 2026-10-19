#!/usr/bin/env bash
set -euo pipefail
# Supply a Docker endpoint/config externally; do not change the user's global config.
repo=$(cd "$(dirname "$0")/../.." && pwd)
context=$(mktemp -d /tmp/cardea-load-build.XXXXXX)
trap 'rm -rf "$context"' EXIT
curl -fsSL --max-time 120 --retry 2 \
 https://github.com/tsenart/vegeta/releases/download/v12.13.0/vegeta_12.13.0_linux_amd64.tar.gz \
 -o "$context/vegeta.tar.gz"
printf '%s  %s\n' e8759ce45c14e18374bdccd3ba6068197bc3a9f9b7e484db3837f701b9d12e61 "$context/vegeta.tar.gz" | shasum -a 256 -c -
tar -xzf "$context/vegeta.tar.gz" -C "$context" vegeta
mkdir -p "$context/packages/cardea-load"
for item in src tests profiles pyproject.toml uv.lock; do
 cp -R "$repo/packages/cardea-load/$item" "$context/packages/cardea-load/"
done
docker build --platform linux/amd64 -f "$repo/packages/cardea-load/Dockerfile" \
 --build-arg M1_IMAGE="${M1_IMAGE:-cardea-m1-acceptance:local}" -t cardea-load:m1 "$context"
