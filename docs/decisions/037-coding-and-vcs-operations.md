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

## Decision (proposed)

Compose existing pieces into one operations package, `vcs.*` and `code.*`, installed like any ADR-023 package. The custom parts are the operations' handlers, the credential minting and the content gate's wiring.

### 1. Operations

| Operation | Effect | Idempotency scope (ADR-026) | Reconciler (after `outcome_unknown`) |
|---|---|---|---|
| `code.session {repository, base_ref, task, task_key, harness?, model?, gates?}` | effect (publishes kernel objects; nothing leaves the host) | `/repository`, `/base_ref`, `/task_key` | the published change object exists for the call's effect key → applied; else not applied, so a retry runs a fresh session |
| `vcs.read_change {repository, number}` | read | none | none |
| `vcs.push_branch {repository, branch, change, expected_head?}` | effect | `/repository`, `/branch`, `/change` | the branch's head on the host equals the change's head commit → applied |
| `vcs.open_change {repository, head, base, title, body, draft?}` | effect | `/repository`, `/head` | `GET .../pulls?head=<owner>:<head>&state=all` returns the pull request → applied, with its number and URL |
| `vcs.comment {repository, number, body, client_key}` | effect | `/repository`, `/number`, `/client_key` | a comment on the pull request carries the marker `cardea:<effect_key>` → applied |
| `vcs.review {repository, number, event, body, client_key}` | effect | `/repository`, `/number`, `/client_key` | a review by the app carries the marker → applied |

- There is no `vcs.merge`. `vcs.review` accepts `COMMENT` and `REQUEST_CHANGES`; `APPROVE` is refused unless the host owner enables it (open question 2).
- `code.session` returns, within the 64 KiB receipt: the base and head commits, file and line counts, a bounded diff excerpt, each gate's target, exit status, duration and output tail, and the agent's bounded summary, turns and cost. The whole change is a `git bundle` published as a task-owned kernel object (the ADR-014/015 publication path), which `vcs.push_branch` cites by exact version. Nothing else carries state between calls.
- ADR-034 records cite these receipts: a `submit` transition can require a `vcs.open_change` receipt for the record (`requires_receipt`), as the ADR's worked example already does.

### 2. Execution

- **`code.session`** runs as an ADR-028 confined worker under a new host-owner profile (name it `coding`):
  - a scratch workspace and no other writable path;
  - network to the model API and to a read-only repository source only: either a host-side mirror served over the loopback, or the host's read endpoint with no credential for public repositories (open question 8 covers package registries for `make test`);
  - no repository-host credential at all.

  Inside it, the worker clones `base_ref` with hooks disabled for its own git commands, runs the selected harness non-interactively (for Claude Code: `--bare`, an explicit tool list, `--permission-prompts none` and a budget; for OpenCode: the pinned binary with its permissions set to the same effect), commits the result, runs the gate targets with their own timeouts, and publishes the bundle. The harness and model are an ADR-033 selection, granted like an agent run's (`harness:<id>`).
- **`vcs.*` effects** run as confined workers under a second profile (`vcs`) whose network allow-list is the repository host's API and git endpoints. They never check out the change: `vcs.push_branch` fetches the bundle into an empty bare repository and pushes one ref, with hooks disabled.
- **Timeouts.** ADR-023 caps an operation at 600 s, which a coding session plus a full test run can exceed. This needs a small ADR-023 amendment: a confined operation may declare up to the profile's wall-time limit (open question 9).

### 3. Credentials

