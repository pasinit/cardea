#!/bin/sh
# Build the staged package inside a fresh debian:13 container of the host's native architecture,
# then install it in a second fresh container and check the installed entry points.
# Usage: sh packaging/container_build.sh STAGE_DIR OUT_DIR [amd64|arm64]
# The architecture defaults to the machine's own; naming another one builds under emulation.
# STAGE_DIR comes from `make m5-stage`. This is a build and install check only: a container is
# not a host fit and establishes no confinement (packaging/README.md).
set -eu
stage=$(cd "$1" && pwd)
mkdir -p "$2"
out=$(cd "$2" && pwd)
case "${3:-$(uname -m)}" in
    x86_64 | amd64) platform=linux/amd64 ;;
    aarch64 | arm64) platform=linux/arm64 ;;
    *) echo "unsupported architecture: ${3:-$(uname -m)}" >&2; exit 2 ;;
esac
image="debian:13"

docker run --rm --pull always --platform "$platform" -v "$stage:/src:ro" -v "$out:/out" "$image" sh -euc '
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends debhelper dh-virtualenv=1.2.2-1.7 \
        python3-venv python3-pip build-essential >/dev/null
    mkdir /build && cp -a /src /build/src
    cd /build/src && dpkg-buildpackage -us -uc -b
    cp /build/*.deb /out/
    dpkg-deb --field /out/*.deb Package Version Architecture
'

docker run --rm --pull always --platform "$platform" -v "$out:/out:ro" "$image" sh -euc '
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq /out/cardea-kernel_*_"$(dpkg --print-architecture)".deb >/dev/null
    dpkg -V cardea-kernel
    id cardea-kernel && id cardea-worker
    test "$(stat -c %a /etc/sudoers.d/cardea-host)" = 440
    test "$(stat -c %a /etc/sudoers.d/cardea-team)" = 440
    getent group cardea-operators >/dev/null
    cardea team --help >/dev/null
    test -f /usr/share/man/man1/cardea-team.1.gz
    /opt/venvs/cardea-kernel/bin/pip check
    cardea --help >/dev/null
    /opt/venvs/cardea-kernel/bin/python -I -c "import sys; sys.path.insert(0, \"/usr/lib/cardea\"); import cardea.service, cardea.hosts.launcher"
    echo "installed and imported on $(dpkg --print-architecture)"
'
