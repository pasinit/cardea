"use strict";

// One member's page: what it is doing, may do, remembers and has done. Everything from the API is
// untrusted text: it is only ever put in with textContent, never as markup and never as a link.
const params = new URLSearchParams(location.search);
const team = params.get("team") || "";
const member = params.get("member") || "";
const REFRESH_MS = 15000;
const CALLS = 20;

let detail = null;
let calls = [];
let callsNext = null;
let callsTotal = 0;
let callsTask = null;
let live = true;
let failuresOnly = false;
let timer = null;

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}

function note(text) {
  return el("p", text, "muted");
}

function failure(text, retry) {
  const box = el("div");
  box.append(el("p", text, "bad"));
  if (retry) {
    const again = el("button", "Try again");
    again.addEventListener("click", retry);
    box.append(again);
  }
  return box;
}

function when(iso) {
  const date = new Date(iso);
  if (!iso || Number.isNaN(date.getTime())) return el("span", "-");
  const node = el("time", date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }));
  node.dateTime = iso;
  return node;
}

function day(iso) {
  const date = new Date(iso);
  if (!iso || Number.isNaN(date.getTime())) return el("span", "-");
  const today = new Date().toDateString() === date.toDateString();
  const clock = date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const node = el("time", today ? `Today ${clock}` : `${date.toLocaleDateString()} ${clock}`);
  node.dateTime = iso;
  return node;
}

function seconds(from, to) {
  const span = (new Date(to) - new Date(from)) / 1000;
  if (!Number.isFinite(span) || span < 0) return "-";
  if (span < 1) return "under 1 s";
  if (span < 60) return `${Math.round(span)} s`;
  if (span < 3600) return `${Math.round(span / 60)} min`;
  return `${(span / 3600).toFixed(1)} h`;
}

function money(usd) {
  return usd === null || usd === undefined ? "unknown" : `$${Number(usd).toFixed(2)}`;
}

function kb(bytes) {
  return bytes < 1024 ? `${bytes} B` : `${(bytes / 1024).toFixed(bytes < 10240 ? 1 : 0)} KB`;
}

async function get(path) {
  const response = await fetch(path, { cache: "no-store" });
  const body = await response.json();
  if (!response.ok) throw new Error(body.code || "unavailable");
  return body;
}

function table(head, rows) {
  const wrap = el("div", null, "tablewrap");
  const t = el("table");
  const tr = el("tr");
  head.forEach((h) => tr.append(el("th", h)));
  t.append(tr);
  rows.forEach((row) => {
    const r = el("tr");
    row.forEach((cell) => {
      const td = el("td");
      td.append(cell instanceof Node ? cell : document.createTextNode(cell === null || cell === undefined ? "-" : String(cell)));
      r.append(td);
    });
    t.append(r);
  });
  wrap.append(t);
  return wrap;
}

// The header: who, in what state, and what the run in progress is.
function stateOf(d) {
  if (d.disabled) return "paused";
  return d.active_task_id ? "running" : "idle";
}

function header() {
  const d = detail;
  document.title = `${d.member} - team member`;
  const title = document.getElementById("title");
  const state = stateOf(d);
  title.replaceChildren(document.createTextNode(`${d.member} `), el("span", d.pool, "muted"), el("span", state, `state state-${state}`));
  const summary = document.getElementById("summary");
  const run = d.runs.find((r) => r.task_id === d.active_task_id);
  const claims = d.claims.length ? ` · holds ${d.claims.join(", ")}` : "";
  if (run) {
    summary.replaceChildren(
      document.createTextNode(`Running since `),
      when(run.created_at),
      document.createTextNode(` (${seconds(run.created_at, new Date().toISOString())}) · ${money(run.cost_usd)} this run${claims} · `),
      el("span", live ? "● Live" : "Updates paused", live ? "live" : "muted"),
    );
  } else {
    const last = d.runs[0];
    const text = d.disabled ? "This member is disabled; its runs are refused." : last ? "Not running. Last run ended " : "Not running. It has not run yet.";
    summary.replaceChildren(document.createTextNode(text));
    if (last && !d.disabled) summary.append(when(last.updated_at), document.createTextNode(claims));
  }
}

const OUTCOMES = { succeeded: "Finished", failed: "Failed", cancelled: "Cancelled" };

function outcome(run) {
  const word = OUTCOMES[run.outcome] || "Running";
  return run.failure_code ? `${word}: ${run.failure_code}` : word;
}