- The host owner installs a GitHub App on the allowed repositories and gives the kernel its private key as a private file (like other credentials, ADR-010/028), plus the installation and repository ids.
- For each `vcs.*` effect, the kernel mints an installation access token restricted to the single repository (`repository_ids`) and to the call's permissions (`contents: write` for a push, `pull_requests: write` or `issues: write` for the others), valid for at most an hour.
- The token goes to the worker on its private input channel, outside the recorded arguments, and is used through a credential helper (`GIT_ASKPASS`) or an HTTP header. Receipts, outputs, attempt logs and error summaries never contain it (ADR-026's failure-reason rules already keep exception text out).
- The coding session never receives a token.
- **Second guard on the host:** a ruleset restricts the app to creating and updating branches matching the prefix, blocks force pushes and deletions elsewhere, and keeps the default branch behind required reviews that the app cannot bypass. The kernel checks at install time that the ruleset exists and reports drift as a failed readiness check; it cannot enforce it.

### 4. Content gate

- A kernel-owned configuration lists the secret rules (a Gitleaks configuration) and a denylist file (one term per line, case-insensitive), both host-owner files outside worker reach.
- **Before push:** the `vcs.push_branch` worker scans the added lines of the change with Gitleaks and the denylist. A match fails the call with `content_refused` naming the rule and file, never the matched text.
- **Before send:** titles, bodies, comments, review bodies and commit messages are checked by the same gate inside the call that sends them.
- The gate runs before any network call, so a refusal sends nothing and is a definite failure, not `outcome_unknown`.

### 5. Authority

- Each operation is a grant (ADR-023). Repositories are resource-qualified like ADR-033's `harness:<id>`: a call names a repository alias from host configuration, and needs `repo:<alias>` in its grants and in the supervisor's live ceiling (open question 4).
- Operations that leave the host can carry ADR-036 rules, for example "always" for `vcs.open_change` on a protected repository.
- Branch names come from the operation, not the agent: `agent/<group>/<record or task>` by default, inside the configured prefix (open question 3).

## Integration

- **State.** No new kernel tables are expected: receipts, effect scopes, objects and the existing confined-worker bookkeeping hold everything. The package's configuration (repositories, App ids, gate files, profiles) is host-owner configuration.
- **Compatibility.** Additive: a new package and two worker profiles. ADR-033's rule that agent-run harnesses have no built-in shell or edit tools is unchanged: the harness inside `code.session` is not an agent run's harness but an operation's implementation, confined by ADR-028, and its result is one receipt.
- **Operational cost.** A coding session holds a confined-worker slot and model budget for minutes; profiles bound concurrency. Each effect mints one token (one host API call).
- **Dependencies.** None new for the kernel. The package pins a Gitleaks binary by checksum (as `fetch_opencode.py` pins OpenCode) and uses the already-pinned `httpx` and harnesses.

## Implementation plan (PR-sized slices, after selection)

1. Fit tests, recorded as evidence: Claude Code `--bare` and OpenCode `run` inside an ADR-028 profile on a fixture repository (tool list, repository hooks ignored, no network beyond the allow-list); Gitleaks on fixture diffs; the GitHub calls against a test repository with an App, including the 422 on duplicate pull requests and the reconciliation reads.
2. ADR-023 amendment for long confined operations.
3. `code.session` with a fake harness and fake gates; change bundles as objects.
4. Credential minting and injection; `vcs.push_branch` and `vcs.open_change` with reconcilers, against a fake host.
5. `vcs.comment`, `vcs.review`, `vcs.read_change`.
6. Content gate in both places.
7. Debian acceptance with a real App on a test repository.

## Verification

None. This record is a proposal.

## Limits

- Confinement, not the coding agent's tool list, is what stops it; on hosts without the ADR-028 helper, `code.session` is refused.
- The content gate finds known secret shapes and listed terms. It cannot find every piece of personal data.
- Repository-host rules are configured outside Cardea; Cardea can check them but not enforce them.
- A coding session's result depends on the model; its gates are evidence of the change's state, not of its correctness.

## Revisit

Revisit if:
- the repository host adds idempotency keys for pull requests, comments or reviews, so reconciliation reads become unnecessary;
- a headless coding agent ships a sandbox that meets ADR-028's requirements by itself;
- a second forge is needed (the `vcs.*` names stay; a second package implements them);
- tokens with narrower scopes than an installation token (for example per-branch) become available.

## Open questions for the owner

1. Which harness should `code.session` run first: Claude Code in `--bare` mode, or OpenCode with a configured provider? Both are already pinned for agent runs.
2. May an agent submit an `APPROVE` review? The proposal refuses it by default, because an app's approval could satisfy the repository's review rule and amount to an unattended merge path.
3. Branch naming: a fixed prefix (`agent/*`), or one per group (`agent/<group>/*`) so rulesets can separate teams?
4. Repository authority: resource-qualified grants `repo:<alias>` with aliases in host configuration, or one installed package per repository?
5. Content gate scope: added lines only, or the whole tree of the pushed branch? And should a refusal notify the supervisor by mail (ADR-034 decision 4) as well as failing the call?
6. Change transport: what size cap and retention for change bundles stored as objects?
7. Credentials for small deployments: is a GitHub App required, or is a fine-grained personal access token an accepted fallback (no per-call minting, longer-lived secret)?
8. Network for gates: `make test` often installs packages. Allow-list the package registries in the `coding` profile, or require pre-provisioned caches and no network beyond the model API?
9. Long calls: raise ADR-023's 600 s cap for confined operations to the profile's wall time, or split a session into code and gate calls?
