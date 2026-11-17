# `code.session` without registries: the strict cache option

The kit's default lets `code.session`'s gates install dependencies from PyPI (ADR-037 question 8). The `coding` profile then lists `registries`, and its proxy port allows them over TLS.

The proxy allows a profile's registries per port, not per process. Every process in the unit can reach them: the coding agent (Claude Code runs with the proxy variables) and any code in the checkout, which can name the proxy address itself. `"dependencies": "cache"` only withholds the proxy variables from the gates. It does not take the network away. Network isolation comes from the profile.

The strict option is therefore these three files together:

| File | Replaces |
|---|---|
| `coding.json` | `/opt/cardea-code/coding.json`: `"dependencies": "cache"`, and `gate_env` pointing at the read-only cache in `/opt/cardea-code/cache`. |
| `coding-network.json` | the `coding` profile's `network` in `/etc/cardea-host-workers.json`: no `registries`. |
| `squid-coding.conf` | the `coding_hosts` and `coding_registries` lines of `/etc/squid/squid.conf`: the model API only. |

Install all three, then run `squid -k parse` and restart squid and the kernel. A mismatch is refused. `"dependencies": "cache"` on a profile that still lists registries, or `"dependencies": "registries"` on one without them, fails each call with `dependencies_mismatch` before anything runs. The helper tells the worker which registries its profile allows (`CARDEA_REGISTRIES`).

Checks:
- `curl -s -o /dev/null -w '%{http_connect}\n' -x http://127.0.10.5:3128 https://pypi.org/` prints 403.
- `experiments/debian-acceptance/registry_egress.py --strict` passes: the profile lists no registry, and `pypi.org` is refused.
