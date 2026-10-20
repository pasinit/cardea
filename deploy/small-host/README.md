# Small-host deployment: Cardea for Telos routines

One small Debian 13 VM runs Cardea, SPIRE and the agent sessions. The Telos host (Ubuntu + Docker) reaches it over the private network. See [ADR-030](../../docs/decisions/030-small-host-profile.md) for the design and its limits.

**Status:** configuration is validated against the current models and the root helper's checks by `tests/service/test_deploy_examples.py`. Sections 1–3 were first run on a real Debian 13 host on 2026-10-20, and the fixes from that run are included: store permissions, SPIRE units, and certificates that strict TLS clients accept. Section 4 (confined workers) has **not yet been run on a real host**.

```
Telos host (Ubuntu, Docker)            Cardea host (Debian 13, 2 GB)
├── telos  ──HTTPS:8443 + JWT-SVID──►  ├── cardea-kernel (systemd)
├── cardea-identity (spiffe-helper)    ├── spire-server :8081 (private net only)
└── spire-agent ───────:8081────────►  └── spire-agent (unix attestor)
```

## 1. Hosts

- Create a Debian 13 VM with at least 2 GB of RAM, in the same region and VPC as the Telos host. Note both private IPs.
- Firewall the Cardea host so that ports 8081 (SPIRE) and 8443 (kernel API) accept only the Telos host's private IP. Expose nothing publicly except SSH.
- On a 2 GB host, add 2 GB of swap. Confined workers run with `MemorySwapMax=0`, so swap only absorbs the kernel service, SPIRE and the OS during peaks:

```sh
fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
```

## 2. SPIRE (Cardea host)

1. Install SPIRE v1.15.3: download the release tarball from github.com/spiffe/spire/releases, verify its checksum and unpack it so that the binaries are `/opt/spire/bin/spire-server` and `/opt/spire/bin/spire-agent`. Create a `spire` system user for the server.
2. Place `spire/server.conf` as `/etc/spire/server.conf` and `spire/agent-cardea.conf` as `/etc/spire/agent.conf`. Install the units `spire/spire-server.service` and `spire/spire-agent.service` in `/etc/systemd/system/`.
3. Start the server, export its bundle for the agents, and create one join token per node. The server's admin socket is `/run/spire-server/api.sock`:

```sh
systemctl enable --now spire-server
S="/opt/spire/bin/spire-server"; A="-socketPath /run/spire-server/api.sock"
$S bundle show $A > /etc/spire/bootstrap-bundle.crt
$S token generate $A -spiffeID spiffe://cardea.internal/node/cardea
$S token generate $A -spiffeID spiffe://cardea.internal/node/telos
```

4. Start the local agent with the first token. The token goes in an environment file that the unit reads, and is cleared once the agent has attested. Join tokens are single-use, and later starts re-attest with the SVID the agent stored:

```sh
install -m 0600 /dev/null /etc/spire/agent-join-token.env
echo 'SPIRE_AGENT_JOIN=-joinToken <cardea-node-token>' > /etc/spire/agent-join-token.env
systemctl enable --now spire-agent
/opt/spire/bin/spire-agent healthcheck -socketPath /run/spire/agent.sock
: > /etc/spire/agent-join-token.env
```

5. Register the two workloads:

```sh
$S entry create $A -parentID spiffe://cardea.internal/node/cardea \
  -spiffeID spiffe://cardea.internal/runtime/kernel -selector unix:user:cardea-kernel
$S entry create $A -parentID spiffe://cardea.internal/node/telos \
  -spiffeID spiffe://cardea.internal/service/telos \
  -selector docker:label:com.docker.compose.service:cardea-identity
```

## 3. Cardea (Cardea host)

1. Create a `cardea-kernel` system user. Clone the repository to `/opt/cardea` at a reviewed commit, then run `uv sync --locked --python /usr/bin/python3` in `apps/api`. A system Python keeps the venv usable inside confined workers, which see only `/usr` and their read-only binds.
2. Create the store directory. It must be owned by `cardea-kernel` with mode `0700`; otherwise `init` fails with `unsafe_directory`:

```sh
install -d -o cardea-kernel -g cardea-kernel -m 0700 /var/lib/cardea
```
3. Install the Telos operations package into the same environment:

```sh
uv pip install --python /opt/cardea/apps/api/.venv/bin/python \
  "git+https://github.com/pasinit/telos@<commit>#subdirectory=packages/telos-cardea"
```

4. Copy its descriptors to `/etc/cardea/operations/`. Each file must be owned by `cardea-kernel` with mode `0600`. Configure the operations' credentials (SMTP/Brevo and so on) as that package's README describes.
5. Credentials, each in a `0600` file owned by `cardea-kernel`:
   - `/etc/cardea/claude-oauth-token`: a Claude subscription OAuth token, or switch `agent_session` in `config.json` to `api_key_file`.
   - TLS: `cardea/make-tls.sh <cardea-private-ip> /etc/cardea/tls`. Keep `ca.key` offline and copy `ca.crt` to the Telos host as `cardea-ca.crt`.
