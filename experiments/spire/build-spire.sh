#!/usr/bin/env bash
# Native test build only; no services or host-wide SPIRE installation.
set -euo pipefail
spire_source_dir="${SPIRE_SOURCE_DIR:-/tmp/cardea-spire-1.15.3}"
spire_revision="2f7861ae3923caf1f57eb087fc2928d58c0fb1d2"
if [[ ! -d "$spire_source_dir" ]]; then
  git clone --depth 1 --branch v1.15.3 https://github.com/spiffe/spire.git "$spire_source_dir"
fi
[[ "$(git -C "$spire_source_dir" rev-parse HEAD)" == "$spire_revision" ]] || {
  printf 'SPIRE checkout does not match pinned revision.\n' >&2
  exit 1
}
[[ -z "$(git -C "$spire_source_dir" status --porcelain --untracked-files=no)" ]] || {
  printf 'SPIRE checkout has modified tracked source.\n' >&2
  exit 1
}
cd "$spire_source_dir"
GOTOOLCHAIN=auto go build -o bin/spire-server ./cmd/spire-server
GOTOOLCHAIN=auto go build -o bin/spire-agent ./cmd/spire-agent
printf 'Built SPIRE in %s/bin\n' "$spire_source_dir"
