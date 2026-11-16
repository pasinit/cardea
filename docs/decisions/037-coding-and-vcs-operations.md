# ADR-037: Coding and Version-Control Operations for an Agent Team

- **Date / owner / milestones:** 2026-11-16; runtime, host-execution and API maintainers. Builds on [ADR-023](023-installed-operation-registry.md) (installed operations), [ADR-026](026-effect-idempotency-and-reconciliation.md) (effect scopes and reconciliation), [ADR-028](028-configurable-worker-profiles.md) (confined workers), [ADR-033](033-pluggable-agent-harnesses.md) (harnesses), [ADR-034](034-agent-groups-and-shared-records.md) (agent groups; its software-team example names `vcs.open_change`) and [ADR-036](036-approval-waits.md) (approval waits).
- **Status:** proposed. Nothing here is selected, implemented or verified. No fit test below has run; every "fit" is from documentation and says so. The owner's open questions are at the end.
- **Affected components:** [operation broker](../components/operation-broker.md), [installed host operations](../components/installed-host-operations.md), [Debian host](../components/debian-host.md), [identity and capabilities](../components/identity-capabilities.md), [agent runtime](../components/agent-runtime.md).

## Requirement

An agent team (ADR-034) that works on software needs four things the kernel does not provide yet. All of them are installed operations: an operations package that the deployment owns, not new kernel vocabulary.

1. **A coding session as one confined call.** `code.session` checks out a branch of an allowed repository into a scratch workspace, runs a headless coding agent on a task, runs the repository's own gates (`make lint`, `make typecheck`, `make test`, or the target names the host configures), and returns the resulting change and the gate results.
   - It is one long operation call. It keeps no process, workspace or session between calls (no cross-call process state); a later call starts from a branch or a stored change, never from a live process.
   - It has no credential for the repository host and cannot push. Its network is limited to the model API and a read-only source of the repository.
   - Its output is bounded and receipted like any operation; the full change is stored as a kernel object the following calls can cite.
2. **Repository-host effects with receipts.** Operations to push a branch, open a pull request, comment, and submit a review.
   - A pushed branch name is restricted to a configured prefix (default `agent/*`).
   - Each call is idempotent (an ADR-026 scope) and, after an interruption, is reconciled against the repository host's state, never blindly retried.
   - There is no merge operation. Merging stays a human or host decision.
3. **Credentials the agent never sees.** The kernel mints a short-lived, repository-scoped token for the call and injects it only into the worker of these operations. It never appears in arguments, outputs, receipts, prompts, logs or the coding session's environment. Repository-side branch rules are a second, independent guard.
4. **A privacy gate.** Content leaving the host is checked first: a configurable content check (secret patterns plus a denylist the host owner maintains) runs in the worker on the change before any push, and on pull-request titles and bodies, comments, review bodies and commit messages before they are sent. A match refuses the call; nothing is sent.

**Trust boundary.**
- The coding agent, the repository's content (including its own hooks, scripts and agent configuration) and the model's output are untrusted. They can change files in the scratch workspace and nothing else.
- The repository-host credential, the content gate's configuration and the allowed-repository list are host-owner configuration, outside worker reach.
- Authority is the ordinary ceiling: each operation is granted like any other (ADR-010), per repository where the grant is resource-qualified, and every effect runs under the caller's live ceiling and, where a rule asks, an ADR-036 approval.

**Recovery.**
- A crash during `code.session` leaves no effect outside the scratch workspace, which cleanup removes; the call is retried as a fresh session.
- A crash during a host effect leaves it `outcome_unknown` until a reconciler reads the host's state (the branch's head, the pull request for that head, the comment or review carrying the call's effect key).

