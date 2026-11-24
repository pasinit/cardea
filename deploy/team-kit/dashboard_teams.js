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

function overview(team) {
  const box = el("div");
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

async function get(path) {
  const response = await fetch(path, { cache: "no-store" });
  const body = await response.json();
  if (!response.ok) throw new Error(body.code);
  return body;
}

async function post(path, body) {
  const response = await fetch(path, {
    method: "POST",
    cache: "no-store",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.code);
  return result;
}

// A paused team's supervisor is disabled, so its overview cannot be read: that refusal means paused.
function isPaused(team) {
  return team.state === "paused" || team.error === "principal_disabled";
}

function controlKeyOf(team) {
  return team && team.controls && (!team.error || isPaused(team)) ? `${team.name}/${isPaused(team)}` : null;
}

let controlKey = null;
let controlMessage = "";

// Team-wide Pause / Resume. Every action asks first; the kernel and the host decide, not this page.
function controls(team) {
  const root = document.getElementById("controls");
  controlKey = controlKeyOf(team);
  if (!controlKey) return root.replaceChildren();
  const paused = isPaused(team);
  const verb = paused ? "resume" : "pause";
  const label = paused ? "Resume team" : "Pause team";
  const status = el("p", controlMessage, "muted");
  status.setAttribute("role", "status");
  const start = el("button", label, paused ? "" : "danger");
  const actions = el("div", null, "actions");
  actions.append(start);
  start.addEventListener("click", () => {
    const box = el("div", null, "confirm");
    box.setAttribute("role", "alertdialog");
    box.setAttribute("aria-labelledby", "confirm-title");
    const title = el("b", paused ? `Resume ${team.name}?` : `Pause ${team.name}?`);
    title.id = "confirm-title";
    const text = paused
      ? "The members can start new runs again. This is recorded in the audit log."
      : "Every unfinished run stops and nothing new starts until you resume. This is recorded in the audit log.";
    const reason = el("input");
    reason.placeholder = "Reason (optional, shown on the dashboard)";
    reason.maxLength = 200;
    reason.setAttribute("aria-label", "Reason");
    const yes = el("button", paused ? "Yes, resume the team" : "Yes, pause the team", "danger");
    const cancel = el("button", "Cancel");
    cancel.addEventListener("click", () => controls(team));
    yes.addEventListener("click", async () => {
      yes.disabled = cancel.disabled = true;
      try {
        await post("/api/control", { team: team.name, action: verb, confirm: true, ...(paused ? {} : { reason: reason.value }) });
        controlMessage = paused ? "Team resumed." : "Team paused.";
      } catch (error) {
        controlMessage = `Could not ${verb} the team. Nothing was changed unless the team now shows a new state. Try again.`;
      }
      await refresh();
      controls(teams.find((t) => t.name === team.name));
    });
    const row = el("div", null, "actions");
    row.append(yes, cancel);
    box.append(title, el("p", text));
    if (!paused) box.append(reason);
    box.append(row);
    root.replaceChildren(box, status);
    (paused ? yes : reason).focus();
  });
  root.replaceChildren(actions, status);
}

let teams = [];
let selected = null;
let filter = { member: "", item: "" };
let mailRun = 0;

function selectTeam(name) {
  if (name === selected) return;
  selected = name;
  filter = { member: "", item: "" };
  draw();
}

function tabs() {
  const bar = document.getElementById("tabs");
  bar.replaceChildren(
    ...teams.map((team) => {
      const tab = el("button", team.name);
      tab.setAttribute("role", "tab");
      tab.setAttribute("aria-selected", String(team.name === selected));
      const state = team.error ? "unreadable" : team.state;
      tab.append(el("span", state, `state state-${team.error ? "stalled" : team.state}`));
      tab.addEventListener("click", () => selectTeam(team.name));
      return tab;
    }),
  );
}

// One message: every string is put in as text, never as markup, and nothing becomes a link.
function message(m) {
  const box = el("div", null, "message");
  const who = (p) => (p.role ? `${p.role} (${p.address})` : p.address);
  const kind = m.kind === "notice" ? " [notice]" : "";
  const item = m.item ? `; about ${m.item.collection}/${m.item.record_id}` : "";
  const delivery = Object.entries(m.deliveries).map(([k, v]) => `${k} ${v}`).join(", ");
  box.append(el("div", `${m.sent_at}${kind}: ${who(m.from)} to ${who(m.to)}${item}${delivery ? `; ${delivery}` : ""}`, "head"));
  m.parts.forEach((part) => {
    const text = typeof part.text === "string" ? part.text : JSON.stringify(part.data === undefined ? part : part.data, null, 2);
    box.append(el("pre", text));
  });
  return box;
}

async function loadMail(team, after, list, more, run) {
  const query = new URLSearchParams({ team: team.name });
  if (filter.member) query.set("member", filter.member);
  if (filter.item) query.set("item", filter.item);
  if (after) query.set("after", after);
  try {
    const page = await get(`/api/mail?${query}`);
    if (run !== mailRun) return;
    list.append(...page.items.map(message));
    if (!list.children.length) list.append(note("No messages."));
    more.replaceChildren();
    if (page.next) {
      const next = el("button", "Load older");
      next.addEventListener("click", () => loadMail(team, page.next, list, more, run));
      more.append(next);
    }
    more.append(note(`${page.total} matching`));
  } catch (error) {
    if (run === mailRun) more.replaceChildren(el("p", `Cannot read this team's mail: ${error.message}`, "bad"));
  }
}

function mailView(team) {
  const run = ++mailRun;
  const root = document.getElementById("mail");
  const list = el("div");
  const more = el("div");
  const member = el("select");
  member.append(el("option", "Any member"));
  member.firstChild.value = "";
  if (team.view) {
    team.view.pools.forEach((pool) =>
      pool.members.forEach((m) => {
        const option = el("option", m.principal_id);
        option.value = m.principal_id;
        member.append(option);
      }),
    );
  }
  member.value = filter.member;
  const item = el("input");
  item.placeholder = "Item: collection/record-id";
  item.maxLength = 200;
  item.value = filter.item;
  const apply = el("button", "Filter");
  apply.addEventListener("click", () => {
    filter = { member: member.value, item: item.value.trim() };
    mailView(team);
  });
  root.replaceChildren(el("div", null, "filters"), list, more);
  root.firstChild.append(member, item, apply);
  loadMail(team, "", list, more, run);
}

function draw() {
  tabs();
  const team = teams.find((t) => t.name === selected);
  const target = document.getElementById("team");
  if (!team) return target.replaceChildren(note("No team."));
  const scroll = el("div", null, "scroll");
  scroll.append(overview(team));
  target.replaceChildren(el("h2", team.name), scroll);
  mailView(team);
  controls(team);
}

async function refresh() {
  try {
    teams = (await get("/api/status")).teams;
    const before = selected;
    if (!teams.some((t) => t.name === selected)) selected = teams.length ? teams[0].name : null;
    if (before === selected && document.getElementById("team").firstChild) {
      // Keep the open mail view and its filters; only the tabs and the overview are redrawn.
      tabs();
      const team = teams.find((t) => t.name === selected);
      if (team) document.querySelector("#team .scroll").replaceChildren(overview(team));
      if (team && controlKeyOf(team) !== controlKey) controls(team);
    } else {
      draw();
    }
  } catch (error) {
    document.getElementById("tabs").replaceChildren(el("p", `Cannot read the dashboard: ${error.message}`, "bad"));
  }
}

refresh();
setInterval(refresh, 15000);