6. Edit `cardea/config.json` (replace `CARDEA_PRIVATE_IP`) and save it as `/etc/cardea/config.json` (`0600`, `cardea-kernel`).
7. Initialize, provision and start:

```sh
sudo -u cardea-kernel env PYTHONPATH=/opt/cardea/apps/api/src \
  /opt/cardea/apps/api/.venv/bin/python -m cardea.service init --config /etc/cardea/config.json
sudo -u cardea-kernel env PYTHONPATH=/opt/cardea/apps/api/src \
  /opt/cardea/apps/api/.venv/bin/python -m cardea.service provision \
  --config /etc/cardea/config.json /etc/cardea/provision.telos.json
cp cardea/cardea-kernel.service /etc/systemd/system/ && systemctl enable --now cardea-kernel
```

Edit `provision.telos.json` first: one `telos-<user>` namespace per Telos user, one `telos-<agent>` principal per agent that has routines, and real expiry dates.

## 4. Confined workers (Cardea host)

Without this section, sessions and installed operations run inside the kernel's own service cgroup with no egress limit. ADR-028 confines each one in a systemd transient unit plus bubblewrap. Debian enforcement is still to be verified (ADR-028).

1. Packages and user: `apt install bubblewrap squid`, then create a locked `cardea-worker` system user. The helper refuses a setuid `bwrap` and checks the exact systemd and bubblewrap package versions.
2. The root helper, **from the same commit as `/opt/cardea`**. The kernel compares the helper's SHA-256 with its own copy and refuses to start on a mismatch:

```sh
install -o root -g root -m 0755 /opt/cardea/apps/api/src/cardea/hosts/launcher.py /usr/libexec/cardea-host
echo "{\"kernel_uid\": $(id -u cardea-kernel)}" > /etc/cardea-host.json && chmod 0644 /etc/cardea-host.json
echo 'cardea-kernel ALL=(root) NOPASSWD: /usr/libexec/cardea-host ""' > /etc/sudoers.d/cardea-host
chmod 0440 /etc/sudoers.d/cardea-host && visudo -cf /etc/sudoers.d/cardea-host
```

3. Profiles and workers: edit `cardea/cardea-host-workers.json` and install it as `/etc/cardea-host-workers.json` (owned by root, mode `0644`).
   - The `session` profile runs the agent session. It may reach only the model API through its proxy and the loopback tool endpoint. Its credential is a root-only file such as `/etc/cardea/credentials/claude` (`0600 root`), which systemd hands to the unit.
   - The `browser` profile runs browser operations. Set its `hosts` to the sites they need.
   - `concurrency` bounds how many workers of a profile run at once. JIT profiles default to 1.
   - Worker paths depend on the operations package. A browser needs its browser build bound read-only under `/opt/`.
4. The egress proxy for networked profiles: install `cardea/squid-workers.conf` as `/etc/squid/conf.d/cardea-workers.conf` and restart squid. Keep its host lists identical to the profiles' `hosts`. The proxy is what enforces them, and units can reach only the proxy's address.
5. Let the kernel use the helper: install the drop-in `cardea/cardea-kernel.service.d/host-execution.conf` as `/etc/systemd/system/cardea-kernel.service.d/host-execution.conf`. It drops `NoNewPrivileges` (sudo needs it) and makes `/var/lib/cardea-host` writable inside the unit's read-only file system.
6. Switch `/etc/cardea/config.json` to `cardea/config.confined.json`: `host_execution: true` and a confined `agent_session` that holds no credential itself. Then run `systemctl daemon-reload && systemctl restart cardea-kernel`.

Check: `sudo -u cardea-kernel sudo -n /usr/libexec/cardea-host <<< '{"action":"probe"}'` prints the profile and source hash. An error means the kernel version, package versions or helper bytes do not match.

## 5. Telos host

1. Run a SPIRE agent with `spire/agent-telos.conf` (replace `CARDEA_PRIVATE_IP`), using the second join token. Its socket must be at `/run/spire/agent.sock`.
2. Copy `telos/spiffe-helper.conf` and `cardea-ca.crt` next to the Telos compose file, then add the overlay:

```sh
docker compose -f docker-compose.yml -f docker-compose.cardea.yml up -d
```

3. Telos starts in **shadow** mode:
   - routines are mirrored to Cardea with `mail.preview` in place of email;
   - Telos still runs them itself;
   - each run's evidence is at `GET /v0.2/tasks/{id}/run`.

   Once shadow runs match, set `TELOS_CARDEA_MODE=live`. Cardea then sends the emails and Telos stops running mirrored routines.

## Checks after install

- `systemctl status cardea-kernel spire-server spire-agent`, and `free -m` while a routine runs.
- With confined workers: `systemctl list-units 'cardea-host-*'` shows one unit per running worker, and none after a run.
- From the Telos container: a request to `https://CARDEA_PRIVATE_IP:8443/v0.2/system` using the token file gets 200; without the token it gets 401.
- `GET /v0.2/triggers` lists `telos.<routine-id>` triggers within a minute of Telos starting.
