#!/bin/sh
# ADR-023 amendment acceptance: long confined calls on the real helper (see long_call.py).
# Runs as root on a Debian 13 dev host with the small-host kit's confined workers installed
# (deploy/small-host/README.md, section 4, including the `coding` profile and the
# `code-session` worker). It spends no model usage: the harness is a sleeping command.
#
#   sudo sh experiments/debian-acceptance/long_call.sh [WORK_DIR] [-- long_call.py options]
#
# It installs a fixture repository at /opt/cardea-code/repos/acceptance.git and, for the
# run only, the test's code.session configuration; the previous /opt/cardea-code/coding.json
# is restored on exit, also after an interrupted run. Running it again starts over.
set -eu
WORK="${1:-/var/tmp/cardea-acceptance/long-call}"
[ "$#" -gt 0 ] && shift
[ "${1:-}" = "--" ] && shift
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="${CARDEA_PYTHON:-/opt/cardea/apps/api/.venv/bin/python}"
CODE=/opt/cardea-code
FIXTURE="$CODE/repos/acceptance.git"
CONFIG="$CODE/coding.json"
SAVED="$CODE/coding.json.before-acceptance"

[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 2; }
for need in /usr/libexec/cardea-host /etc/cardea-host-workers.json "$PY"; do
  [ -e "$need" ] || { echo "missing: $need" >&2; exit 2; }
done
grep -q '"coding"' /etc/cardea-host-workers.json || { echo "no coding profile" >&2; exit 2; }
grep -q '"code-session"' /etc/cardea-host-workers.json || { echo "no code-session worker" >&2; exit 2; }
"$PY" -I -c 'import cardea_coding_ops' || { echo "cardea-coding-ops not installed" >&2; exit 2; }
id cardea-kernel >/dev/null 2>&1 || { echo "no cardea-kernel user" >&2; exit 2; }

# On exit: the owner's configuration back, or none if there was none before.
restore_config() {
  if [ -e "$SAVED" ]; then mv "$SAVED" "$CONFIG"; else rm -f "$CONFIG"; fi
}
# An interrupted earlier run left its backup: put the owner's configuration back first.
if [ -e "$SAVED" ]; then mv "$SAVED" "$CONFIG"; fi

# The fixture: a bare repository with one commit on main, owned by this test alone.
install -d -o root -g root -m 0755 "$CODE" "$CODE/repos"
rm -rf "$FIXTURE"
SCRATCH="$(mktemp -d)"
git -C "$SCRATCH" init --quiet --initial-branch=main
echo "acceptance fixture" > "$SCRATCH/README"
git -C "$SCRATCH" add README
git -C "$SCRATCH" -c user.name=fixture -c user.email=fixture@example.invalid commit --quiet -m fixture
git clone --quiet --bare "$SCRATCH" "$FIXTURE"
rm -rf "$SCRATCH"
chmod -R a+rX "$FIXTURE"

if [ -e "$CONFIG" ]; then cp -a "$CONFIG" "$SAVED"; fi
trap restore_config EXIT INT TERM
"$PY" -I "$HERE/long_call.py" config "$FIXTURE" > "$CONFIG"
chmod 0644 "$CONFIG"

# The kernel user runs its own copy of the scripts, in a directory it owns.
PARENT="$(dirname "$WORK")"
install -d -o cardea-kernel -m 0700 "$PARENT"
COPY="$PARENT/long-call-scripts"
rm -rf "$COPY"
install -d -o root -g root -m 0755 "$COPY"
for file in common.py long_call.py; do install -o root -g root -m 0644 "$HERE/$file" "$COPY/$file"; done
status=0
sudo -u cardea-kernel "$PY" -I "$COPY/long_call.py" run --work "$WORK" "$@" || status=$?
echo "evidence: $WORK/evidence.json (exit $status)"
exit "$status"