function history() {
  const target = document.getElementById("hi-body");
  const runs = detail.runs;
  if (!runs.length) return target.replaceChildren(note("This member has not run yet."));
  const rows = runs.map((r) => {
    const link = el("button", r.task_id === callsTask ? "Showing" : "Show calls");
    link.addEventListener("click", () => chooseRun(r.task_id));
    return [day(r.created_at), r.outcome ? seconds(r.created_at, r.updated_at) : "running", money(r.cost_usd), outcome(r), link];
  });
  target.replaceChildren(table(["Started", "Length", "Cost", "Outcome", "Calls"], rows));
  if (detail.runs_next) target.append(note("Older runs are not shown."));
}

// What the grants mean in plain words; a grant not listed here is shown as the kernel names it.
function describe(operation) {
  const colon = operation.indexOf(":");
  const kind = colon < 0 ? operation : operation.slice(0, colon);
  const scope = colon < 0 ? "" : operation.slice(colon + 1);
  const words = {
    mail: ["Send mail", "to"],
    repo: ["Work in the repository", ""],
    record: ["Change work items", "in"],
    read: ["Read", ""],
    env: ["Run tests", "in"],
    vcs: ["Change pull requests", "in"],
  };
  const w = words[kind];
  if (!w) return [`Other permission: ${operation}`, "Everything it covers"];
  return [w[0], scope ? (w[1] ? `${w[1]} ${scope}` : scope) : "Everything"];
}

function capabilities() {
  const target = document.getElementById("cap-body");
  const list = detail.capabilities;
  if (!list.length) return target.replaceChildren(note("This member holds no grants."));
  const rows = list.map((c) => {
    const [action, scope] = describe(c.operation);
    const allowed = c.mode === "allow";
    return [action, scope, el("span", allowed ? "✓ Allowed" : "✗ Not allowed", allowed ? "ok" : "no")];
  });
  target.replaceChildren(table(["Action", "Scope", "Status"], rows));
  if (detail.capabilities_truncated) target.append(note(`${detail.capabilities_truncated} more not shown.`));
  target.append(note("Read-only here. The kernel checks every call against these grants as it happens."));
}

function memory() {
  const target = document.getElementById("mem-body");
  const m = detail.memory;
  if (m.version === null) return target.replaceChildren(note("Nothing saved yet."));
  const share = Math.min(100, Math.round((m.archive_bytes / m.limit_bytes) * 100));
  const meter = el("div", null, share >= 80 ? "meter warn" : "meter");
  meter.setAttribute("role", "img");
  meter.setAttribute("aria-label", `${share} percent of the memory limit`);
  const bar = el("i");
  bar.style.width = `${share}%`;
  meter.append(bar);
  target.replaceChildren(el("p", `Version ${m.version} · ${m.files.length} files · ${kb(m.archive_bytes)} of ${kb(m.limit_bytes)}`), meter);
  m.files.forEach((f) => {
    const box = el("details");
    const sum = el("summary", f.name);
    sum.append(el("span", `${kb(f.bytes)} · changed ${new Date(f.changed_at).toLocaleString()}`, "muted"));
    box.append(sum, el("pre", f.truncated ? `${f.text}\n… (shortened)` : f.text));
    target.append(box);
  });
}

// One tool call of the run: what it was, how it ended, and (tapped open) its text.
function callRow(c) {
  const ok = c.state === "succeeded";
  const bad = c.state === "failed" || c.failure_code;
  const box = el("details");
  const sum = el("summary");
  sum.append(when(c.created_at), el("span", c.tool || c.operation_id, "tool"));
  const status = bad ? "✗ refused or failed" : ok ? "✓ done" : c.state || "running";
  sum.append(el("span", `${status} · ${seconds(c.created_at, c.updated_at)}`, bad ? "no" : ok ? "ok" : "muted"));
  const lines = [];
  if (c.arguments) lines.push(`Asked: ${c.arguments}`);
  if (c.failure_code) lines.push(`Failed: ${c.failure_code}${c.failure_message ? `: ${c.failure_message}` : ""}`);
  else if (c.result) lines.push(`Result: ${c.result}`);
  box.append(sum, el("pre", lines.join("\n") || "No details."));
  return box;
}

function timeline() {
  const target = document.getElementById("tl-body");
  const controls = document.getElementById("tl-controls");
  const run = detail.runs.find((r) => r.task_id === callsTask);
  const liveRun = run && !run.outcome;
  const liveButton = el("button", live ? "Pause updates" : "Resume updates");
  liveButton.setAttribute("aria-pressed", String(!live));
  liveButton.addEventListener("click", () => {
    live = !live;
    schedule();
    header();
    timeline();
  });
  const fails = el("button", "Show only failures");
  fails.setAttribute("aria-pressed", String(failuresOnly));
  fails.addEventListener("click", () => {
    failuresOnly = !failuresOnly;
    timeline();
  });
  controls.replaceChildren(el("span", liveRun && live ? "● Live " : "", "live"), liveButton, fails);
  const shown = failuresOnly ? calls.filter((c) => c.state === "failed" || c.failure_code) : calls;
  if (!callsTask) return target.replaceChildren(note("No tool calls yet: this member has not run."));
  const list = el("ul", null, "tl");
  if (liveRun && live) list.setAttribute("aria-live", "polite");
  shown.forEach((c) => {
    const item = el("li");
    item.append(callRow(c));
    list.append(item);
  });
  target.replaceChildren(list);
  if (!shown.length) target.append(note(failuresOnly ? "No failed calls among those shown." : "No tool calls yet in this run."));
  if (callsNext) {
    const more = el("button", "Load earlier calls");
    more.addEventListener("click", () => loadCalls(callsNext, false));
    target.append(more);
  }
  target.append(note(`${calls.length} of ${callsTotal} shown for this run`));
}

