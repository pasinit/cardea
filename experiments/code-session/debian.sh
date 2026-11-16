#!/bin/sh
# Debian fit test for code.session (ADR-037). Runs as root on a Debian 13 dev host where the
# Cardea kernel, the root helper and the `coding` profile are installed (see README.md).
# Not a container: the test needs systemd, bubblewrap and the egress proxy of the real host.
#
#   sudo sh experiments/code-session/debian.sh TOKEN_FILE [OUT_DIR]
#
# TOKEN_FILE holds a Claude subscription token from `claude setup-token`. It is installed as
# the profile's credential, /etc/cardea/credentials/claude-code (root, 0600), and never
# copied anywhere else. The script starts one real session: it spends model usage.
set -eu
TOKEN_FILE="${1:?usage: debian.sh TOKEN_FILE [OUT_DIR]}"
OUT_DIR="${2:-/var/tmp/code-session-fit}"
HERE="$(cd "$(dirname "$0")" && pwd)"
PY=/opt/cardea/apps/api/.venv/bin/python
CODE=/opt/cardea-code
CREDENTIAL=/etc/cardea/credentials/claude-code

[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 2; }
for need in /usr/libexec/cardea-host /etc/cardea-host-workers.json "$PY" "$TOKEN_FILE"; do
  [ -e "$need" ] || { echo "missing: $need" >&2; exit 2; }
done
grep -q '"coding"' /etc/cardea-host-workers.json || { echo "no coding profile" >&2; exit 2; }
grep -q '"code-session"' /etc/cardea-host-workers.json || { echo "no code-session worker" >&2; exit 2; }
"$PY" -I -c 'import cardea_coding_ops' || { echo "cardea-coding-ops not installed" >&2; exit 2; }
[ -d "$CODE/cache" ] || { echo "missing the read-only package cache $CODE/cache" >&2; exit 2; }

# The profile's credential: root-only, loaded into the unit by systemd.
install -d -o root -g root -m 0700 /etc/cardea/credentials
install -o root -g root -m 0600 "$TOKEN_FILE" "$CREDENTIAL"

# The fixture repository and the fit test's code.session configuration, read-only inputs.
install -d -o root -g root -m 0755 "$CODE" "$CODE/repos"
rm -rf "$CODE/repos/fixture.git"
"$PY" -I "$HERE/fit.py" fixture "$CODE/repos/fixture.git"
chmod -R a+rX "$CODE/repos/fixture.git"
if [ -e "$CODE/coding.json" ]; then cp -a "$CODE/coding.json" "$CODE/coding.json.before-fit"; fi
"$PY" -I "$HERE/fit.py" config > "$CODE/coding.json"
chmod 0644 "$CODE/coding.json"

mkdir -p "$OUT_DIR"
# Proxy evidence: the coding port passes the model API only.
P=http://127.0.10.5:3128
{
  echo "api.anthropic.com $(curl -s -o /dev/null -w '%{http_connect}' -x $P https://api.anthropic.com/ || true)"
  echo "pypi.org $(curl -s -o /dev/null -w '%{http_connect}' -x $P https://pypi.org/ || true)"
  echo "github.com $(curl -s -o /dev/null -w '%{http_connect}' -x $P https://github.com/ || true)"
} > "$OUT_DIR/proxy.txt"

chown cardea-kernel "$OUT_DIR"
# The kernel user may not be able to read this checkout; it runs its own copy.
install -o root -g root -m 0644 "$HERE/fit.py" "$OUT_DIR/fit.py"
status=0
sudo -u cardea-kernel "$PY" -I "$OUT_DIR/fit.py" run --out "$OUT_DIR/evidence.json" || status=$?

if [ -e "$CODE/coding.json.before-fit" ]; then mv "$CODE/coding.json.before-fit" "$CODE/coding.json"; fi
echo "evidence: $OUT_DIR/evidence.json and $OUT_DIR/proxy.txt (exit $status)"
exit "$status"
