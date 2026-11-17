#!/bin/sh
# ADR-038 acceptance: env.try in the real `try` profile (see env_try.py). NOT YET RUN.
# Runs as root on a Debian 13 dev host with the small-host kit's confined workers installed
# (deploy/small-host/README.md, section 4, including the `try` profile, the `env-try`
# worker and the squid `try` port). It spends no model usage.
#
#   sudo sh experiments/debian-acceptance/env_try.sh [WORK_DIR]
#
# It installs a fixture repository at /opt/cardea-code/repos/env-acceptance.git (three
# commits: good, bad, hostile) and, for the run only, a repository registry naming it; the
# previous /opt/cardea-code/repositories.json is restored on exit, also after an
# interrupted run.
set -eu
WORK="${1:-/var/tmp/cardea-acceptance/env-try}"
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="${CARDEA_PYTHON:-/opt/cardea/apps/api/.venv/bin/python}"
CODE=/opt/cardea-code
FIXTURE="$CODE/repos/env-acceptance.git"
REGISTRY="$CODE/repositories.json"
SAVED="$CODE/repositories.json.before-env-try"

[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 2; }
for need in /usr/libexec/cardea-host /etc/cardea-host-workers.json "$PY"; do
  [ -e "$need" ] || { echo "missing: $need" >&2; exit 2; }
done
grep -q '"env-try"' /etc/cardea-host-workers.json || { echo "no env-try worker" >&2; exit 2; }
"$PY" -I -c 'import cardea_env_ops' || { echo "cardea-env-ops not installed" >&2; exit 2; }
id cardea-kernel >/dev/null 2>&1 || { echo "no cardea-kernel user" >&2; exit 2; }

restore() {
  if [ -e "$SAVED" ]; then mv "$SAVED" "$REGISTRY"; else rm -f "$REGISTRY"; fi
}
if [ -e "$SAVED" ]; then mv "$SAVED" "$REGISTRY"; fi

# The fixture: three commits whose `mode` file decides what the scenario does.
install -d -o root -g root -m 0755 "$CODE" "$CODE/repos"
rm -rf "$FIXTURE"
SCRATCH="$(mktemp -d)"
git -C "$SCRATCH" init --quiet --initial-branch=main
"$PY" -I "$HERE/env_try.py" scenario > "$SCRATCH/scenario.py"
commit() {
  echo "$1" > "$SCRATCH/mode"
  git -C "$SCRATCH" add mode scenario.py
  git -C "$SCRATCH" -c user.name=fixture -c user.email=fixture@example.invalid \
    commit --quiet -m "$1"
  git -C "$SCRATCH" rev-parse HEAD
}
GOOD="$(commit good)"
BAD="$(commit bad)"
HOSTILE="$(commit hostile)"
git clone --quiet --bare "$SCRATCH" "$FIXTURE"
rm -rf "$SCRATCH"
chmod -R a+rX "$FIXTURE"

if [ -e "$REGISTRY" ]; then cp -a "$REGISTRY" "$SAVED"; fi
trap restore EXIT INT TERM
"$PY" -I "$HERE/env_try.py" repositories "$FIXTURE" > "$REGISTRY"
chmod 0644 "$REGISTRY"

PARENT="$(dirname "$WORK")"
install -d -o cardea-kernel -m 0700 "$PARENT"
COPY="$PARENT/env-try-scripts"
rm -rf "$COPY"
install -d -o root -g root -m 0755 "$COPY"
for file in common.py env_try.py; do install -o root -g root -m 0644 "$HERE/$file" "$COPY/$file"; done
printf '{"good": "%s", "bad": "%s", "hostile": "%s"}\n' "$GOOD" "$BAD" "$HOSTILE" > "$COPY/commits.json"
chmod 0644 "$COPY/commits.json"
status=0
sudo -u cardea-kernel env CARDEA_REPOSITORIES="$REGISTRY" \
  "$PY" -I "$COPY/env_try.py" run --work "$WORK" --commits "$COPY/commits.json" || status=$?
echo "evidence: $WORK/evidence.json (exit $status)"
exit "$status"
