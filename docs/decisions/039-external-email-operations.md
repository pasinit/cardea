# ADR-039: External Email Operations

- **Date / owner / milestones:** 2026-11-17; runtime and operations-package maintainers; agent teams after milestone 1 (ADR-034, ADR-037).
- **Status:** selected and implemented; portable tests only.
  - **Implemented:**
    - `cardea-email-ops`: `email.send`, its reconciler `email.find_sent` and its reply poller `email.poll_replies`, with two adapters (AgentMail and SMTP/IMAP);
    - the kernel's generic parts: reply polling for installed operations (`replies` in the descriptor binding, `service/replies.py`), the per-role call count `OperationContext.count_calls`, and `OperationContext.pool`;
    - the dev-team example.
  - **Portable tests only:** both adapters were tested against in-process fakes, never against the real services.
  - **Verified on Debian:** nothing. The live smoke step in the [Debian acceptance checklist](../acceptance/debian-adr-034-037.md#adr-039-external-email) has not run.
- **Requirement:** an agent role, for example a product manager, can email a person outside Cardea (the client, or the owner) a short question. The person's reply reaches that role as a message it reads on its next run.
  - **Allow-list.** The model names a recipient alias. Only aliases on the role's allow-list in host configuration can be addressed, and anything else is refused with a typed error.
  - **Content.** The subject and body pass the content gate.
  - **Rate limit.** Each role has its own limit, set in configuration.
  - **Idempotency.** The send is an effect with receipts and an ADR-026 idempotency key.
  - **Inbound.** Replies are polled. There is no public inbound endpoint and no webhook, so a host behind NAT works.
  - **Reply filtering.** Only replies from the allow-listed address that match a thread Cardea created reach the sending role's ADR-034 mailbox, as untrusted data. Everything else is dropped and counted, and is never shown to an agent.
  - **Credentials.** They are host secrets, read inside the operation. They never appear in arguments, receipts, logs or anything an agent sees.
  - **Transport.** The transport is pluggable, and the provider is chosen by configuration.

## Candidates and primary evidence

Checked 2026-11-17.

### Transports

| Candidate | Version / licence / maintenance | Fit |
|---|---|---|
| SMTP submission ([RFC 6409](https://www.rfc-editor.org/rfc/rfc6409)) and IMAP4rev1 ([RFC 3501](https://www.rfc-editor.org/rfc/rfc3501)) with the standard library's `smtplib`, `imaplib` and `email` | CPython 3.12/3.13 stdlib; PSF licence; maintained with CPython | **Selected as the generic adapter.** It works with any provider that offers submission and IMAP, for example a hosted mailbox with an app password. Threads are standard: our `Message-ID` ([RFC 5322 §3.6.4](https://www.rfc-editor.org/rfc/rfc5322#section-3.6.4)) is named by a reply's `In-Reply-To` or `References`, and IMAP `SEARCH HEADER` finds exactly those messages. Gaps: SMTP does not deduplicate a resubmission (the reconciler reads the sent folder instead), and IMAP carries no sender-authentication verdict of its own. The verdict comes from the receiving server's RFC 8601 `Authentication-Results` header ([RFC 8601](https://www.rfc-editor.org/rfc/rfc8601)), trusted only under a configured authserv-id; see decision 5. |
| [AgentMail](https://docs.agentmail.to/api-reference) HTTP API, `https://api.agentmail.to/v0` | Hosted service. Pricing per the [rate-limits page](https://docs.agentmail.to/knowledge-base/rate-limits): Free plan 3 inboxes, 100 emails a day, no monthly cap (about 3,000 a month, the owner's figure), no custom domains, no card | **Selected as the first host's adapter**, because the agent team gets its own inbox rather than the owner's mailbox. Fit from its documentation: [Send Message](https://docs.agentmail.to/api-reference/inboxes/messages/send) returns `message_id` and `thread_id`. [Idempotent Requests](https://docs.agentmail.to/idempotency): an `Idempotency-Key` header makes a retry within 24 hours return the first send and send nothing; the same key with a different request is `409`. [Get Thread](https://docs.agentmail.to/api-reference/inboxes/threads/get) lists a thread's messages with `from`, `labels`, `extracted_text` (the new text without the quoted history) and authentication verdicts. Received mail that fails authentication but is still delivered is labelled `unauthenticated` ([messages guide](https://docs.agentmail.to/messages)). [List Messages](https://docs.agentmail.to/api-reference/inboxes/messages/list) filters by label. Webhooks and WebSockets exist but are not needed: the rate-limit page allows slow polling ("one list call every few seconds per inbox is plenty"). Vendor risk: see Decision. |
| JMAP ([RFC 8620](https://www.rfc-editor.org/rfc/rfc8620), [RFC 8621](https://www.rfc-editor.org/rfc/rfc8621)) | Standard; few providers offer it | Not selected now: neither the first host's provider nor common hosted mailboxes expose it. It is a natural third adapter behind the same interface. |
| Provider inbound webhooks or an inbound SMTP gateway | Various | Rejected: they need a public endpoint, and the host is behind NAT. |

### Client libraries

| Candidate | Version / licence / maintenance | Fit |
|---|---|---|
| Official AgentMail Python SDK, [`agentmail`](https://pypi.org/project/agentmail/) ([source](https://github.com/agentmail-to/agentmail-python)) | 2.0.25, released 2026-11-06; MIT; 155 releases, several a week (generated) | Not used. It requires `anyio>=4.14.2`, and the kernel's lock pins 4.13.0. It also pulls in `websockets` and its own `pydantic` models. The weekly release rate makes a pin go stale fast. The adapter needs three endpoints, which a thin client covers. |
| [`httpx`](https://www.python-httpx.org/) | 0.28.1, already pinned in the kernel; BSD-3-Clause | **Selected** for the AgentMail adapter: a thin client over the three endpoints above. |
| `aiosmtplib`, `imapclient` / `aioimaplib` | Maintained; MIT/BSD | Not needed. The stdlib clients do submission, STARTTLS/TLS, login, `EXAMINE`, `UID SEARCH` and `UID FETCH`, and run in a worker thread. |
| [`aiosmtpd`](https://pypi.org/project/aiosmtpd/) (test server) | 1.4.6, released 2024-05-18; Apache-2.0 | Not used. The tests need a few SMTP verbs and a matching IMAP fake, which has no maintained Python server library. Both fakes are small socket servers in `cardea_email_ops.fake`. |

### Kernel mechanisms

| Need | Candidate | Fit |
|---|---|---|
| Deliver a reply to a role | ADR-034 mailboxes (`mailbox.send`, `mailbox.inbox`) | **Reused.** A reply becomes kernel mail to the agent that sent the email. `mailbox.inbox` already renders mail as an untrusted ADR-031 context block that markup cannot close. Its `client_key` makes delivery idempotent. Its `cardea.mailbox.sent` event wakes the recipient through its existing journal trigger. |
| Find the thread of a send | The send's receipt (ADR-023) | **Reused.** The thread id is in the receipt's output. Outstanding threads are the operation's succeeded receipts in the reply window. No new table. |
| Count dropped replies without their content | The kernel journal (`events`) | **Reused.** One `operation.reply_dropped` row per dropped message, holding the operation, the receipt, a reason and a SHA-256 of the provider's id. Journal triggers expose only the `cardea.*` types, so no agent sees it. |
| Periodic polling | DBOS scheduled workflows; ADR-024 time triggers | Not used. A trigger admits agent or operation tasks, not a kernel-side read that writes mail. The kernel already runs such loops as service threads (reconciliation every 30 s). The poller is one more thread, which calls only the declared poller and keeps no state of its own. |
| Rate limit per role | Group spend accounts (ADR-034 slice 9); `expect.max_calls` (ADR-029) | Neither fits. Spend counts money, and `max_calls` bounds one run. The kernel gives in-process handlers a read-only count of their role's recent calls (`count_calls`). The limit itself stays in the operation's host configuration. |

## Decision

1. **`email.send`** is an installed operation in `packages/cardea-email-ops`: in-process (ADR-023), `effect`, `idempotency_scope` `["/to", "/client_key"]`, reconciler `email.find_sent`.
   - Arguments: `to` (an alias, `^[a-z][a-z0-9-]{0,63}$`), `subject` (one line, at most 200), `body` (at most 8,000) and `client_key`.
   - Output: `to` (the alias), `thread_id`, `message_id` and `sent_at`. No address.
   - Checks, in order, before anything is sent:
     - The caller's role (`<group>.<pool>`, from the kernel's member table) is listed in the host configuration, and the alias is one of that role's recipients. Otherwise `recipient_not_allowed`.
     - The role's calls in its window, this one included, are within `rate_limit`. Otherwise `rate_limited`. A call without the kernel's counter is refused with `rate_limit_unavailable`.
     - The subject and body pass the content gate, which fails closed.
     - The credential is readable. Otherwise `credential_unavailable`.
   - A refusal from the provider before sending (for example HTTP 400/401/403/404/413/422/429, a refused SMTP recipient, or no connection) settles as failed with nothing sent. Any other error leaves the call `outcome_unknown`.
2. **Thread identity.**
   - SMTP: the Message-ID `<cardea.<effect key>@<message_id_domain>>`, which is also the thread id.
   - AgentMail: its own `thread_id`. The send also carries `Idempotency-Key: <effect key>` and the label `cardea-<first 40 hex of the effect key>`.
3. **Reconciliation** reads and never sends.
   - AgentMail: list messages carrying the send's label.
   - SMTP/IMAP: search the configured `sent_folder` for the Message-ID. Without a `sent_folder`, the call stays uncertain.
   - After a "not applied" reconciliation, the retry reuses the same effect key. Within 24 hours, AgentMail's idempotency then returns the earlier send if the label lookup missed it.
4. **Replies (kernel, generic).** An installed in-process effect may declare `replies: {poller, interval_seconds (60–86400, default 300), window_days (1–90, default 14)}`.
   - A kernel service thread wakes every 30 s. For each such operation that is due, it calls the poller with each succeeded, non-deduplicated call in the window: its receipt, arguments, context and output.
   - The poller returns `Polled(replies, dropped)`.
   - The kernel delivers each accepted reply once into the calling agent's mailbox, sent by the kernel's runtime, with the mailbox `client_key` `reply.<digest of receipt and reply id>` and a `reply_to` data part naming the operation and the receipt.
   - It records each dropped reply once as `operation.reply_dropped`.
   - A poller error is logged with its class name only and changes nothing.
5. **`email.poll_replies`** asks the adapter for the messages on the thread and skips our own.
   - It accepts a message only from the current address of the alias the send went to (compared exactly, lower-case), and never one marked `spam`.
   - **It fails closed on authentication** (amended 2026-11-17 in review). The From header alone proves nothing, because anyone can forge it. A reply is accepted only when its sender is authenticated: a DMARC pass, or failing that an SPF or DKIM pass for a domain aligned with the From domain. Alignment is relaxed alignment (RFC 7489 §3.1), approximated without the public suffix list: the domains must be equal, or one must be a subdomain of the other. That is stricter, never looser. Anything else is dropped as `unauthenticated` (a verdict says it is not authenticated) or `authentication_unknown` (no verdict). Code: `cardea_email_ops/authentication.py`.
     - **IMAP:** the verdict comes only from the topmost `Authentication-Results` header, and only when its authserv-id equals the configured `imap.authserv_id` (for example `mx.google.com` for Gmail). A receiving server prepends its own header, so a header a sender planted sits below it and is ignored, even when it claims the same authserv-id. With no such header, or no `authserv_id` configured, the verdict is unknown.
     - **AgentMail:** the `unauthenticated` label means not authenticated. Its absence does not mean authenticated. AgentMail's [Get Message](https://docs.agentmail.to/api-reference/inboxes/messages/get) documentation guarantees four things. The label is AgentMail's acceptance decision and is looser than alignment: a message whose only pass is an unaligned envelope SPF is delivered without it. `authentication_results.spf` appears only when the envelope domain is aligned with From. `dkim_domain` must be compared with the From domain. The field is absent on mail received before 2026-09-09, or when a verdict could not be computed. So a reply is accepted only with an aligned pass in `authentication_results`, checked the same way as for IMAP.
     - **Opt-out:** `accept_unauthenticated: true`, per transport in the host configuration, accepts replies without an authenticated sender. It is for servers that write no verdicts. It **weakens the check** to the From header alone. It defaults to false, and the sender-address check still applies.
   - The reply text is shortened to `max_reply_bytes` (default 8 KiB). With AgentMail it is the extracted new text.
   - The agent sees the alias, the subject, the received time and the thread id, never the address.
6. **Narrow reading by construction.**
   - AgentMail: one `GET` of the thread Cardea created, and nothing else in the inbox.
   - IMAP: `EXAMINE` (read-only) of the configured folder, `UID SEARCH HEADER In-Reply-To <id>` and `HEADER References <id>`, then `UID FETCH (BODY.PEEK[])` of only those UIDs. It never runs `STORE`, `COPY`, `MOVE` or `EXPUNGE`, never fetches unmatched mail and keeps no copy of it.
   - **A mailbox password or app password (for example Gmail's) grants the whole mailbox.** This narrowing in the operation is the only guard. The provider enforces none of it. A dedicated inbox, which is what AgentMail gives the first host, limits what a leaked credential exposes.
7. **Configuration.** `CARDEA_EMAIL_CONFIG` (default `/etc/cardea/email.json`) is a private file: `transport` (`agentmail` with `inbox_id`, `api_url` and `api_key_file`; or `smtp_imap` with `from_address`, `message_id_domain`, `smtp` and `imap` endpoints and their `password_file`), `recipients` (alias → address), `roles` (role → `recipients` and `rate_limit {count, window_days}`), and `max_reply_bytes`.
   - It holds no secret: keys and passwords are separate private files, read for each call.
   - Plain-text `security: none` is allowed only on loopback.
   - Real addresses live only in this host file.
8. **Grants.** `email.send` is granted per role in the team definition (pool `grants` and `run.operations`), like any installed operation. Which aliases a role may address is the host's decision, in the configuration above.
9. **Vendor choice.** AgentMail is the first host's adapter. It is a young hosted service whose plan limits, API and labels may change. The SMTP/IMAP adapter selects any standard mailbox instead, and only `transport.kind` changes. Nothing outside the adapter depends on AgentMail.

## Integration

- **Authority.** The kernel's checks are unchanged: `email.send` must be in the run's grants and the live ceiling. The package adds the role allow-list and the rate limit as definite refusals. A reply grants nothing. It is mail from the kernel's runtime to an agent of this kernel, like a dead-letter notice.
- **State.** No migration.
  - Threads are the receipts.
  - Delivered replies are mailbox messages, deduplicated by `client_key`.
  - Drops are journal rows, deduplicated per receipt by the reply digest. The lookup uses the `events_by_task` index.
  - `count_calls` and the thread lookup scan `operations` by `json_extract` of the operation name. That is fine for a small host. An index is a revisit item.
- **Untrusted content.** Reply text reaches an agent only through `mailbox.inbox`, the same untrusted block as all mail (ADR-031, ADR-034 decision 4). It is not run through the content gate: it is inbound, and the gate checks what leaves the host.
- **Credentials.** The key and passwords are read inside the adapter for each call. They never appear in arguments, outputs, receipts, the journal, agent prompts or the service log (every `cardea.*` record). The adapters' exceptions carry only an HTTP status, an SMTP reply code or an exception class, never a URL or the provider's text. `test_credentials_and_addresses_never_leave_the_adapter` greps every table, the captured log, the prompts and the environment for them, as `test_vcs_confined.py` does for the repository token. Addresses are kept out of the same places.
- **In-process, not confined.** The handler runs in the service process, so the key is in that process's memory during a call, as for any in-process operation (ADR-023). The reply poller must write mailbox rows, which a confined worker cannot. Moving the send into a confined worker with a profile credential (ADR-028) is a revisit item.
- **Dependencies.** None new for the kernel. `cardea-email-ops` uses the stdlib and the pinned `httpx`. It is a dev dependency of `apps/api` (for tests), like the other operation packages.
- **Example.** In `examples/teams/dev-team`, only the `product` pool is granted `email.send`, with `mailbox.inbox` context. `dev-team/email.json` names the placeholder alias `client` (`client@example.invalid`) and a limit of 3 per 7 days. The product manager owns feature design: features and bugs go straight to the backlog. Its instructions say to keep client emails few and short, with concrete options, never to ask the client to design or specify, and never to wait for a reply. The rehearsal sends one question, a fake client and a stranger reply, and the PM's next run reads only the client's reply.

## Verification

`apps/api/tests/service/test_email_ops.py`, 42 tests, all against fakes:
- a send reaches the configured address once, and a reworded retry with the same `client_key` is deduplicated;
- the idempotency key is the effect key;
- aliases outside the role, unknown aliases, an unconfigured role and a raw address are refused;
- the rate limit refuses the third send of a role limited to two, including in a later run;
- the content gate refuses before anything is sent;
- a provider 403 is a definite failure;
- a 503 leaves the call `outcome_unknown`, the kernel's reconciler settles it as not applied, and the retry then sends once;
- the reconciler finds a send by its label;
- an allow-listed reply reaches the sender's next run as an untrusted block that hostile markup cannot close, exactly once across polls;
- replies from another sender, labelled `unauthenticated` or `spam`, without verdicts, or whose only pass is for an unaligned domain or a DMARC failure, are dropped, counted once and kept nowhere, and their journal row is invisible to triggers;
- AgentMail verdicts are `True` only for an aligned pass, never for the absence of the label alone;
- the opt-out accepts unauthenticated replies and still drops other senders;
- IMAP: a spoofed From with no `Authentication-Results` is dropped; a forged header naming another authserv-id is dropped; a forged pass with the right authserv-id below the server's failing header is dropped; DMARC pass, and SPF pass aligned to a subdomain, from the configured authserv-id are accepted; an unaligned DKIM pass is dropped; the opt-out is honoured and off by default;
- mail on other threads is never requested;
- a failing provider changes nothing;
- the grep test for the key and the addresses;
- the descriptor rules: replies only on in-process effects, and the poller must be installed;
- SMTP: the minted Message-ID, and a refused recipient raises `Refused`;
- IMAP: only matching replies (by `In-Reply-To` and by `References`) are fetched, under `EXAMINE`, with only `UID SEARCH` and `UID FETCH`;
- the reconciler via the sent folder, or uncertain without one;
- plain text allowed on loopback only.

`examples/teams/test_dev_team.py` checks the rehearsal and that only `product` holds `email.send`. On macOS with Python 3.13, `make lint`, `make typecheck` and `make test` pass.

Not verified:
- **AgentMail itself.** Its documented behaviours (24-hour idempotency, label filtering on list, `extracted_text`, the `unauthenticated` and `spam` labels, and the `authentication_results` field and its keys) are taken from its documentation. Only the fake exercises them.
- **A real SMTP or IMAP server**, including STARTTLS and TLS, and a real receiving server's `Authentication-Results` (its authserv-id, and whether it removes a planted header that claims its id, as RFC 8601 §5 recommends).
- **The daemon's poller thread** under systemd.

`experiments/debian-acceptance/email_live.py` is the live smoke step. It has not run.

## Revisit

Revisit if:
- AgentMail changes its plans, idempotency window or labels, or stops being suitable; switch `transport.kind`;
- a provider offers JMAP or authoritative sender verdicts over IMAP;
- receipts grow enough that `count_calls` and the thread lookup need an index on the operation name;
- the send should move into a confined worker, which needs a confined way to deliver replies;
- several roles share one thread, or replies need to reach a pool rather than the sending member.

## Amendment (2026-11-21): inbound mail from the client

- **Requirement (owner).** The client must be able to email a team's inbox with a new message, not only reply to a thread a role started. That mail reaches one configured role as data and wakes it like any mail. The checks stay as strict as for replies:
  - only the host's listed aliases may write;
  - the sender must be authenticated, failing closed;
  - each message is delivered once;
  - size and rate are bounded;
  - every rejection is recorded without its content.
- **Standards and existing mechanisms checked (2026-11-21).**

  | Need | Candidate | Fit |
  |---|---|---|
  | Find new mail | IMAP4rev1 `UID SEARCH HEADER From` ([RFC 3501 §6.4.4](https://www.rfc-editor.org/rfc/rfc3501#section-6.4.4)) under `EXAMINE`; AgentMail [List Messages](https://docs.agentmail.to/api-reference/inboxes/messages/list) by the `received` label with `after`, and [Get Message](https://docs.agentmail.to/api-reference/inboxes/messages/get) | **Reused**, with the same clients and the same read-only narrowing. With IMAP, only mail whose From names a configured address is fetched. With AgentMail, only such mail is opened in full; any other sender's message is seen as list metadata only, which is enough to reject it. |
  | Tell new mail from replies | `In-Reply-To` / `References` ([RFC 5322 §3.6.4](https://www.rfc-editor.org/rfc/rfc5322#section-3.6.4)); AgentMail `thread_id` | **Reused.** A message on a thread Cardea started (a recent send's thread id or Message-ID, or a reference to a Cardea Message-ID) belongs to `poll_replies` and is skipped. |
  | Sender authentication | The existing RFC 8601 / AgentMail verdicts (decision 5) | **Reused unchanged:** an aligned pass is required, and an unknown verdict is a rejection unless `accept_unauthenticated` is set. |
  | Exactly-once delivery | The Message-ID ([RFC 5322 §3.6.4](https://www.rfc-editor.org/rfc/rfc5322#section-3.6.4)) as the key, ADR-034 mailbox `client_key` | **Reused.** The delivery key is derived from the operation and the provider's message id. |
  | Deliver and wake | ADR-034 pool mail and its `cardea.mailbox.sent` journal event | **Reused.** A pool address wakes the pool through its existing mail trigger. |
  | Push delivery | AgentMail webhooks and WebSockets; IMAP IDLE ([RFC 2177](https://www.rfc-editor.org/rfc/rfc2177)) | Not used. Webhooks need a public endpoint. IDLE and WebSockets need a long-lived connection per inbox, and the existing 5-minute poll is enough for a client's mail. |

  The only new mechanism is the kernel's inbound pass: one more call on the reply poller's thread, with no new table.
- **Decision.**
  1. **Host opt-in.** `email.json` gains an optional `inbound` section, off by default:

     ```json
     "inbound": {"from": ["<alias>"], "deliver_to": {"namespace": "<ns>", "pool": "<group>.<pool>"},
                 "rate_limit": {"count": 10, "window_days": 1}, "max_bytes": 8192, "window_days": 7}
     ```

     The aliases must be in `recipients`. One section serves the transport's inbox.
  2. **Poller.** `email.send`'s `replies` names a second entry point, `inbound: "email.poll_inbound"`. The kernel calls it once per reply interval with the recent send threads. It returns a `replies.Inbox`, or None when `inbound` is not configured. For each new message within `window_days` that is on none of those threads, it applies these checks:
     - the exact sender address must belong to a listed alias, else `sender_not_allowed`;
     - the sender must be authenticated, else `unauthenticated` or `authentication_unknown`;
     - the message must not be spam, else `spam`;
     - its text must be at most `max_bytes`, else `too_large`. It is rejected, not shortened, because a cut-off request from a client is worse than none.
  3. **Delivery (kernel).** Each accepted message goes to the configured pool once, keyed by the Message-ID. It is sent by the kernel's runtime and carries these parts:
     - its text;
     - `{"email_inbound": {from: <alias>, subject, received_at, thread_id}}`;
     - `{"inbound": {operation, from_outside: true}}`.

     The mailbox renders it as an untrusted context block, like any mail. It is never instructions to the kernel or the role.
     - Its receipt is the content-free journal event `operation.inbound_delivered`, holding the operation, the message-id digest, the mailbox message id and the pool.
     - The host's `rate_limit` bounds deliveries per window. A message past it is rejected with `rate_limited`.
  4. **Rejections.** Each rejected message is recorded once, as `operation.inbound_dropped` with the operation, the reason, the sender's domain and the message-id digest. It is never delivered later. Journal triggers see neither inbound event, and the logs carry only counts.
  5. **Replying.** The role answers with `email.send` to the alias, as today. That starts a new thread whose replies are polled as before. Replying inside the client's own thread (AgentMail's reply endpoint, `In-Reply-To` over SMTP) is not part of this change.
- **Compatibility.** `replies.inbound` is optional. A descriptor without it keeps its digest, and `email.send`'s digest changes because it now names the poller. Hosts without `inbound` see no change: no inbox listing and no extra reads.
- **Tests** (`apps/api/tests/service/test_email_ops.py`, both fake transports):
  - accepted mail reaches the pool once and arrives as untrusted data from the alias, and a later `email.send` works;
  - the rejections: wrong sender (never opened or fetched), unauthenticated, unknown verdict, oversize, and rate-limited. Each is recorded once with only the domain and the reason, and its content is stored nowhere;
  - a duplicate Message-ID is delivered once;
  - a reply on a Cardea thread is delivered as a reply, not as inbound mail;
  - inbound is off by default;
  - configuration validation;
  - `test_team_kit`: the PM fragment is opt-in.

## Amendment (2026-11-23): attachments on `email.send`

`email.send` takes an optional `attachments` list (1 to 5 items of `{filename, content_type, data}`, `data` being base64). The bytes are in the call's arguments, so the receipt, the idempotency scope and the content gate see exactly what is sent. No object store, credential or path is involved.

- **Types.** `image/png` (the bytes must start with the PNG signature and an IHDR chunk) and `text/html` (UTF-8 text without control characters, and self-contained: no script, frame, object, embed, link, base or form element, and no `src`, `href`, `url()` or `@import` except a `data:` URI or an in-page `#` anchor). The reference check is a conservative screen, not a sanitizer. The filename must be a plain name ending `.png` or `.html` to match the type, and unique in the call.
- **Limits.** Host file `attachments {max_count (default 3, at most 5), max_file_bytes (default 24576), max_total_bytes (default 32768)}`; the byte ceilings are 32 KiB so the base64 stays within a receipt's 64 KiB document. Refusals are definite, before anything is sent, and do not count against the role's rate limit: `attachments_too_many`, `attachment_too_large`, `attachments_too_large`, `attachment_invalid`, `attachment_type_mismatch`, `attachment_external_reference`.
- **Idempotency.** The scope is now `/to`, `/client_key` and, when present, `/attachments` (the descriptor's new `idempotency_optional`). The same key with the same attachments is one effect; other attachments, or none, are a different effect. A call without attachments keeps the key it had. The rate limit is unchanged: one call is one send, with or without attachments.
- **Content gate.** HTML attachments are passed to the gate with the subject and body. A PNG has no text to read, so only its type and size are checked.
- **Transports.** AgentMail sends `attachments[{filename, content_type, content}]`; SMTP builds a MIME multipart message.