async function loadCalls(after, fresh) {
  if (!callsTask) return timeline();
  const task = callsTask;
  const query = new URLSearchParams({ team, task, limit: String(CALLS) });
  if (after) query.set("after", after);
  try {
    const page = await get(`/api/calls?${query}`);
    if (task !== callsTask) return;
    callsTotal = page.total;
    if (fresh) {
      // A refresh brings the newest calls and keeps the older ones already loaded.
      const known = new Set(calls.map((c) => c.operation_id));
      const first = !calls.length;
      calls = [...page.items.filter((c) => !known.has(c.operation_id)), ...calls];
      if (first) callsNext = page.next;
    } else {
      calls = [...calls, ...page.items];
      callsNext = page.next;
    }
    timeline();
  } catch (error) {
    document.getElementById("tl-body").replaceChildren(failure("Could not load the timeline. Try again in a moment.", () => loadCalls(after, fresh)));
  }
}

function chooseRun(task) {
  callsTask = task;
  calls = [];
  callsNext = null;
  callsTotal = 0;
  history();
  loadCalls("", false);
}

function message(m) {
  const box = el("details");
  const who = (p) => p.role || p.address;
  const first = (m.parts.find((p) => typeof p.text === "string") || { text: "" }).text;
  const sum = el("summary");
  const time = el("span", new Date(m.sent_at).toLocaleString([], { hour: "2-digit", minute: "2-digit", month: "short", day: "numeric" }), "head");
  sum.append(time, el("span", `${who(m.from)} → ${who(m.to)}`, "head"), el("span", first.slice(0, 80)));
  box.append(sum);
  m.parts.forEach((part) => {
    const text = typeof part.text === "string" ? part.text : JSON.stringify(part.data === undefined ? part : part.data, null, 2);
    box.append(el("pre", text));
  });
  return box;
}

async function mail(after, list, more) {
  const query = new URLSearchParams({ team, member, limit: "10" });
  if (after) query.set("after", after);
  try {
    const page = await get(`/api/mail?${query}`);
    list.append(...page.items.map(message));
    if (!list.children.length) list.append(note("No messages."));
    more.replaceChildren();
    if (page.next) {
      const next = el("button", "Load older");
      next.addEventListener("click", () => mail(page.next, list, more));
      more.append(next);
    }
    more.append(note(`${page.total} matching`));
  } catch (error) {
    more.replaceChildren(failure("Could not load the mail. Try again in a moment.", () => mail(after, list, more)));
  }
}

function mailView() {
  const list = el("div");
  const more = el("div");
  document.getElementById("mail-body").replaceChildren(list, more);
  mail("", list, more);
}

function draw() {
  header();
  history();
  capabilities();
  memory();
}

async function load(first) {
  try {
    detail = await get(`/api/member?${new URLSearchParams({ team, member })}`);
  } catch (error) {
    const text = error.message === "member_not_found" ? "This member is not in the team." : "Could not load this member. Try again in a moment.";
    document.getElementById("summary").replaceChildren(failure(text, () => load(true)));
    if (error.message === "member_not_found") {
      document.body.classList.add("gone");
      document.title = "Member not found";
      document.getElementById("title").textContent = "Member not found";
      clearInterval(timer);
    }
    return;
  }
  const keep = detail.runs.some((r) => r.task_id === callsTask);
  draw();
  if (first || !keep) {
    callsTask = detail.active_task_id || (detail.runs[0] && detail.runs[0].task_id) || null;
    calls = [];
    callsNext = null;
    history();
    await loadCalls("", false);
  } else {
    await loadCalls("", true);
  }
  if (first) mailView();
}

function schedule() {
  clearInterval(timer);
  if (live) timer = setInterval(() => load(false), REFRESH_MS);
}

if (!team || !member) {
  document.body.classList.add("gone");
  document.getElementById("summary").replaceChildren(failure("No member chosen. Go back to the teams and tap a member."));
} else {
  load(true);
  schedule();
}
