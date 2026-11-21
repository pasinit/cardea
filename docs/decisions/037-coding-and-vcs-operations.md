# ADR-037: Coding and Version-Control Operations for an Agent Team

- **Date / owner / milestones:** 2026-11-16; runtime, host-execution and API maintainers. Builds on [ADR-023](023-installed-operation-registry.md) (installed operations), [ADR-026](026-effect-idempotency-and-reconciliation.md) (effect scopes and reconciliation), [ADR-028](028-configurable-worker-profiles.md) (confined workers), [ADR-033](033-pluggable-agent-harnesses.md) (harnesses), [ADR-034](034-agent-groups-and-shared-records.md) (agent groups; its software-team example names `vcs.open_change`) and [ADR-036](036-approval-waits.md) (approval waits).
- **Status:** proposed, except the parts below, which are selected and implemented; nothing is verified on Debian or against a real repository host. The owner decided questions 2–8 and 10 on 2026-11-16 (see [Decided by the owner](#decided-by-the-owner-2026-11-16)). Questions 1 and 9 are the lead's decisions (see [Decisions](#decisions-lead-2026-11-16)); question 10's answer amends question 1. On 2026-11-17 the owner decided that the developer who opened a pull request merges it once the principal engineer approved it. This supersedes "there is no merge operation" and "the owner merges"; see the [amendment](#amendment-2026-11-17-the-developer-merges).
  - **Implemented:**
    - the content gate (decision 4, plan slice 6), with redaction of refused content and the confined-worker callback (see [Content gate: implementation](#content-gate-implementation-2026-11-16) and [redaction and confined workers](#content-gate-redaction-and-confined-workers-2026-11-16));
    - plan slice 2, the [ADR-023 amendment](023-installed-operation-registry.md#amendment-2026-11-16-long-confined-operations);
    - plan slice 3, `code.session`, with the Claude Code harness entry, the `coding` profile in the small-host kit and the Debian fit-test script;
    - plan slices 4 and 5, the repository-host operations `vcs.push_branch`, `vcs.open_change`, `vcs.comment`, `vcs.review` and `vcs.read_change` (package `packages/cardea-vcs-ops`), with reconcilers, against a fake host and a test credential provider.
    - the `vcs` confined-worker profile (the `vcs.*` handlers and reconcilers run in ADR-028 workers with the profile's credential), staged inputs for confined workers, and the read-only readiness check `vcs.check_repository`;
    - the owner's decisions that change the code: a fine-grained personal access token as the credential (question 7) and `APPROVE` allowed per pool (question 2). See [Owner decisions implemented](#owner-decisions-implemented-2026-11-16-portable-tests-only).

    See [Implementation status](#implementation-status).
  - **Not implemented:**
    - the GitHub App minter, the documented upgrade from the personal access token (question 7);
    - allow-listed package registries for gates (question 8), which a separate change implements;
    - the 30-day retention of change bundles (question 6): today every object is kept for the store's lifetime.
  - **Portable tests only:** everything implemented. The harness is tested against a fake `claude`, and the `vcs.*` operations against a fake repository host. Apart from the Gitleaks fit test, no fit test has run: the `code.session` fit test in `experiments/code-session/` is written, not run, and nothing has run with the real CLI, a real repository host, a real personal access token, a GitHub App or a ruleset. Every other "fit" is from documentation and says so.
  - **Verified on Debian:** nothing yet (plan slice 7); see the [Debian acceptance checklist](../acceptance/debian-adr-034-037.md).
- **Affected components:** [operation broker](../components/operation-broker.md), [installed host operations](../components/installed-host-operations.md), [Debian host](../components/debian-host.md), [identity and capabilities](../components/identity-capabilities.md), [agent runtime](../components/agent-runtime.md).

## Requirement

An agent team (ADR-034) that works on software needs four things the kernel does not provide yet. All of them are installed operations: an operations package that the deployment owns, not new kernel vocabulary.

1. **A coding session as one confined call.** `code.session` checks out a branch of an allowed repository into a scratch workspace, runs a headless coding agent on a task, runs the repository's own gates (`make lint`, `make typecheck`, `make test`, or the target names the host configures), and returns the resulting change and the gate results.
   - It is one long operation call. It keeps no process, workspace or session between calls (no cross-call process state); a later call starts from a branch or a stored change, never from a live process.
   - It has no credential for the repository host and cannot push. Its network is limited to the model API and a read-only source of the repository.
   - Its output is bounded and receipted like any operation; the full change is stored as a kernel object the following calls can cite.
2. **Repository-host effects with receipts.** Operations to push a branch, open a pull request, comment, and submit a review.
   - A pushed branch name is restricted to a configured prefix (`agent/<group>/`, question 3).
   - Each call is idempotent (an ADR-026 scope) and, after an interruption, is reconciled against the repository host's state, never blindly retried.
   - There is no merge operation. Merging stays a human or host decision. *Superseded on 2026-11-17 by the owner: the developer that opened a pull request merges it with `vcs.merge`, once an allowed reviewer approved its head and every check passed (see the [amendment](#amendment-2026-11-17-the-developer-merges)).*
3. **Credentials the agent never sees.** Only the worker of these operations holds a repository-host credential, and only for the call. For now it is a fine-grained personal access token stored as a host secret; the documented upgrade is a short-lived, repository-scoped token minted per call by a GitHub App (question 7). It never appears in arguments, outputs, receipts, prompts, logs or the coding session's environment. Repository-side branch rules are a second, independent guard.
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
| `vcs.read_change {repository, number, diff?, diff_offset?}` | read | none | none |
| `vcs.push_branch {repository, branch, change, expected_head?}` | effect | `/repository`, `/branch`, `/change` | the branch's head on the host equals the change's head commit → applied |
| `vcs.open_change {repository, head, base, title, body, draft?}` | effect | `/repository`, `/head` | `GET .../pulls?head=<owner>:<head>&state=all` returns the pull request → applied, with its number and URL |
| `vcs.comment {repository, number, body, client_key}` | effect | `/repository`, `/number`, `/client_key` | a comment on the pull request carries the marker `cardea:<effect_key>` → applied |
| `vcs.review {repository, number, event, body, client_key}` | effect | `/repository`, `/number`, `/client_key` | a review by the app carries the marker → applied |

- There is no `vcs.merge`; the owner merges. *Superseded on 2026-11-17: see the [amendment](#amendment-2026-11-17-the-developer-merges).* `vcs.review` accepts `COMMENT` and `REQUEST_CHANGES`. `APPROVE` is refused unless the host owner allows it, for every caller or for named pools (question 2, decided by the owner).
- `code.session` returns, within the 64 KiB receipt: the base and head commits, file and line counts, a bounded diff excerpt, each gate's target, exit status, duration and output tail, and the agent's bounded summary, turns and cost. The whole change is a `git bundle` published as a task-owned kernel object (the ADR-014/015 publication path), which `vcs.push_branch` cites by exact version. Nothing else carries state between calls.
- ADR-034 records cite these receipts: a `submit` transition can require a `vcs.open_change` receipt for the record (`requires_receipt`), as the ADR's worked example already does.

### 2. Execution

- **`code.session`** runs as an ADR-028 confined worker under a new host-owner profile (name it `coding`):
  - a scratch workspace and no other writable path;
  - network to the model API and to a read-only repository source only: either a host-side mirror served over the loopback, or the host's read endpoint with no credential for public repositories; and the package registries the gates need (question 8, decided by the owner);
  - no repository-host credential at all.

  Inside it, the worker clones `base_ref` with hooks disabled for its own git commands, runs the selected harness non-interactively (for Claude Code: `--bare`, an explicit tool list, `--permission-prompts none` and a budget; for OpenCode: the pinned binary with its permissions set to the same effect), commits the result, runs the gate targets with their own timeouts, and publishes the bundle. The harness and model are an ADR-033 selection, granted like an agent run's (`harness:<id>`).
- **`vcs.*` effects** run as confined workers under a second profile (`vcs`) whose network allow-list is the repository host's API and git endpoints. They never check out the change: `vcs.push_branch` fetches the bundle into an empty bare repository and pushes one ref, with hooks disabled.
- **Timeouts.** ADR-023 caps an operation at 600 s, which a coding session plus a full test run can exceed. This needs a small ADR-023 amendment: a confined operation may declare up to the profile's wall-time limit (question 9, decided by the lead).

### 3. Credentials

*Revised after the owner's answer to question 7 (2026-11-16).*

- **Now: a fine-grained personal access token.** The host owner creates one on the repository host, limited to the registry's repositories, with the permissions the operations need (contents, pull requests and issues read and write; metadata read) and an expiry. It is stored as the `vcs` profile's root-only credential file, a host secret that systemd loads into each worker unit (ADR-010/028). The host-owner file names the provider and the expiry, never the token: `{"kind": "personal_access_token", "expires_on": "<date>"}`.
- **Injected only inside `vcs.*`.** The worker reads the token for each call through the `RepositoryCredentials` provider and uses it only for that call's HTTP `Authorization` header and its push child (`GIT_ASKPASS`). The kernel never reads it. Arguments, tool results the agent sees, receipts, the store, job documents, staged inputs, worker output and logs never contain it (ADR-026's failure-reason rules already keep exception text out). From its expiry day on, every call is refused with `credential_unavailable` and nothing is sent.
- **The trade-off, accepted by the owner:**
  - *The secret lives longer:* months, until the owner rotates it, instead of at most an hour. A leaked token is useful to an attacker for that long.
  - *No per-call minting:* the token's repositories and permissions are fixed when it is created. A call cannot narrow it to one repository or to the permissions that call needs, so every call carries the whole grant.
  - *Branch protection is the second guard:* the repository's rules (below), checked by `vcs.check_repository`, are what stop a misused token from pushing outside the agent prefix or merging into the default branch.
- **The documented upgrade: a GitHub App.** The host owner installs an App on the allowed repositories, and the App's private key becomes the profile credential. For each `vcs.*` effect, the worker mints an installation access token restricted to the single repository (`repository_ids`) and to the call's permissions (`contents: write` for a push, `pull_requests: write` or `issues: write` for the others), valid for at most an hour. It drops into the same provider interface; it is not built.
- The coding session never receives a token.
- **Second guard on the host:** a ruleset restricts pushes to branches matching the prefix, blocks force pushes and deletions elsewhere, and keeps the default branch behind required reviews that the token cannot bypass. `vcs.check_repository` reports whether the ruleset exists and what is missing; Cardea cannot enforce it.

### 4. Content gate

- A kernel-owned configuration lists the secret rules (a Gitleaks configuration) and a denylist file (one term per line, case-insensitive), both host-owner files outside worker reach.
- **Before push:** the `vcs.push_branch` worker scans the added lines of the change with Gitleaks and the denylist. A match fails the call with `content_refused` naming the rule and file, never the matched text.
- **Before send:** titles, bodies, comments, review bodies and commit messages are checked by the same gate inside the call that sends them.
- The gate runs before any network call, so a refusal sends nothing and is a definite failure, not `outcome_unknown`.
- **Scope (question 5, decided by the owner):** the change's added lines plus every text that leaves the host (pull-request titles and bodies, comments, review bodies and commit messages), not the whole tree. A refusal also sends kernel mail to the run's supervisor (ADR-034 decision 4).

### 5. Authority

- Each operation is a grant (ADR-023). Repositories are resource-qualified like ADR-033's `harness:<id>`: a call names a repository alias from host configuration, and needs `repo:<alias>` in its grants and in the supervisor's live ceiling (question 4, decided by the owner).
- Operations that leave the host can carry ADR-036 rules, for example "always" for `vcs.open_change` on a protected repository.
- Branch names come from the operation, not the agent: `agent/<group>/<record or task>` by default, inside the configured prefix (question 3, decided by the owner).

### Content gate: implementation (2026-11-16)

**Gitleaks as a pinned binary, or a port of its rules?** Both were fit-tested on Gitleaks v8.30.1, and the binary was chosen. The evidence is in [`gitleaks-fit-evidence-2026-11-16.json`](gitleaks-fit-evidence-2026-11-16.json).

- **A rule port loses fidelity.** The default configuration has 222 rules.
  - Of the 221 with a regex, 25 do not compile with Python's `re`: Go RE2 allows `(?i)` in the middle of a pattern and `\z`, and Python does not.
  - 130 rules also depend on an entropy threshold, 221 on keyword prefilters, and 9 on allowlists, plus a global allowlist and stopwords.
  - A port would need a regex translator and a re-implementation of each of those, and it would drift from upstream with every release.
- **The binary fits.**
  - `gitleaks stdin` scans the text it is given in about 0.3 s, and `--redact` keeps secrets out of its report.
  - It reads only what the kernel passes: an explicit `--config` (or its built-in rules), an empty ignore directory (`-i`), a cleaned environment (so `GITLEAKS_CONFIG` has no effect) and a scratch working directory.
  - It honours an inline `gitleaks:allow` comment unless `--ignore-gitleaks-allow` is passed. The fit test confirmed the bypass, so the gate always passes the flag: content must never be able to exempt itself.
- **Pinned like OpenCode.** `packaging/fetch_gitleaks.py` downloads the release archive and checks the archive's SHA-256 against the release's checksums. The host configuration pins the binary's own SHA-256, which the gate checks before every use.

**What is built** (`cardea.service.content_gate`):
- **Configuration.** A `content_gate` section in the host configuration: the binary and its SHA-256, an optional Gitleaks TOML, a denylist file, `notify_supervisor` (default `true`) and a timeout. Every file is private and read like the other secret files.
- **Denylist.** One case-insensitive term per line, or `re:<pattern>` for a regular expression. Blank lines and `#` comments are ignored. The rule ID is `denylist:<line>`, so the term itself is never shown. An invalid pattern, a pattern that matches the empty string, an over-long line, a file that is not UTF-8 and a file that is not private all fail closed with `content_gate_unavailable`, and the message names the line at most.
- **Inputs.**
  - The added lines of a unified diff, located as `<file>:<line>` in the new file. An added line outside a hunk refuses the diff.
  - Free-text fields, located as `<field>` or `<field>:<line>`.
- **The check.**
  - The kernel hands every in-process installed operation `OperationContext.check_content(operation=…, diff=…, texts=…)`. The future `vcs.*` operations call it before any network request.
  - A finding raises `OperationRejected` with code `content_refused`. The refusal names each rule and location (at most ten, then a count), never the matched text. Nothing is sent and the failure is definite.
  - When the gate is not configured, the scanner is missing or tampered with, or the scan fails, the call is refused with `content_gate_unavailable`.
- **Supervisor mail.** A refusal also sends kernel mail to the run's supervisor (ADR-034 decision 4), listing the rules and locations. The owner decided this on question 5. A host owner can still turn it off with `notify_supervisor: false`.
- **Scope.** Added lines plus every text sent, as the owner decided on question 5. Whole-tree scanning is not built.
- **Tests** (`apps/api/tests/service/test_content_gate.py`; the Gitleaks tests run in CI with the pinned binary):
  - diff and text locations;
  - the denylist's terms and patterns, case-insensitive;
  - five ways a denylist fails closed;
  - the binary's digest pin;
  - a fake secret, assembled at runtime, found in an added line despite an inline `gitleaks:allow`, and in a text field;
  - a clean change sent;
  - a denylisted term and a secret each refusing a call through an installed operation, with nothing sent, and neither the receipt's refusal nor the supervisor's mail containing the matched text;
  - the notice switched off;
  - no gate, or a malformed one, refusing the call.

### Content gate: redaction and confined workers (2026-11-16)

**Refused content is redacted from the store.** A `content_refused` call used to keep its arguments in its receipt and in the task's bound request. Now, in the transaction that records the refusal:
- **Strings replaced.** Every argument string that is, or contains, a refused input or a matched line becomes `{"redacted": "content_refused", "sha256", "bytes"}`. The bound request (`task_bindings.request_json`) is scrubbed the same way. Matched lines shorter than six characters are not used to find copies.
- **Identity kept.** The receipt's `request_digest` is unchanged. Its arguments gain `redaction: {reason, request_digest, arguments_sha256}`, so the call's identity and intent digest still hold.
- **Kernel model rule.** `OperationReceipt` accepts a digest that differs from its request only for a failed `content_refused` receipt whose marker carries that digest. `KernelTransaction.redact_refused_operation` refuses any other receipt.
- **Bound requests.** Migration `m17_content_redaction` narrows the `task_bindings` immutability trigger to allow exactly that rewrite. Every other column, the request digest included, must be unchanged; the new request must carry the marker; and the task must hold a `content_refused` receipt.
- **No copies left behind.** Service connections run with `PRAGMA secure_delete=ON`, so freed pages are zeroed. A refusal truncates the write-ahead log (`wal_checkpoint(TRUNCATE)`, best effort: a busy reader leaves it to SQLite's next checkpoint).
- **Never replayed.** A refused call is definite, never `outcome_unknown`. Its effect scope is `failed`, so it is neither reconciled nor replayed. Resubmitting the same request ID still finds its task.
- **Agent runs.** A tool call refused in an agent run is redacted the same way. The attempt-log tool events carry no arguments. A retried attempt's journal prompt lists the call as `content_refused (<rule> at <location>; …)` with its redacted arguments. A call held for approval and run as `apr-NN` (ADR-036) also has its frozen intent (`approval_requests.arguments_json`) scrubbed. `m17` narrows `approval_intent_immutable` the same way: every other column and digest must be unchanged, the marker present, and the `apr-NN` receipt failed with `content_refused`.
- **Event payloads** carry only digests, and the gate writes no object. Tests scan every file under the store's directory (the SQLite file, its log, objects and attempt logs), the kernel's logs and the mail for the denylist term and for a fake secret. Three paths are covered:
  - an operation task;
  - an agent run whose refused call is followed by a retried attempt, which also checks the retried prompt (`test_content_gate_agent_runs.py`);
  - an agent run whose call was held for approval, approved, and refused as `apr-01`.

**The gate inside confined workers.** An ADR-028 worker reaches the kernel through the channel a confined agent session already uses: a per-call loopback endpoint with a bearer token (`content_gate.GateEndpoint`).
- **What the worker gets.** Only the endpoint's URL and token, in its job document. `cardea.hosts.operation_worker` turns them into `OperationContext.check_content`, which posts the diff and texts. The kernel runs the gate and answers `ok` or the refusal.
- **What stays outside.** The gate's configuration, denylist and scanner binary never enter the sandbox.
- **The refusal is the kernel's.**
  - If the worker reports the refusal, the kernel raises its own recorded refusal, and the receipt is redacted as for an in-process call.
  - If the worker reports success after a refusal, the effect is left in doubt (`content_refusal_ignored`, `outcome_unknown`), because it may have sent the content.
- **When the callback cannot be reached.** Without a callback, or when it cannot be reached, the worker's check refuses with `content_gate_unavailable`. That covers a profile with no network, since only profiles that reach the loopback tool endpoint (`model_api_only`) can reach the gate.
- **Tests.** `test_content_gate_confined.py` runs the real worker loop against the real endpoint. It covers a clean change, a refusal with redaction, a refusal the worker ignored, no gate, and no callback.

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

## Decisions (lead, 2026-11-16)

The owner may veto either.

1. **Question 9, long calls.** ADR-023's 600 s cap is raised for confined operations only, to the profile's declared wall time, with a hard maximum of two hours. `code.session` stays one call. See [ADR-023, Amendment (2026-11-16)](023-installed-operation-registry.md#amendment-2026-11-16-long-confined-operations).
2. **Question 1, first harness.** Claude Code in `--bare` mode is the first harness for `code.session`, consistent with the subscription-token decision for agent runs. *These two parts conflicted. The owner's answer to question 10 settles it: Claude Code with the subscription token, without `--bare`.*
3. **Question 8, network for gates.** *Superseded by the owner's answer (2026-11-16): allow the package registries.* The lead had recommended no network beyond the model API, with dependencies from a pre-provisioned, read-only package cache.

## Decided by the owner (2026-11-16)

Each entry is the owner's answer to the question of that number in [Questions for the owner](#questions-for-the-owner), and what it changes.

- **Question 2, `APPROVE` reviews: allowed, refused by default.** The generic default stays: `allow_approve` is off unless a host owner enables it. A host may allow it for every caller (`true`) or only for named pools (`["<group>.<pool>"]`). The dev-team rehearsal allows it for its principal engineers only. Agents still have no merge operation: the owner merges. *Superseded on 2026-11-17 by the [amendment](#amendment-2026-11-17-the-developer-merges): the opener merges.*
- **Question 3, branches: `agent/<group>/`,** as implemented.
- **Question 4, repository authority: `repo:<alias>`,** as implemented, for `code.session` and `vcs.*`.
- **Question 5, content gate: the added lines plus the pull-request text, and mail the supervisor on refusal,** as recommended and implemented. Every text that leaves the host is scanned: titles, bodies, comments, review bodies and commit messages. The mail was already wired (`notify_supervisor`, default on).
- **Question 6, change bundles: a 10 MiB cap, kept 30 days.** The cap is implemented (`code.session` artifacts and the `vcs` profile's staged input). The retention is not: change bundles are task-owned objects, and every object is kept for the store's lifetime today. Enforcing 30 days needs object expiry in the kernel, a separate change.
- **Question 7, credentials: a fine-grained personal access token for now.** A GitHub App is not required. The token is stored as a host secret and injected only inside `vcs.*`, through the existing profile credential and `RepositoryCredentials` provider. The App stays the documented upgrade. The trade-off is in [decision 3](#3-credentials): the secret lives longer, nothing is minted per call, and branch protection is the second guard.
- **Question 8, network for gates: allow the package registries.** The `coding` profile's allow list admits the package registries the gates need, beside the model API, so `make test` may install packages; the read-only cache stays available. Gates still get no repository-host credential and no other host. The cost: repository code that the gates run can reach those registries. Publishing to one needs a credential the sandbox does not hold. A separate change implements it.
- **Question 10, the first harness: the subscription token without `--bare`,** with the explicit isolation flags, as implemented. The fit test records whether repository hooks or MCP servers run and whether the model follows the repository's `CLAUDE.md`.

The owner did not change the lead's decisions on questions 1 (as amended by question 10) and 9.

## Amendment (2026-11-17): the developer merges

**Decided by the owner (2026-11-17).** The principal engineer approves pull requests on the repository host, and the pull request's own developer then merges it. This supersedes "there is no merge operation in the MVP" (requirement 2), "there is no `vcs.merge`; the owner merges" (decision 1) and the same words in question 2's answer.

**Why two identities.** Every pull request an agent opens is authored by one bot account, and the repository host refuses an author's approval of its own pull request. So the principal approves as a second identity: the owner's account, through a fine-grained token limited to the one repository with pull-request write only. The developer merges as the bot account.

**Reasoning.**
- **Merging was the team's bottleneck.** The owner had to merge every approved change.
- **Four eyes, enforced twice.** The host's ruleset (one approval, approval of the last push, no force push, no deletion) still decides on the host. Cardea additionally requires its own record of who opened the pull request and who approved which head.
- **Attributable.** The member that wrote the change is the one that merges it, and the kernel's receipts say so.

**What is built** (generic, configured by the host owner):
1. **Per-caller credentials.** A worker profile may load several named credentials ([ADR-028 amendment](028-configurable-worker-profiles.md#amendment-2026-11-17-several-named-credentials)). The vcs provider `personal_access_tokens` holds one `expires_on` per token, a `default` token and `pools: {"<group>.<pool>": <name>}`. The kernel-resolved member id selects the token, matched as `allow_approve` is. No argument takes part, and a caller outside every listed pool gets the default.
2. **`vcs.merge`,** an effect with a receipt, the ADR-026 scope `/repository`, `/number` and the reconciler `vcs.find_merge`. It needs the `vcs.merge` grant and `repo:<alias>`. Each of these refusals is typed and sends nothing:
   - `merge_not_configured`: the host has no `merge` section.
   - `merge_not_opened`, `merge_not_opener`: the caller must be the member recorded in the first `vcs.open_change` receipt for that pull request. This is Cardea's receipt, not the host's author field, which is the shared bot account.
   - `merge_not_approved`, `merge_approval_stale`: the kernel must hold a `vcs.review` APPROVE receipt from an allowed pool whose recorded `commit_id` is the pull request's current head.
   - `merge_host_not_approved`: the host must show the configured `reviewer_login`'s latest deciding review as an approval whose `commit_id` is the head.
   - `merge_checks_missing`, `merge_checks_pending`, `merge_checks_failed`: there must be at least one check run on the head, none pending, and every one concluded `success`.
   - `merge_not_mergeable`, `merge_not_open`, `merge_already_merged`, `merge_head_moved`: the host must report the pull request open, ready and mergeable, with the head the caller expects.

   Then the operation squash-merges with the head SHA pinned (`PUT .../merge` with `sha`). The host answers 409 if the head moved, which becomes `merge_head_moved`. An interrupted merge is `outcome_unknown`, and `vcs.find_merge` reads the pull request before anything is retried: merged means applied.
3. **Receipts as evidence.** A confined worker has no store, so the descriptor declares `evidence` queries ([ADR-023 amendment](023-installed-operation-registry.md#amendment-2026-11-17-evidence-from-the-kernels-own-receipts)). The kernel finds the matching receipts at admission and sends them in the job document.
4. **`vcs.review`** records the head it approved (`commit_id`, descriptor version 2).
5. **`vcs.check_repository`** now requires approval of the last push, no force push and no deletion on the default branch.
6. **The kit and the rehearsal.** The small-host kit's `vcs` profile loads `github-author` and `github-reviewer`, and has a `vcs-merge` worker. As installed, nobody uses the reviewer token, `APPROVE` is refused and merging is refused; the README says how to enable them for a team. In the dev-team rehearsal the developers hold `vcs.merge`, and `dev-team.principal` holds the reviewer token and `allow_approve`.

**Standards first.**
- **The host's auto-merge** (merge when the branch rules pass) is the closest existing mechanism, and the host's rules stay the final word. But it cannot check Cardea's own facts, such as which member opened the change or which pool approved it, and enabling it is itself an effect someone must be allowed to make. `vcs.merge` uses the host's rules and adds the kernel's checks before the merge request.
- **Merge bots** (hosted services that merge on rules) add a third party that holds a write credential. Rejected.
- **A second GitHub App for the reviewer** is the upgrade path, as for the author: per-call tokens minted for one repository instead of a long-lived personal token. It fits the same named-credential provider.

**Risks.**
- **The owner's account approves without the owner reading.** The host's audit log shows the owner's approval. Anyone reading the host must know that this account's approvals on that repository are the principal agent's.
- **A misused reviewer token can approve any pull request in its repository,** including one opened outside Cardea. It is limited to one repository and pull-request write, it expires, and it is selected only for the reviewer pool. Whether pull-request write alone lets it merge on the real host has not been checked. Check it before production, and if it can, rely on the branch rules (an approval by someone other than the last pusher).
- **Both tokens are in one sandbox.** The separation rests on the vcs worker's code and the kernel's caller identity, not on confinement. The `vcs` worker runs no untrusted commands, unlike `coding`.
- **A prompt-injected principal could approve a bad change.** The gates and checks still have to pass, the content gate scans what leaves the host, and a host owner can put an ADR-036 approval rule on `vcs.merge` or on `vcs.review` for a protected repository.
- **A change after approval** is caught three ways: the receipt check, the host's own approval check of the head, and last-push approval. The SHA-pinned merge closes the window between the checks and the merge.
- **The evidence lookup scans receipt JSON without an index.** That is acceptable at a small host's volume.

**Tests** (fake host only; nothing has run against the real host):
- `test_vcs_merge.py`:
  - the opener merges an approved, green pull request;
  - each refusal: unapproved, stale approval after a new push, red checks, pending checks, no checks, another developer, not mergeable, the wrong host reviewer, no `merge` section, an approval from a pool no longer allowed;
  - the principal cannot merge;
  - an interrupted merge is reconciled from the host, with exactly one merge;
  - only the reviewer pool gets the reviewer token, including near-miss callers, and each token's expiry refuses its own callers only;
  - each caller reaches the host with its own token;
  - the host refuses the bot account's approval of its own pull request;
  - the evidence binding keeps old digests and refuses bad paths.
- `test_vcs_confined.py`:
  - in the worker, the developer's calls carry only the author token, and the caller's pool alone switches to the reviewer token;
  - the containment scan finds neither token in tool results, prompts, receipts, the store's files, logs, worker output or job documents;
  - a merge in its worker receives the kernel's evidence;
  - the readiness checks above.
- `test_confined_launcher.py`: the named credentials.
- `make teams-test`, the rehearsal:
  - a developer merges an approved, green pull request;
  - merging is refused while CI is pending or red, for another developer's pull request, before any approval, and with a stale approval after a new push;
  - the principal's pool cannot merge.
- `test_deploy_examples.py`: the kit's two credentials, the `vcs-merge` worker and the off-by-default configuration.

## Implementation status

### Slices 2 and 3 (implemented 2026-11-16, portable tests only)

- **ADR-023 amendment** as recorded there: timeouts up to the profile's wall time (two hours at most), a heartbeat lease for long confined calls, and artifacts stored as task-owned objects.
- **`code.session`** is the package `packages/cardea-coding-ops` (entry point `coding.code.session`, descriptor `code.session.json`: confined profile `coding`, worker `code-session`, a one-hour timeout, an ADR-026 scope on `/repository`, `/base_ref` and `/task_key`, artifacts up to 10 MiB). The host owner configures it with a read-only JSON file named by `CARDEA_CODING_CONFIG`, which holds the `harness` command and the variable names it may receive. Repositories come from the host repository registry (`CARDEA_REPOSITORIES`): each has a read-only `source` and its `gates`. A call names an alias, never a path or a command, and needs `repo:<alias>` for it (descriptor version 2).
- **In the worker:** a clone of `base_ref` with git hooks and global configuration off; the harness run in the checkout with the task; a commit of everything it changed; a diff summary; the gates; and the `change.bundle` artifact (`git bundle` of the commits on top of `base_ref`). The receipt holds the base and head commits, file and line counts, a diff excerpt of at most 16 KiB, each gate's exit status, duration and output tail, the harness's exit status and output tail, and the bundle's object references. The kernel stores the bundle in parts of at most 1 MiB.
- **No repository-host credential.** Every command runs with an environment built from nothing (fixed `PATH`, scratch `HOME`, no global git configuration), plus the variables the harness entry names; names that could carry a repository-host credential (`GH_*`, `GITHUB_*`, `GIT_*`, `*ASKPASS*`, `*CREDENTIAL*`, `SSH_AUTH_SOCK`) cannot be passed through. A test plants such credentials in the worker's own environment and checks that neither the harness nor the gates see them, and that no receipt holds them.
- **Tests** (`apps/api/tests/service/test_code_session.py`, 11 tests): a session against a local bare-repository fixture with a fake harness that applies a scripted change and fake gates with configurable exit status, producing a bounded receipt and a bundle that verifies and applies to a fresh clone; failing gates reported; no host credential reaching the harness, the gates or the receipt; a refused credential passthrough and an unknown repository; an oversized change refused and reconciled as not applied; a timeout beyond the profile's wall time refused; the hard timeout limits; and the heartbeat tests listed in the ADR-023 amendment.
- **Not yet (at that point):** a real harness and its fit test, and the `coding` profile; see the next section. Slices 4–7 are not implemented.

### Claude Code harness and the `coding` profile (implemented 2026-11-16, portable tests only)

- **Harness entry.** `harness.kind: "claude-code"` in the `code.session` configuration (`cardea_coding_ops/claude_code.py`). It runs the CLI bundled with the Agent SDK that ADR-033 pins (`claude-agent-sdk==0.2.163`, Claude Code 2.1.286), unless the entry names a `binary`. Claude Code runs headless in the checkout with the task on standard input:
  - `-p --output-format json --model <model> --max-turns <n> --max-budget-usd <usd>`;
  - tools `Bash,Read,Edit,Write,Glob,Grep`, with the file tools allowed only under the checkout (`Read(./**)` and the like);
  - `--permission-mode dontAsk --permission-prompts none`, so anything else is denied without a prompt;
  - `--setting-sources user` with an empty `CLAUDE_CONFIG_DIR` and `HOME` in the scratch, so no project or local settings, and so none of the repository's hooks or permission rules;
  - `--settings '{"disableAllHooks": true}'`;
  - `--strict-mcp-config --mcp-config '{"mcpServers": {}}'`, so no `.mcp.json` server;
  - `--disable-slash-commands` and `--no-session-persistence`.
  
  With `credential_kind: "api_key"` it also passes `--bare`.
- **Credential, the ADR-033 way.** The worker reads the profile's single credential at `$CARDEA_CREDENTIAL`; systemd loads it into the unit. Only the harness's environment receives it, as `CLAUDE_CODE_OAUTH_TOKEN` (`oauth_token`, the default) or `ANTHROPIC_API_KEY` (`api_key`), together with the proxy variables the helper set for the profile's model-API egress. Gates never see it. If the model credential appears in the change, as text with binary files included, or in any command's output, the call is refused with `credential_in_change` and returns nothing.
- **Receipt.** The harness record holds the exit status, whether it timed out, the duration, the turns, the stop reason, the cost if reported, whether it errored, and a summary of at most 2 KiB. Claude Code's standard output and standard error are not copied.
- **Gates and the package cache.** A `gate_env` object in the configuration gives the gates fixed variables, such as `UV_CACHE_DIR`, `UV_OFFLINE=1`, `PIP_NO_INDEX=1` and `PIP_FIND_LINKS`, pointing at the read-only cache. Credential-shaped names are refused, as for the harness.
- **`coding` profile** (`deploy/small-host/cardea/cardea-host-workers.json`):
  - `model_api_only` through its own proxy port `127.0.10.5`, allowing `api.anthropic.com` only (`squid-workers.conf`);
  - 4 GiB of memory, 200 % CPU, 512 tasks, 4 GiB of scratch and 3600 s of wall time;
  - `output_kb` 14336, which holds a 10 MiB bundle in base64 plus the receipt;
  - concurrency 1;
  - the credential `/etc/cardea/credentials/claude-code`.
  
  The `code-session` worker binds `/opt/cardea` (the kernel environment with the pinned CLI) and `/opt/cardea-code` read-only. The latter holds the host owner's bare repository mirrors, the package cache and `coding.json`, an example of which is `cardea/cardea-coding.json`.
- **Tests:**
  - `test_code_session_claude.py` (6 tests) runs against a fake `claude` that records its argv and environment, in a fixture repository whose `.claude/settings.json` declares hooks and permission rules and whose `.mcp.json` declares a server. It asserts the flags; that the configuration directory and `HOME` are in the scratch; the credential variable for each kind; the proxy; that no repository-host credential is present, including one planted in the worker; the parsed record; and the refusal of a change carrying the credential.
  - `test_deploy_examples.py` checks that the profile fits the descriptor: wall time, output size and network.
  - `test_code_session_fit_script.py` keeps the fit script consistent with the worker.
  
  The portable tests cannot show that the real CLI ignores the repository's configuration. The fit test checks that.
- **Debian fit test, written, not run.** `experiments/code-session/` (`debian.sh`, `fit.py` and a README) runs one `code.session` through the root helper on a fixture repository with the real CLI. It records the unit's resource use, the harness record, the bundle, the proxy's answers, and whether the fixture's hooks or MCP server left marker files. It needs a subscription token file, the installed `coding` profile and the package cache.

### Slices 4 and 5 without credential minting (implemented 2026-11-16, portable tests only)

- **Package.** `packages/cardea-vcs-ops` installs `vcs.push_branch`, `vcs.open_change`, `vcs.comment`, `vcs.review` and `vcs.read_change` as ADR-023 operations: descriptors, entry points and an in-process handler per operation over the pinned `httpx`. Host configuration is one private file, `CARDEA_VCS_CONFIG`, holding `allow_approve` and the credential provider. The repository aliases, their hosts and branch prefixes come from the host repository registry (`CARDEA_REPOSITORIES`) that `code.session` reads too.
- **Credentials.** A `RepositoryCredentials` provider is asked for one repository and the call's permissions (`contents:write`, `pull_requests:write`, `issues:write` or `pull_requests:read`) at every call.
  - The credential goes only to that call's HTTP client (the `Authorization` header) and to its push child process, through `GIT_ASKPASS` and one environment variable. It is never on a command line, in a URL, on disk, in arguments, outputs, receipts, logs or an agent's environment.
  - The only provider at that point was `FixedTestToken`, for tests and fakes. The personal access token came later (see [Owner decisions implemented](#owner-decisions-implemented-2026-11-16-portable-tests-only)).
- **Operations and reconcilers,** as in decision 1. The reconcilers read the host and never send again:
  - the branch's head equals the named commit;
  - the pull request for the head branch;
  - a comment or review carrying the hidden marker `<!-- cardea:<effect key> -->`.

  A duplicate pull request (422 from another effect scope) returns the existing pull request. Any other 4xx is a definite refusal, `host_refused`; a 5xx or a dropped connection leaves the effect `outcome_unknown` for the reconciler.
- **Pushing.** `vcs.push_branch` takes the change and the commit to push. The change is the `change.bundle` artifact entry that `code.session` returns (its object parts, joined and checked against its `sha256`), or one object version.
  - In a private bare repository it first fetches the host's branch heads, so a thin bundle's base (`base..HEAD`) is present.
  - It verifies the bundle, checks that the bundle holds that commit, and pushes one ref without force, with hooks and credential helpers disabled.
  - A branch already at the commit is not pushed again.
  - `expected_head` refuses a branch that moved.
  - Its effect scope is `/repository`, `/branch`, `/head` rather than `/change`: two bundles of the same commit are the same effect.
- **Rules in the operation, before any network call:**
  - the alias must exist;
  - the branch and a pull request's head must start with `agent/<group>/`;
  - `APPROVE` is refused unless `allow_approve`;
  - every text and the change pass the kernel's content gate.
- **Content gate.** Every sending operation calls the kernel's gate (slice 6, `OperationContext.check_content`) before it sends anything.
  - `vcs.push_branch` passes the commits the host does not have yet, as one unified diff of added lines, and each of their messages as a text field (`commit_message:<sha>`). Its only earlier network use is reading the host's heads.
  - `vcs.open_change` passes the title and body, `vcs.comment` the comment, and `vcs.review` the review body.
  - A finding is a definite `content_refused` that names the rule and place, never the text.
  - A call with no gate bound, or a gate that cannot run, is refused (`content_gate_unavailable`): the gate fails closed.
- **Kernel additions,** generic and additive, with descriptor digests unchanged when unset:
  - descriptor `resource: {grant: "repo", argument: "/repository"}`: the invocation core checks `repo:<value>` in the caller's live ceiling before the intent exists;
  - descriptor `inputs`: argument pointers that name an object version, or an artifact listing (`{sha256, parts}`, the form confined operations return), in the call's namespace. The core reads them under the `objects.read` ceiling: the store verifies each part's digest and the core checks the joined bytes against `sha256` (`input_digest_mismatch`). The handler gets the bytes in `OperationContext.inputs`, and they are never recorded.
  - `OperationContext.group`: the caller's group from the member table, also recorded in the receipt's context for reconcilers;
  - the grant form `repo:<alias>`, which provisioning accepts like `mail:` and `record:`.
- **Not yet (at that point):**
  - the `vcs` confined-worker profile;
  - the App minter;
  - the repository-side ruleset readiness check.

  The profile and the readiness check are in the next section.

  No migration was needed.
- **Tests** (`apps/api/tests/service/test_vcs_ops.py`, with an in-process fake GitHub in `cardea_vcs_ops.fake`: REST over `httpx.MockTransport`, backed by real bare repositories so pushes and ref reads agree):
  - **Flow:** push, open, comment, review and read by a group member's agent run. A repeated push is deduplicated without a host call.
  - **Branches:** outside the group prefix (including `main`, another group and `..`) they are refused before any network call, and a caller outside any group cannot push.
  - **Reviews:** `APPROVE` is refused by default and allowed when enabled.
  - **Repository grant:** a repository outside the run's `repo:` grants is refused by the kernel before the handler runs.
  - **Inputs:** a change from another namespace is not staged.
  - **Crash and reconcile,** for each effect: push, open, comment and review each apply on the host and then drop the connection. The run fails with `reconciliation_required`, and the reconcile endpoint settles each one from host state with exactly one effect on the host.
  - **Duplicate:** a duplicate pull request returns the existing one.
  - **Thin bundles:** a `base..HEAD` bundle passed as a two-part artifact listing, as `code.session` returns it, pushes. The base already on the host is not scanned again, and a listing whose parts do not match its `sha256` is refused before anything runs.
  - **Content gate:** a denylisted term in a pushed file, in a commit message or in a comment is refused with nothing sent and without echoing the term, and with no gate configured nothing is sent.
  - **Token containment:** the token reaches the host's `Authorization` header and appears in no database row, log record, session request or handler representation.
  - **Digests:** descriptors without `resource` or `inputs` keep their digest.

### The `vcs` confined worker, staged inputs and the readiness check (implemented 2026-11-16, portable tests only)

- **Confined.** The installed `vcs.*` descriptors name the executor `{profile: "vcs", worker: "vcs-<operation>"}` and no in-process handler, so the service process never loads the package. Each worker runs `operation_worker <handler> [<reconciler>]`: the handler by default, and its reconciler only for a reconcile job (`"reconcile": true` with a listed `entry`). A worker never runs the handler when asked to reconcile.
- **Confined reconcilers.** For a confined operation that declares a reconciler, the kernel runs the reconciler in the operation's worker, with its own deadline, as a new job on each pass. The worker returns `{applied, output}`. The registry no longer needs a confined operation's reconciler in-process.
- **The `vcs` profile** (`deploy/small-host/cardea/cardea-host-workers.json`):
  - `allow_list` through its own proxy port `127.0.10.6`, allowing `api.github.com` and `github.com` (git over HTTPS);
  - the new optional `kernel_callback: true`, which adds the dedicated endpoint address to the unit's `IPAddressAllow` (`127.0.11.1` since the [ADR-028 amendment](028-configurable-worker-profiles.md#amendment-2026-11-16-a-dedicated-endpoint-address); `127.0.0.1` before). It is allowed only on `allow_list` profiles and lets the worker reach the content gate's per-call loopback endpoint (#53);
  - `input_kb` 12288 for a 10 MiB change bundle;
  - 300 s of wall time, concurrency 2;
  - the credential `/etc/cardea/credentials/github`.
- **Credential, into the worker only.** The provider is now chosen in the host-owner file. `{"kind": "profile_credential"}` reads the profile's single credential at `$CARDEA_CREDENTIAL` for each call, as #54's provider is asked for each call. systemd loads the credential into the unit from a root-only file. The kernel never reads it, and it never appears in a job document, a staged input, a worker's output or the store.
  - A configuration file that holds no secret may be a shared, read-only host-owner file. The worker reads it at `/opt/cardea-vcs/vcs.json`, bound read-only; an example is `cardea/cardea-vcs.json`.
  - A file holding the test provider's token must still be private.
  - A missing credential is a definite refusal, `credential_unavailable`, with nothing sent.
  - The GitHub App minter, the documented upgrade (question 7), fits here: the App's key becomes the profile credential and minting happens in the worker.
- **Content gate** through the #53 callback: the worker's `check_content` asks the kernel over loopback. A refusal keeps its code (`content_refused`) and names no matched text.
- **Rejection codes.** A worker's refusal now carries its code (`{"rejected", "code"}`), so `branch_not_allowed`, `approve_refused`, `content_refused` and the others reach the caller as they do in-process.
- **Group.** The caller's group goes to the worker in the job document, as the recorded context already carries it for reconcilers.
- **Staged inputs.** `OperationContext.inputs` reach confined workers as files:
  - The kernel sends each input's bytes and SHA-256 beside the job document, with a manifest in the document (pointer to file index, SHA-256 and size). It refuses more than 8 inputs or 16 MiB before anything starts (`input_too_large`).
  - The helper checks each digest (`input_digest_mismatch`) and their total against the profile's `input_kb`, which defaults to 0, before its no-relaunch fence. It then writes them as `0444` files in the job's staging and binds that directory read-only at `/run/cardea/inputs` (`$CARDEA_INPUTS`).
  - The worker checks each file's size and SHA-256 against the manifest again.
  - The files go with the job's cleanup. Inputs are never recorded: not in the receipt, the job document or the helper's status.
  - The helper's request limit grows to carry them base64-encoded.
- **`vcs.check_repository`** (read, `repo:<alias>`) reports and enforces nothing. It reads the repository's default branch and the ruleset rules that apply (`GET /repos/{owner}/{repo}/rules/branches/{branch}`) to three branch names: the default branch, a branch outside the agent prefix, and one inside it. These probe names are never created. It reports required checks and recommendations, and lists under `missing` the required ones that fail:
  - **Required:**
    - `default_branch_requires_review`: a `pull_request` rule with at least one approving review;
    - `pushes_outside_agent_prefix_blocked`: `creation` and `update` rules outside the prefix;
    - `agent_prefix_pushable`: neither rule under the prefix;
    - since 2026-11-17, because agents merge: `default_branch_last_push_approval`, `default_branch_no_force_push` (`non_fast_forward`) and `default_branch_no_deletion` (`deletion`).
  - **Recommended:**
    - `agent_prefix_no_force_push`;
    - `default_branch_accepts_merges`: no `update` rule on the default branch, which would also refuse `vcs.merge`.

  It cannot see bypass actors or classic branch protection, and it says so in `not_checked`.
- **Tests:**
  - `test_vcs_confined.py` (9 tests) runs the real worker command as a separate process with only the worker's environment, against the fake GitHub served over loopback HTTP. It covers:
    - push, open, comment, review, read and check, each in its own worker, with the change staged as an input whose manifest matches;
    - token containment: the token reaches the host from the worker, and appears in no database row, log record, session request, job document, staged input, worker output, the kernel's environment or its handlers;
    - refusal codes and the content gate through the callback, with nothing sent;
    - a crash after the host acted, reconciled in the operation's worker as a reconcile job;
    - the readiness report on a repository without rules, with the right rulesets, and with rules that also stop the agents' branches;
    - a tampered staged input refused in the worker;
    - a reconcile job that never runs the handler;
    - a shared configuration that may not hold a token;
    - a missing profile credential refused with nothing sent.
  - `test_confined_launcher.py` covers staged inputs as read-only files bound read-only, digest, size and count refusals before the fence, a profile without `input_kb`, and `kernel_callback` only on `allow_list` profiles.
  - `test_deploy_examples.py` checks that the profile and its six workers fit the descriptors.
  - #54's in-process tests (`test_vcs_ops.py`) keep testing the handler logic, with the executor removed.
- **Not yet:** the App minter (the documented upgrade, question 7), and a Debian fit test of the `vcs` profile against a real repository host. No migration was needed.

### Owner decisions implemented (2026-11-16, portable tests only)

- **Personal access token (question 7).** `cardea_vcs_ops.credentials.PersonalAccessToken`, configured as `{"kind": "personal_access_token", "expires_on": "<date>"}`:
  - it reads the `vcs` profile's credential at `$CARDEA_CREDENTIAL` for each call, as `profile_credential` does;
  - from `expires_on` on it refuses with `credential_unavailable` before any network call;
  - the configuration holds no secret, so it may be a shared, read-only file.

  The small-host kit's `cardea-vcs.json` uses it, and the kit's README says how to create the token.
- **`APPROVE` per pool (question 2).** `allow_approve` is `false` (the default), `true`, or a list of `<group>.<pool>` addresses. A pool entry matches a caller whose group the kernel resolved from its member table and whose member id is `<group>.<pool>.<ordinal>`, the id the kernel gives pool members. Group and pool names hold no dots, so the match is exact. The dev-team rehearsal allows `dev-team.principal`: the principal approves on the host, and the restructured team's `security` pool, which also holds `vcs.review`, is refused.
- **Tests:**
  - `test_vcs_confined.py`:
    - the PAT is the bearer of every REST call the worker makes, and a push, a pull request, a comment and a read succeed with it;
    - a containment scan finds the PAT in none of these: the tool results the agent read, its prompts, receipts, any database row, any file under the store's directory, the kernel's logs, the worker's output and standard error, job documents, staged inputs, the kernel's environment or its handlers;
    - an expired PAT sends nothing;
    - unit tests of the provider: expiry, `repr` and `str` redacted, and a shared configuration accepted.
  - `test_vcs_ops.py`: `APPROVE` refused by default, refused for another pool, allowed for the caller's pool and allowed for everyone; and exact matching of pool entries, including a longer pool name, another group and a caller outside a group, plus invalid entries refused.
  - `make teams-test`: the principal's approval lands on the host, and security's is refused.
  - `test_deploy_examples.py`: the kit uses the PAT provider with `allow_approve` off.

  No migration was needed.

### Milestone 1 dress rehearsal (2026-11-16, portable)

`examples/teams/dev_team.py` (`make teams`, tested by `make teams-test`) runs an ADR-034 software team on `code.session`, `vcs.*`, the content gate, records with claims, mailboxes, spend accounts and `expect` with `any_of`, with fake backends only. It needed no kernel change. It found:

- **Receipt evidence needs a record link.** `requires_receipt` accepts a cited call when its `record_id` argument names the record, or when its idempotency context is the record's `<record>.<epoch>`. Only claim follow-up runs have that context. `vcs.open_change` had no `record_id`, so a developer that claims and opens a change in one run could never `submit`.
  - Fixed in the package: `vcs.open_change` and `vcs.push_branch` take an optional `record_id`, recorded in the receipt and never sent to the host. Their descriptors are at version 2.
- **`code.session` has no repository grant.** It does not declare a `repo:<alias>` resource, unlike `vcs.*`, so a run granted `code.session` may work on any repository in the coding configuration. 
  - Fixed (lead decision): `code.session` declares `resource: {grant: "repo", argument: "/repository"}` at descriptor version 2. A run without `repo:<alias>` is refused with `task_grant_denied` before any worker starts.
- **Two alias configurations.** `CARDEA_CODING_CONFIG` and `CARDEA_VCS_CONFIG` each name repositories by alias. A coding alias's read-only source must be the same repository as the vcs alias's host repository, or the base of `code.session`'s thin bundle is not found at push. Nothing checks that they agree.
  - Fixed (lead decision): one host repository registry, `CARDEA_REPOSITORIES` (`cardea.service.repositories`), read by both packages. Each alias holds the read-only mirror `source`, the host (`owner`, `name`, `api_url`, `git_url`; optional for an alias only `code.session` reads), the `gates` and the `branch_prefix`. A separate file, rather than a section of the kernel's configuration, because the confined `code-session` worker must read it and cannot read the kernel's configuration. The kernel checks it at start. For one release the package files' `repositories` (and the vcs `branch_prefix`) still work behind a deprecation warning. If both are set and disagree, the kernel refuses to start and calls are refused with `repositories_disagree`. See [operations](../operations.md#repository-registry).

## Verification

Portable tests only, for the implemented parts (all in `apps/api/tests/service/`): `test_content_gate.py`, `test_content_gate_agent_runs.py` and `test_content_gate_confined.py` for the gate; `test_code_session.py`, `test_code_session_claude.py` and `test_code_session_fit_script.py` for slices 2 and 3; and `test_vcs_ops.py` for slices 4 and 5. The Debian fit test in `experiments/code-session/` is written, not run. Nothing has run on Debian, or against a real repository host, a GitHub App or a ruleset; see the [Debian acceptance checklist](../acceptance/debian-adr-034-037.md). The rest of this record is a proposal.

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
- tokens with narrower scopes than an installation token (for example per-branch) become available;
- a deployment needs per-call minting or shorter-lived secrets than a personal access token gives: move to the GitHub App (question 7).

## Questions for the owner

All decided; the answers are in [Decided by the owner](#decided-by-the-owner-2026-11-16) and [Decisions](#decisions-lead-2026-11-16).

1. *Decided by the lead (2026-11-16): Claude Code in `--bare` mode; amended by the owner's answer to question 10 (2026-11-16): without `--bare`.* Which harness should `code.session` run first: Claude Code in `--bare` mode, or OpenCode with a configured provider? Both are already pinned for agent runs.
2. *Decided by the owner (2026-11-16): allowed, refused by default; a host enables it, for every caller or for named pools.* May an agent submit an `APPROVE` review? The proposal refuses it by default, because an app's approval could satisfy the repository's review rule and amount to an unattended merge path. **Lead recommendation (2026-11-16, accepted by the owner):** refuse by default; a host owner may enable it per host (`allow_approve`). Implemented that way.
3. *Decided by the owner (2026-11-16): `agent/<group>/`.* Branch naming: a fixed prefix (`agent/*`), or one per group (`agent/<group>/*`) so rulesets can separate teams? **Lead recommendation (2026-11-16, accepted by the owner):** one prefix per group, `agent/<group>/`, checked in the operation from the kernel's member table. Callers outside a group cannot push. Implemented that way.
4. *Decided by the owner (2026-11-16): `repo:<alias>`.* Repository authority: resource-qualified grants `repo:<alias>` with aliases in host configuration, or one installed package per repository? **Lead recommendation (2026-11-16, accepted by the owner):** `repo:<alias>` grants with aliases in host configuration. Implemented that way.
5. *Decided by the owner (2026-11-16): the added lines plus the pull-request text, and mail the supervisor on refusal.* Content gate scope: added lines only, or the whole tree of the pushed branch? And should a refusal notify the supervisor by mail (ADR-034 decision 4) as well as failing the call?
6. *Decided by the owner (2026-11-16): 10 MiB, kept 30 days.* Change transport: what size cap and retention for change bundles stored as objects?
7. *Decided by the owner (2026-11-16): a fine-grained personal access token for now; the GitHub App is the documented upgrade.* Credentials for small deployments: is a GitHub App required, or is a fine-grained personal access token an accepted fallback (no per-call minting, longer-lived secret)?
8. *Decided by the owner (2026-11-16): allow the package registries.* Network for gates: `make test` often installs packages. Allow-list the package registries in the `coding` profile, or require pre-provisioned caches and no network beyond the model API?
9. *Decided by the lead (2026-11-16): raise the cap for confined operations to the profile's wall time, at most two hours.* Long calls: raise ADR-023's 600 s cap for confined operations to the profile's wall time, or split a session into code and gate calls?
10. *Decided by the owner (2026-11-16): the subscription token without `--bare`, with the explicit isolation flags.* `--bare` and the subscription token (raised 2026-11-16). Claude Code's documentation says bare mode does not read `CLAUDE_CODE_OAUTH_TOKEN`, and that a script passing `--bare` must authenticate with `ANTHROPIC_API_KEY` or an `apiKeyHelper` ([headless mode](https://code.claude.com/docs/en/headless), [authentication](https://code.claude.com/docs/en/authentication)). Question 1's decision, `--bare` with the subscription token, therefore cannot be met as written. The implementation supports both:
    - with a subscription token (`oauth_token`, the default and the kit's setting), Claude Code runs without `--bare`, and the isolation comes from explicit flags ([CLI reference](https://code.claude.com/docs/en/cli-reference)): `--setting-sources user` with an empty configuration directory, `disableAllHooks`, an empty strict MCP configuration and no slash commands;
    - with an API key (`api_key`), it also passes `--bare`.

    Which should the first harness use: the subscription token without `--bare`, relying on those flags and on the fit test's evidence that repository hooks and MCP servers do not run, or an API key with `--bare`? Without `--bare`, Claude Code still reads the repository's `CLAUDE.md`. That is repository text the model reads, which is untrusted either way, and the fit test records whether the model follows it.

## Amendment (2026-11-21): reviewers read the diff

A dry run of the dev team showed the principal engineer could not review: `vcs.read_change` returned the description but no code, so every review requested changes "for lack of evidence". `vcs.read_change` gains `diff: true`, which adds a bounded diff of the pull request. There is no new operation: grants are unchanged (the operation's grant plus `repo:<alias>`), and so are its confined worker and the host permission (`pull_requests:read`).

- **Shape.** `diff.files` lists the changed files: path, previous path, status, line counts and `binary`. It lists at most 100, within 12 KiB, and `files_truncated` and `files_total` say what was left out. `diff.patches` holds the patches of the files from `diff_offset` on, as the host gives them (hunks only). Each patch is capped at 12 KiB and all of them at 32 KiB. A cut patch ends with `[cardea: patch truncated, N of M bytes shown]` and has `truncated: true`. `next_offset` is the index to read from next, or `null` when every patch has been shown.
- **Bounds.** Sizes are counted as the receipt records them (compact ASCII JSON, escapes included). Whatever the body and reviews take, the whole output stays within the receipt's 64 KiB recorded output, so one call fits a reviewer's context. At most 50 patches are returned per call.
- **Binary and oversized files.** These are listed and never dumped. A patch the host omits is `omitted: "binary"` when it counts no lines, else `omitted: "too_large"`.
- **Data, not instructions.** The body, reviews and patches are untrusted repository text. `diff.note` says so, and credential-shaped strings are replaced with `[redacted]`: the shapes `env.try` scrubs, not key-like assignments, so ordinary code reads unchanged. The team kit's principal engineer is told to read the whole diff, following `next_offset`, before reviewing, and to treat it as the author's data.
- **Tests.** `test_vcs_diff.py` runs against the fake host, whose `pulls/<n>/files` is computed with git. It covers the file list and statuses, binary files, scrubbing, per-file truncation, paging until `next_offset` is `null` within the recorded-output cap, the file-list cap, and host-omitted patches.


## Amendment (2026-11-21): merge commit text and merged branches

- **Commit text.** `vcs.merge` passes the pull request's title with its number, `<title> (#<n>)`, as the squash commit's `commit_title`, and its description as `commit_message`. Before, the host used the branch's commit message, for example "code.session change".
- **Merged agent branches are deleted.** After a successful merge, the head branch is deleted (`DELETE .../git/refs/heads/<branch>`) when it is under the caller's group's agent prefix. Other branches are left alone.
  - Deletion is best effort and part of the merge's own receipt. The output reports `head_deleted`.
  - A refused or failed deletion never fails a merge that already happened.
  - `vcs.find_merge` reports whether the head branch is gone, which shows when an interrupted merge left it behind.

## Amendment (2026-11-21): a host owner may waive the host's branch rules

- **Requirement.** Some host plans offer no rulesets or branch protection. GitHub answers its rules API with 403 "Upgrade to GitHub Pro" for a private repository on a free plan. The owner decided that a team may develop such a repository on Cardea's own guards alone.
- **Decision.** A repository registry entry may set `host_rules: "waived"` with a required `waiver_reason` (8–500 characters). The default is `required`. A waiver is accepted only on a repository `vcs.*` acts on, and a reason without a waiver is refused.
- **What changes.**
  - `vcs.check_repository` treats a 403 from the rules API as `rules_available: false`, not an error. That holds with or without a waiver.
  - With a waiver, it reports `ready: true`, `host_rules: "waived"`, the reason, and the host-side checks that do not hold as `waived_checks` instead of `missing`. Without a waiver, those checks stay `missing` and the repository is not ready.
  - The `vcs.push_branch` and `vcs.merge` receipts carry `host_rules: "waived"` and the reason.
  - The team kit's `status` prints the waiver.
  - Nothing is waived silently.
- **What does not change.** Cardea enforces all of these itself, and each stays mandatory:
  - the agent branch prefix `agent/<group>/` on every push and every pull request head;
  - the content gate on every change and text that leaves the host;
  - every condition of `vcs.merge`: the caller opened the pull request, an allowed pool's `APPROVE` receipt is at the current head, the configured reviewer login's approval of that head is on the host, every check run is green, and the host reports the pull request mergeable.
- **What the waiver gives up.** The host no longer refuses a direct push to the default branch, a force push or a branch deletion made with the repository token. Only Cardea's workers stop those. The token must therefore stay kernel-only: in the vcs workers' credential, never in an agent's environment, a person's hands or a shared file. A leaked token is a direct write to the default branch.
- **Tests** (`apps/api/tests/service/test_vcs_merge.py`):
  - a 403 from the rules API leaves a required repository not ready and a waived one ready, with the reason and the waived checks;
  - a waived repository still refuses a push outside the prefix, and a merge without approval, without green checks, or by someone other than the opener;
  - the merge receipt records the waiver;
  - the registry refuses a waiver without a reason, a reason without a waiver, and a waiver on a repository with no host.
