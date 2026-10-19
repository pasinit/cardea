# Small-host deployment: Cardea for Telos routines

One small Debian 13 VM runs Cardea, SPIRE and the agent sessions. The Telos host (Ubuntu + Docker) reaches it over the private network. See [ADR-030](../../docs/decisions/030-small-host-profile.md) for the design and its limits.

**Status:** configuration is validated against the current models by `tests/service/test_deploy_examples.py`. The procedure has **not yet been run on a real host**.

```
Telos host (Ubuntu, Docker)            Cardea host (Debian 13, 2 GB)
├── telos  ──HTTPS:8443 + JWT-SVID──►  ├── cardea-kernel (systemd)
├── cardea-identity (spiffe-helper)    ├── spire-server :8081 (private net only)
└── spire-agent ───────:8081────────►  └── spire-agent (unix attestor)
```

## 1. Hosts

- Create a Debian 13 VM with at least 2 GB of RAM, in the same region and VPC as the Telos host. Note both private IPs.
- Firewall the Cardea host so that ports 8081 (SPIRE) and 8443 (kernel API) accept only the Telos host's private IP. Expose nothing publicly except SSH.

## 2. SPIRE (Cardea host)

1. Install SPIRE v1.15.3: download the release tarball from github.com/spiffe/spire/releases and verify its checksum.
2. Place `spire/server.conf` and `spire/agent-cardea.conf` under `/etc/spire/`.
3. Start the server, export its bundle for the agents, and create one join token per node:

```sh
spire-server bundle show > /etc/spire/bootstrap-bundle.crt
spire-server token generate -spiffeID spiffe://cardea.internal/node/cardea
spire-server token generate -spiffeID spiffe://cardea.internal/node/telos
```

4. Start the local agent with the first token (`spire-agent run -joinToken …`), then register the two workloads:

```sh
spire-server entry create -parentID spiffe://cardea.internal/node/cardea \
  -spiffeID spiffe://cardea.internal/runtime/kernel -selector unix:user:cardea-kernel
spire-server entry create -parentID spiffe://cardea.internal/node/telos \
  -spiffeID spiffe://cardea.internal/service/telos \
  -selector docker:label:com.docker.compose.service:cardea-identity
```

## 3. Cardea (Cardea host)

1. Create a `cardea-kernel` system user. Clone the repository to `/opt/cardea` at a reviewed commit, then run `uv sync --locked` in `apps/api`.
2. Install the Telos operations package into the same environment:

```sh
uv pip install --python /opt/cardea/apps/api/.venv/bin/python \
  "git+https://github.com/pasinit/telos@<commit>#subdirectory=packages/telos-cardea"
```

3. Copy its descriptors to `/etc/cardea/operations/`. Each file must be owned by `cardea-kernel` with mode `0600`. Configure the operations' credentials (SMTP/Brevo and so on) as that package's README describes.
4. Credentials, each in a `0600` file owned by `cardea-kernel`:
   - `/etc/cardea/claude-oauth-token`: a Claude subscription OAuth token, or switch `agent_session` in `config.json` to `api_key_file`.
   - TLS: `cardea/make-tls.sh <cardea-private-ip> /etc/cardea/tls`. Keep `ca.key` offline and copy `ca.crt` to the Telos host as `cardea-ca.crt`.
5. Edit `cardea/config.json` (replace `CARDEA_PRIVATE_IP`) and save it as `/etc/cardea/config.json` (`0600`, `cardea-kernel`).
6. Initialize, provision and start:

```sh
sudo -u cardea-kernel env PYTHONPATH=/opt/cardea/apps/api/src \
  /opt/cardea/apps/api/.venv/bin/python -m cardea.service init --config /etc/cardea/config.json
sudo -u cardea-kernel env PYTHONPATH=/opt/cardea/apps/api/src \
  /opt/cardea/apps/api/.venv/bin/python -m cardea.service provision \
  --config /etc/cardea/config.json /etc/cardea/provision.telos.json
cp cardea/cardea-kernel.service /etc/systemd/system/ && systemctl enable --now cardea-kernel
```

Edit `provision.telos.json` first: one `telos-<user>` namespace per Telos user, one `telos-<agent>` principal per agent that has routines, and real expiry dates.

## 4. Telos host

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
- From the Telos container: a request to `https://CARDEA_PRIVATE_IP:8443/v0.2/system` using the token file gets 200; without the token it gets 401.
- `GET /v0.2/triggers` lists `telos.<routine-id>` triggers within a minute of Telos starting.
