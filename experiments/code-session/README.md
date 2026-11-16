# code.session Debian fit test (ADR-037)

This test runs one real `code.session`, confined by the root helper, on a fixture repository with the real Claude Code harness, and records evidence. **It has not been run yet.** An operator runs it on a Debian 13 dev host. It spends model usage (at most `max_budget_usd`, 1 USD in the fit configuration). See [ADR-037](../../docs/decisions/037-coding-and-vcs-operations.md).

The portable tests cover the same paths with a fake `claude`: `apps/api/tests/service/test_code_session_claude.py`. This test checks what they cannot:

- the real CLI's flags and output;
- systemd and bubblewrap confinement;
- the egress proxy;
- the size of a real change bundle.

## What it needs

- A Debian 13 host with the small-host kit installed through section 4 (confined workers) of [deploy/small-host/README.md](../../deploy/small-host/README.md), including step 8:
  - the `coding` profile and the `code-session` worker in `/etc/cardea-host-workers.json`;
  - the coding proxy port `127.0.10.5` in squid;
  - `packages/cardea-coding-ops` installed into `/opt/cardea/apps/api/.venv`.
- **A subscription token file**, given as the first argument, holding a token from `claude setup-token`. The script installs it as `/etc/cardea/credentials/claude-code` (root, `0600`), the profile's credential. No other copy is made, and the evidence never contains it.
- **The package cache** at `/opt/cardea-code/cache` (for example `uv/`, `wheels/`, `npm/`). It is bound read-only, and the gates use it offline. The fixture's gates need only the system Python, but the script refuses to start without the cache, so the binding is exercised.
- `git` and `curl`, and the `cardea-kernel` user with its sudo rule for the helper.

## Run

From a checkout of the same commit as `/opt/cardea`:

```sh
sudo sh experiments/code-session/debian.sh /path/to/token-file /var/tmp/code-session-fit
```

The script:

1. installs the credential;
2. writes the fixture as a bare repository at `/opt/cardea-code/repos/fixture.git`;
3. installs the fit test's configuration as `/opt/cardea-code/coding.json`, after backing up any existing one;
4. records which hosts the coding proxy passes;
5. runs `fit.py run` as `cardea-kernel`, which starts the session through the helper exactly as the kernel's confined executor does;
6. restores the previous configuration.

## Evidence

The evidence goes to `OUT_DIR/evidence.json` and `OUT_DIR/proxy.txt`. The run passes when every check in `checks` is true:

| Check | Meaning |
| --- | --- |
| `profile_model_api_only` | the installed `coding` profile allows the model API only |
| `token_file_present`, `cache_present` | the inputs above exist |
| `worker_succeeded` | the systemd unit finished with `success` |
| `harness_ok`, `harness_reported_turns` | Claude Code exited 0 and its JSON result was parsed (turns, stop reason, cost) |
| `gates_passed` | the fixture's `unittest` gate, and the `no-network` gate, which passes only if `pypi.org` is unreachable from a gate |
| `changed_the_task_files` | the change bundle verifies against a fresh clone and changes `calc.py` and `test_calc.py` |
| `repository_config_not_honoured` | the fixture's `.claude/settings.json` hooks and `.mcp.json` server, which would each leave a marker file in the checkout, did not run |
| `token_unreadable_by_kernel_user` | the kernel user cannot read the credential file |

The evidence also records the following, which are observations rather than checks:

- the unit's CPU time, peak memory and wall time;
- the harness record;
- the bundle size;
- `claude_md_followed`, whether the model obeyed the fixture's `CLAUDE.md`. Repository text is untrusted input either way.

`proxy.txt` should show `200` for `api.anthropic.com` and `403` for the other hosts.
