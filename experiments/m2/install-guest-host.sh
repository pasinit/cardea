#!/bin/sh
# Run only as root in the explicitly marked disposable M2 guest.
set -eu
test "$(cat /etc/cardea-disposable-m2-vm)" = yes
install -d -o root -g root -m 0755 /usr/libexec
install -o root -g root -m 0755 /opt/cardea-src/apps/api/src/cardea/hosts/launcher.py /usr/libexec/cardea-host
python3 - <<'PY'
import json,pwd
from pathlib import Path
Path('/etc/cardea-host.json').write_text(json.dumps({'kernel_uid':pwd.getpwnam('cardea-kernel').pw_uid})+'\n')
PY
chown root:root /etc/cardea-host.json
chmod 0644 /etc/cardea-host.json
printf '%s\n' 'cardea-kernel ALL=(root) NOPASSWD: /usr/libexec/cardea-host ""' > /etc/sudoers.d/cardea-host
chmod 0440 /etc/sudoers.d/cardea-host
visudo -cf /etc/sudoers.d/cardea-host
