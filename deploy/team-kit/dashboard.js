"use strict";

// Everything from the API is untrusted text: it is only ever put in with textContent.
const $ = (id) => document.getElementById(id);

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}

function table(head, rows) {
  const t = el("table");
  const tr = el("tr");
  head.forEach((h) => tr.append(el("th", h)));
  t.append(tr);
  rows.forEach((row) => {
    const r = el("tr");
    row.forEach((cell) => r.append(el("td", cell === null || cell === undefined ? "-" : cell)));
    t.append(r);
  });
  return t;
}

function fill(section, node, truncated) {
  const body = $(section).querySelector(".body");
  body.replaceChildren(node);
  if (truncated) body.append(el("p", `${truncated} more not shown`, "muted"));
}

function empty(what) {
  return el("p", what, "muted");
}

function members(view) {
  const rows = [];
  view.pools.forEach((pool) =>
    pool.members.forEach((m) => {
      const last = m.last_run ? m.last_run.outcome + (m.last_run.failure_code ? ` (${m.last_run.failure_code})` : "") : "-";
      const state = m.disabled ? `${m.state}, disabled` : m.state;
      rows.push([pool.name, m.principal_id, state, m.active_task_id || "idle", last, m.claims.join(", ")]);
    }),
  );
  fill("members", rows.length ? table(["Pool", "Member", "State", "Running", "Last run", "Claims"], rows) : empty("No members."));
}

function collections(view) {
  const box = el("div");
  view.collections.forEach((c) => {
    box.append(el("h3", c.name));
    if (c.status !== "ok") return box.append(el("p", "unavailable", "bad"));
    const counts = Object.entries(c.by_state || {}).map(([k, v]) => `${k} ${v}`).join(", ");
    box.append(el("p", `${c.total} records: ${counts || "none"}`));
    if (c.claimed.length) {
      const rows = c.claimed.map((k) => [k.record_id, k.title, k.state, k.holder, k.age_seconds === null ? null : `${k.age_seconds}s`]);
      box.append(table(["Record", "Title", "State", "Held by", "Age"], rows));
    }
    if (c.truncated) box.append(el("p", `${c.truncated} more claims not shown`, "muted"));
  });
  fill("collections", view.collections.length ? box : empty("No collections."));
}

function mailbox(view) {
  const rows = view.mailbox.addresses.map((a) => [a.address, a.pending, a.in_flight, a.dead, a.consumed]);
  fill("mailbox", rows.length ? table(["Address", "Pending", "In flight", "Dead", "Consumed"], rows) : empty("No mail."), view.mailbox.truncated);
}

function replies(list) {
  const body = $("mailbox").querySelector(".body");
  body.append(el("h3", "Replies to the supervisor"));
  if (!list.length) return body.append(empty("No replies."));
  list.forEach((m) => {
    const text = m.parts.map((p) => (p.text !== undefined ? p.text : JSON.stringify(p.data))).join("\n");
    const item = el("div", null, "reply");
    item.append(el("div", `${m.sender_id} to ${m.to.instance}, ${m.created_at}`, "muted"), el("div", text));
    body.append(item);
  });
}

function approvals(view) {
  const rows = view.approvals.items.map((a) => [a.request_id, a.operation, a.resource, a.state, a.task_id, a.expires_at]);
  fill("approvals", rows.length ? table(["Request", "Operation", "Resource", "State", "Task", "Expires"], rows) : empty("Nothing waits for approval."), view.approvals.truncated);
}

function spend(view) {
  const box = el("div");
  const accounts = view.spend.accounts.map((a) => [
    a.account,
    a.window,
    a.usd === null ? "-" : `${a.used_usd} / ${a.usd}`,
    a.runs === null || a.runs === undefined ? "-" : `${a.used_runs} / ${a.runs}`,
    a.window_ends_at,
  ]);
  box.append(accounts.length ? table(["Account", "Window", "USD", "Runs", "Window ends"], accounts) : empty("No ceilings."));
  if (view.spend.expectations.length) {
    const rows = view.spend.expectations.map((e) => [e.pool, e.window_key, e.outcome, e.observed, e.reported_at]);
    box.append(el("h3", "Expectations"), table(["Pool", "Window", "Outcome", "Observed", "Reported"], rows));
  }
  fill("spend", box, view.spend.expectations_truncated);
}

function journal(view) {
  const rows = view.journal.events.map((e) => [e.cursor, e.at, e.type, e.producer_id, e.subject, e.task_id]);
  fill("journal", rows.length ? table(["#", "At", "Event", "By", "Subject", "Task"], rows) : empty("No events."), view.journal.truncated);
}

function roleChoices(view) {
  const select = $("role");
  const chosen = select.value;
  select.replaceChildren(...view.pools.map((p) => el("option", p.name)));
  if (view.pools.some((p) => p.name === chosen)) select.value = chosen;
}

function render(view, list) {
  $("title").textContent = `${view.group} (${view.namespace})`;
  $("summary").textContent = `${view.state}; generation ${view.generation}, observed ${view.observed_generation}; as of ${view.as_of}`;
  roleChoices(view);
  members(view);
  collections(view);
  mailbox(view);
  approvals(view);
  spend(view);
  journal(view);
  replies(list);
}

async function read(path) {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) throw new Error((await response.json()).code);
  return response.json();
}

async function refresh() {
  try {
    const [view, list] = await Promise.all([read("/api/status"), read("/api/replies")]);
    render(view, list);
    $("error").textContent = "";
  } catch (error) {
    $("error").textContent = `Cannot read the team's status: ${error.message}`;
  }
}

// One key per message. A second submit of the same form re-sends the same key, so the
// kernel delivers it once; the key changes only after a send succeeded or the text did.
function newKey() {
  return "dash-" + crypto.randomUUID();
}
let pending = null;

async function submit(event) {
  event.preventDefault();
  const body = { role: $("role").value, text: $("text").value, client_key: null };
  if (!pending || pending.role !== body.role || pending.text !== body.text) {
    pending = { ...body, client_key: newKey() };
  }
  body.client_key = pending.client_key;
  $("send").disabled = true;
  $("sent").textContent = "Sending…";
  try {
    const response = await fetch("/api/message", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.code);
    $("sent").textContent = `Sent to ${body.role} (${result.message_id}).`;
    $("text").value = "";
    pending = null;
    refresh();
  } catch (error) {
    $("sent").textContent = `Not sent: ${error.message}. Sending again is safe.`;
  } finally {
    $("send").disabled = false;
  }
}

$("message-form").addEventListener("submit", submit);
refresh();
setInterval(refresh, 15000);
