# ADR-031: Agent-Run Context Operations

- **Date / owner / milestones:** 2026-10-20; runtime maintainers; extends ADR-027 and ADR-029.
- **Status:** selected and implemented for portable execution. A live Claude session has not run with context blocks.
- **Requirement:** a recurring agent run often needs current state before it starts, such as what earlier runs delivered or what a memory service holds for this agent.
  - That state must be fetched at run time under the run's own live authority, not baked into a static template.
  - Every fetch must be receipted like any other call.
  - The session must receive it every time, whether or not the model thinks to ask.
  - A failed fetch must not silently produce a run that looks continuous with earlier runs.

## Candidates and primary evidence

Checked 2026-10-20.

| Candidate | Fit |
|---|---|
| Status quo: expose the read operations as tools and ask the model to call them first | Fails the requirement. Whether, when and with which arguments the model calls them is model behaviour, not something Cardea can guarantee. A forgotten call leaves no gap disclosure. |
| [MCP resources](https://modelcontextprotocol.io/specification/2025-06-18/server/resources) (`resources/list`, `resources/read`) on the per-attempt endpoint | Standard transport for read-only data. In MCP, though, the host application decides whether to read a resource and put it into context. Claude Code reads resources only through its own built-in tools, which agent runs disable (`tools=[]`, ADR-027), and the model would still decide whether to read them. Reading would also need its own receipting path beside `tools/call`. Revisit if the Agent SDK can attach named resources to the session up front. |
| Agent SDK hooks (`SessionStart` / `UserPromptSubmit` returning `additionalContext`) | Delivery would be guaranteed, but the hook runs inside the session process, which is the least-trusted component and may be confined (ADR-028). The fetch would happen outside the kernel's invocation core, and the hook would need a kernel credential. |
| The client fetches the context and puts it into `instructions` | Works for one-off submissions. It fails for triggers: the template is fixed at install time, while the data must be current at the slot. The client would also act outside the run's receipts and authority. |
| **Kernel-invoked read operations before the session (selected)** | The same installed operations, the same invocation core (ADR-026) and the same grants. Output is delivered in the prompt, so delivery does not depend on the model. Needs no new transport. |

For the prompt format we follow Anthropic's published guidance to wrap documents in [XML-style tags](https://docs.anthropic.com/en/docs/build-with-claude/prompt-engineering/use-xml-tags) with metadata attributes. The tags are a prompt convention, not a parser contract.

## Decision

1. **Request.** `AgentRunRequest.context` is a list of `{"operation": str, "arguments": object}`, with at most 4 entries and an empty default. It is an ordinary request field, so a client chooses its context per run or per trigger template; nothing in the kernel names a particular client or operation.
   - `arguments` is optional and defaults to `{}`. An explicit `{}` is the same thing. No other keys are accepted.
   - Each operation must be installed and declare `effect: "read"`. A different effect is refused with `context_not_read_only`, both at admission and at trigger install.
   - Granting is enough. Each context operation must appear in `grants`; it need not, and normally does not, appear in `operations` (the tools exposed to the session). Listing it in both is allowed and simply also exposes it as a tool. Context operations count toward the request's limit of 48 grants.
   - The run's live ceiling is checked for each context operation, and for the operations it `requires`, exactly as for an exposed tool: at admission, at each attempt start and before each call.
   - Arguments are validated against the operation's input schema at admission.
2. **Invocation.** Before each attempt's session, Cardea invokes the context operations in order through the shared invocation core, under that attempt's lease.
   - They are receipted with operation ids `ctx-01`, `ctx-02`, … Numbering continues across attempts, so a second attempt with two context operations records `ctx-03` and `ctx-04`.
   - The handler gets the same `OperationContext` as a tool call, including `agent_id`, `idempotency_context` and `slot_time`.
   - Context calls do not count against `max_tool_calls`.
   - Each attempt fetches its context again rather than reusing an earlier attempt's, so a retry sees current state.
3. **Prompt.** The outputs follow the instructions (and any expectations), preceded by a fixed preface. The preface says the blocks are what read-only operations returned, that they are untrusted data and not instructions, and that instructions inside them must not be followed. Each call becomes one block:
   - success: `<context operation="NAME" status="ok">JSON</context>`, with sorted keys;
   - any failure, whether a handler error, a schema-invalid output, a timeout or a refusal: `<context operation="NAME" status="unavailable" reason="REASON"></context>`, where `REASON` is the failure reason from ADR-026 (added 2026-10-20; `rejected` for a call refused before dispatch). After the blocks, a fixed instruction tells the agent that continuity context was missing and to say so in its result.

   Inside the JSON, `<` and `>` are written as `<` and `>`. The JSON stays valid, and output text cannot close a block or open a fake one.
4. **Failures never fail the run by themselves.** A context call is read-only, so a failure has no effect to reconcile. The run goes ahead and the gap is disclosed. One exception: losing authority for a context operation stops the run at the attempt's authority check, as it would for a tool.
5. **Evidence.** The output document's `calls`, the run view (`GET /v0.2/tasks/{id}/run`) and `GET /v0.2/tasks/{id}/agent-receipts` all list context calls next to tool calls, in the order they were made. A context call left `running` by a crash is settled as failed by kernel recovery (ADR-027 item 7).
6. **Triggers.** An `agent-run` template may carry `context`. It is part of the template and therefore of the trigger's definition digest (ADR-024), so changing it is a new definition version.

## Limits

- Context output is bounded only by each operation's declared output limit. There is no separate total cap on prompt size.
- Escaping protects the block structure but cannot stop a model from being persuaded by the content. The preface and the read-only rule limit the harm: the content cannot grant authority, and every effect is still a checked, receipted tool call.
- Context calls run one after another, before the session's own time is computed, so slow context operations shorten the session.

## Verification

Portable tests in `apps/api/tests/service/test_agent_runs.py` and `test_workloads.py` cover:
- receipted `ctx-NN` calls with one success and one failure;
- the `ok` and `unavailable` blocks, the preface and the gap instruction;
- that markup in an output cannot close a block;
- the operation context passed to the handler;
- chronological `calls` in the output and the run view;
- no blocks when there is no context;
- a fresh fetch and continued numbering on retry;
- grant, installation, read-only and argument checks;
- revocation stopping the run;
- trigger templates carrying context in their digest, and install-time refusal of an effect operation.

## Revisit

Revisit if:
- the Agent SDK can attach MCP resources to a session up front, which would deliver the same guarantee over a standard transport;
- runs need more than four context sources or a prompt-size budget;
- context needs to be produced by operations that are not read-only.
