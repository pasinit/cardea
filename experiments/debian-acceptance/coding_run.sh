#!/bin/sh
# Run one code.session acceptance script on the real helper (long_call.py, gate_isolation.py).
# Runs as root on a Debian 13 dev host with the small-host kit's confined workers (deploy/
# small-host/README.md, section 4, with the `coding` profile and the `code-session` worker).
# It spends no model usage: the harness is a plain command.
#
#   sudo sh experiments/debian-acceptance/coding_run.sh SCRIPT.py [WORK_DIR] [-- options]
#
# For the run only, it installs the fixture repository /opt/cardea-code/repos/acceptance.git,
# the script's gate files under /opt/cardea-code/acceptance-gates, the script's coding file as
# /opt/cardea-code/coding.json, and the script's aliases added to the host's repository
# registry /opt/cardea-code/repositories.json. The owner's coding file and registry are put
# back on exit, also after an interrupted run. Running it again starts over.
set -eu
SCRIPT="${1:?usage: coding_run.sh SCRIPT.py [WORK_DIR] [-- options]}"
shift
NAME="$(basename "$SCRIPT" .py | tr _ -)"
WORK="${1:-/var/tmp/cardea-acceptance/$NAME}"
[ "$#" -gt 0 ] && shift
[ "${1:-}" = "--" ] && shift
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="${CARDEA_PYTHON:-/opt/cardea/apps/api/.venv/bin/python}"
CODE=/opt/cardea-code
FIXTURE="$CODE/repos/acceptance.git"
GATES="$CODE/acceptance-gates"
CONFIG="$CODE/coding.json"
REGISTRY="$CODE/repositories.json"

[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 2; }
[ -f "$HERE/$SCRIPT" ] || { echo "no such script: $SCRIPT" >&2; exit 2; }
for need in /usr/libexec/cardea-host /etc/cardea-host-workers.json "$PY"; do
  [ -e "$need" ] || { echo "missing: $need" >&2; exit 2; }
done
grep -q '"coding"' /etc/cardea-host-workers.json || { echo "no coding profile" >&2; exit 2; }
grep -q '"code-session"' /etc/cardea-host-workers.json || { echo "no code-session worker" >&2; exit 2; }
grep -q 'CARDEA_REPOSITORIES' /etc/cardea-host-workers.json || { echo "the code-session worker has no CARDEA_REPOSITORIES; reinstall the kit's workers file" >&2; exit 2; }
"$PY" -I -c 'import cardea_coding_ops' || { echo "cardea-coding-ops not installed" >&2; exit 2; }
id cardea-kernel >/dev/null 2>&1 || { echo "no cardea-kernel user" >&2; exit 2; }

# An interrupted earlier run left its backups: put the owner's files back first.
for file in "$CONFIG" "$REGISTRY"; do
  if [ -e "$file.before-acceptance" ]; then mv "$file.before-acceptance" "$file"; fi
  if [ -e "$file.none-before-acceptance" ]; then rm -f "$file" "$file.none-before-acceptance"; fi
done
# On exit: the owner's files back, or none where there was none before.
restore() {
  for file in "$CONFIG" "$REGISTRY"; do
    if [ -e "$file.before-acceptance" ]; then mv "$file.before-acceptance" "$file"; fi
    if [ -e "$file.none-before-acceptance" ]; then rm -f "$file" "$file.none-before-acceptance"; fi
  done
  rm -rf "$GATES"
}

# The fixture: a bare repository with one commit on main, owned by these tests alone.
install -d -o root -g root -m 0755 "$CODE" "$CODE/repos"
rm -rf "$FIXTURE"
SCRATCH="$(mktemp -d)"
git -C "$SCRATCH" init --quiet --initial-branch=main
echo "acceptance fixture" > "$SCRATCH/README"
echo "a" > "$SCRATCH/a.txt"
git -C "$SCRATCH" add README a.txt
git -C "$SCRATCH" -c user.name=fixture -c user.email=fixture@example.invalid commit --quiet -m fixture
git clone --quiet --bare "$SCRATCH" "$FIXTURE"
rm -rf "$SCRATCH"
chmod -R a+rX "$FIXTURE"

for file in "$CONFIG" "$REGISTRY"; do
  if [ -e "$file" ]; then cp -a "$file" "$file.before-acceptance"; else : > "$file.none-before-acceptance"; fi
done
trap restore EXIT INT TERM
rm -rf "$GATES"
install -d -o root -g root -m 0755 "$GATES"
"$PY" -I "$HERE/$SCRIPT" gates "$GATES"
chmod -R a+rX "$GATES"
"$PY" -I "$HERE/$SCRIPT" config > "$CONFIG.new"
install -o root -g root -m 0644 "$CONFIG.new" "$CONFIG" && rm -f "$CONFIG.new"
"$PY" -I "$HERE/$SCRIPT" registry "$REGISTRY.before-acceptance" > "$REGISTRY.new"
# The registry must stay a private regular file that nobody else can write.
install -o root -g root -m 0644 "$REGISTRY.new" "$REGISTRY" && rm -f "$REGISTRY.new"

# The kernel user runs its own copy of the scripts, in a directory it owns.
PARENT="$(dirname "$WORK")"
install -d -o cardea-kernel -m 0700 "$PARENT"
COPY="$PARENT/$NAME-scripts"
rm -rf "$COPY"
install -d -o root -g root -m 0755 "$COPY"
for file in "$HERE"/*.py; do
  install -o root -g root -m 0644 "$file" "$COPY/$(basename "$file")"
done
status=0
sudo -u cardea-kernel env CARDEA_REPOSITORIES="$REGISTRY" CARDEA_CODING_CONFIG="$CONFIG" \
  "$PY" -I "$COPY/$SCRIPT" run --work "$WORK" "$@" || status=$?
echo "evidence: $WORK/evidence.json (exit $status)"
exit "$status"