**Acceptance tests (to write; none run).**
1. A `code.session` on a fixture repository with a fake model produces a change and gate results; its worker has no host credential and no route to the repository host's write API.
2. A coding agent that tries to read the token, write outside the workspace, or reach the network is refused by confinement; the call still settles.
3. `vcs.push_branch` refuses a branch outside the prefix before any network call; with the prefix, a retried push after a crash is reconciled (head matches) and not repeated.
4. `vcs.open_change` twice in one scope opens one pull request; after a crash between the request and the receipt, reconciliation finds the pull request by head and settles the call.
5. A pull-request body, comment or commit message containing a denylisted term or a secret pattern is refused with nothing sent; a change containing one is refused before push.
6. The token is absent from every receipt, output, prompt, attempt log and the coding session's environment; it expires within an hour.
7. A repository-side rule that forbids pushes outside the prefix refuses a push even if the kernel's check were bypassed.

## Candidates and primary evidence

Checked 2026-11-16 against vendor documentation and release pages. Versions are the pinned dependency where Cardea already uses the project, otherwise the newest release seen that day. **No fit test has run.**

### Coding session: headless coding agents

| Candidate | Version, licence | Fit (from documentation) | Gaps |
|---|---|---|---|
| Claude Code headless (`claude -p`, the Agent SDK CLI; [docs](https://code.claude.com/docs/en/headless)) | Agent SDK pinned `0.2.163` (ADR-033 harness `claude-agent-sdk`); proprietary terms of service | Non-interactive by design. `--bare` skips the host's and the repository's hooks, plugins, MCP servers and settings, which matters because repository content is untrusted. `--allowedTools`, `--permission-mode dontAsk` and `--permission-prompts none` make unattended runs deny anything not allowed. `--output-format json` reports the result, the session id and a cost estimate; `--max-budget-usd` bounds cost. SIGTERM stops the process tree of a running command. | Proprietary. Without `--bare`, a `-p` run executes the repository's `.claude` hooks and `.mcp.json` servers with no trust prompt, so `--bare` (or an equivalent locked configuration) is mandatory. Its Bash tool runs arbitrary commands, so the containment must be the worker, not the tool list. |
| OpenCode `opencode run` ([CLI docs](https://opencode.ai/docs/cli/)) | Pinned `1.18.35` by checksum (`packaging/fetch_opencode.py`, ADR-033); MIT | "Designed for scripts, CI jobs, and other workflows." Any OpenAI-compatible provider, which keeps the model choice a harness choice as ADR-033 requires. | Its non-interactive flags (model, output format, permissions) are not on that page; `opencode run --help` on the pinned binary has to be read in the fit test. How it reads repository-local configuration is not documented there. |
| Aider `--message` ([scripting docs](https://aider.chat/docs/scripting.html)) | Apache-2.0 | One instruction, applies edits and exits; `--yes`, `--no-auto-commits`, `--dry-run`. Edits files rather than running arbitrary tools by default. | Not yet a Cardea harness, so it adds a third runtime to pin. Its Python API is "not officially supported". |
| OpenHands headless (`openhands --headless -t`, [docs](https://docs.openhands.dev/openhands/usage/cli/headless)) | MIT (core; its `enterprise/` directory is separately licensed) | Designed for CI; JSONL event stream with `--json`. | Headless mode "always runs in always-approve mode": no per-tool gate, so containment again rests entirely on the worker. Usually runs its own sandbox container, which overlaps ADR-028 and needs a container runtime the small-host profile (ADR-030) does not assume. |
| Status quo: an ADR-027 agent run with fine-grained file and shell operations | in tree | Every step receipted. | One receipt per file edit or command gives no useful idempotency and makes a coding loop of hundreds of steps pay the invocation core's cost each time. ADR-033 deliberately turns harness shell and edit tools off; reproducing them as operations would be a bespoke shell. Rejected for the inner loop; kept for the effects that leave the host. |

**Selected for the proposal:** run an ADR-033 harness binary (Claude Code in `--bare` non-interactive mode, or OpenCode) *inside an ADR-028 confined worker* as the implementation of one installed operation. The worker is the containment: scratch directory only, no host credential, network limited to the model API and a read-only repository source. The harness's own tool list is a second, weaker layer.

### Version control and the repository host

| Need | Candidate | Fit (from documentation) | Gaps |
|---|---|---|---|
| Checkout, commit, diff, bundle | [git](https://git-scm.com/) (GPL-2.0; Debian's packaged version, 2.56.0 the newest release seen) | `git worktree`/`clone --depth`, `git diff`, and `git bundle` to carry a change between calls as one file, verifiable with `git bundle verify`. Hooks are skipped with `core.hooksPath=/dev/null` for kernel-run commands. | Repository hooks and `.gitattributes` filters are repository content and must not run with kernel authority; the commands run inside the worker. |
| Push, pull requests, comments, reviews | GitHub REST API: [pulls](https://docs.github.com/en/rest/pulls/pulls), [reviews](https://docs.github.com/en/rest/pulls/reviews), issue comments, git refs | `POST /repos/{owner}/{repo}/pulls` needs `head` and `base`; `GET .../pulls?head=owner:branch&state=all` finds an existing pull request for a head, which is the reconciliation read. Reviews take `event` `APPROVE`, `REQUEST_CHANGES` or `COMMENT`. | No idempotency key on create calls: the documented response to a rejected create is a bare 422, so idempotency must come from ADR-026 scopes and a read before retry. Comments need a marker (the effect key) to be found again. |
| Credentials | [GitHub App installation access tokens](https://docs.github.com/en/rest/apps/apps#create-an-installation-access-token-for-an-app) | `POST /app/installations/{id}/access_tokens` takes `repositories` or `repository_ids` and a `permissions` object (for example `contents: write`, `pull_requests: write`). Tokens "expire one hour from the time you create them" and cannot exceed the installation's repositories or the app's permissions. Authenticating needs a JWT signed with the app's private key. | The private key is a long-lived secret on the host; it must stay with the kernel's credential minting, never in a worker. A new token format (`ghs_APPID_JWT`) is rolling out, so no code may assume a fixed token length. |
| A second guard on the repository | [Rulesets](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/about-rulesets) and branch protection | Rulesets target branches by `fnmatch` pattern, can restrict who pushes and block force pushes, and combine with branch protection, "the most restrictive version of each rule" applying. Bypass lists name roles, teams or apps. | Configured on the repository host, outside Cardea; Cardea can only check and report it, not enforce it. The page does not list every rule type; the fit test must read the rules API. |
| Client library | Direct HTTP with the pinned `httpx` (0.28.1), the GitHub CLI, or a generated client such as `githubkit` | `httpx` is already a dependency; four endpoints do not need a full SDK. | `gh` would put a credential-using binary in the worker; a library pin needs the usual age and licence check. Not evaluated. |
| Other forges | Gitea, GitLab REST APIs | Same shapes (refs, merge or pull requests, notes). | Out of scope for the first package; the operations are named `vcs.*` so a second package can implement them. |

### Content gate

| Candidate | Version, licence | Fit (from documentation) | Gaps |
|---|---|---|---|
| [Gitleaks](https://github.com/gitleaks/gitleaks) | v8.30.1 (2026-03-21); MIT | Scans a diff, a directory or stdin for secret patterns; custom rules and allowlists in TOML, so a host-owner denylist can be expressed as rules. | Pattern-based: misses secrets without a known shape and personal data that is not in the denylist. Not run. |
| `detect-secrets`, TruffleHog | Apache-2.0; AGPL-3.0 | Entropy and verified-credential detection. | TruffleHog's licence and its network verification calls need review; not evaluated further. |
| Host push protection (repository-host secret scanning) | hosted | A third layer at the host. | Runs after the content has left the host, so it cannot be the gate. |
| [pre-commit](https://pre-commit.com/) hooks in the repository | MIT | Common in repositories. | Repository-owned and skippable; a gate must be kernel-owned. |

**Selected for the proposal:** a kernel-owned gate that runs Gitleaks with the host owner's rules plus a plain denylist file (case-insensitive terms), on the change before push and on every text field before it is sent. The repository's own hooks are never trusted as the gate.
