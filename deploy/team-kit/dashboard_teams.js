"use strict";

// Everything from the API is untrusted text: it is only ever put in with textContent.
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

function section(title, node) {
  return [el("h3", title), node];
}

function note(text) {
  return el("p", text, "muted");
}

function members(view) {
  const rows = [];
  view.pools.forEach((pool) =>
    pool.members.forEach((m) => {
      const last = m.last_run ? m.last_run.outcome + (m.last_run.failure_code ? ` (${m.last_run.failure_code})` : "") : "-";
      rows.push([pool.name, m.principal_id, m.disabled ? `${m.state}, disabled` : m.state, m.active_task_id || "idle", last]);
    }),
  );
  return rows.length ? table(["Pool", "Member", "State", "Running", "Last run"], rows) : note("No members.");
}

function work(view) {
  const box = el("div");
  view.collections.forEach((c) => {
    if (c.status !== "ok") return box.append(el("p", `${c.name}: unavailable`, "bad"));
    const counts = Object.entries(c.by_state || {}).map(([k, v]) => `${k} ${v}`).join(", ");
    box.append(el("p", `${c.name}: ${c.total} records: ${counts || "none"}`));
  });
  return view.collections.length ? box : note("No collections.");
}

function mail(view) {
  const rows = view.mailbox.addresses.map((a) => [a.address, a.pending, a.in_flight, a.dead]);
  return rows.length ? table(["Address", "Pending", "In flight", "Dead"], rows) : note("No mail.");
}

function approvals(view) {
  const rows = view.approvals.items.map((a) => [a.request_id, a.operation, a.state]);
  return rows.length ? table(["Request", "Operation", "State"], rows) : note("Nothing waits for approval.");
}

function spend(view) {
  const rows = view.spend.accounts.map((a) => [
    a.account,
    a.window,
    a.usd === null ? "-" : `${a.used_usd} / ${a.usd}`,
    a.runs === null || a.runs === undefined ? "-" : `${a.used_runs} / ${a.runs}`,
  ]);
  return rows.length ? table(["Account", "Window", "USD", "Runs"], rows) : note("No ceilings.");
}

function events(view) {
  const rows = view.journal.events.map((e) => [e.cursor, e.at, e.type, e.subject]);
  return rows.length ? table(["#", "At", "Event", "Subject"], rows) : note("No events.");
}

function column(team) {
  const box = el("section", null, "team");
  box.dataset.team = team.name;
  box.append(el("h2", team.name));
  if (team.error) {
    box.append(el("p", `Cannot read this team's status: ${team.error}`, "bad"));
    return box;
  }
  const view = team.view;
  box.append(note(`${view.group} (${view.namespace}): ${view.state}; as of ${view.as_of}`));
  box.append(...section("Members", members(view)), ...section("Work", work(view)), ...section("Mail", mail(view)));
  box.append(...section("Approvals", approvals(view)), ...section("Spend", spend(view)), ...section("Latest events", events(view)));
  return box;
}

async function refresh() {
  const root = document.getElementById("teams");
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    if (!response.ok) throw new Error((await response.json()).code);
    root.replaceChildren(...(await response.json()).teams.map(column));
  } catch (error) {
    root.replaceChildren(el("p", `Cannot read the dashboard: ${error.message}`, "bad"));
  }
}

refresh();
setInterval(refresh, 15000);
