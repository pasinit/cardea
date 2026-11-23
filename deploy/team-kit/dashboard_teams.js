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
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    cache: "no-store",
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.detail ? `${result.code}: ${result.detail}` : result.code);
  return result;
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

const ROLES = ["pm", "tl", "dev", "pe"];

// One labelled field; a blank field leaves its setting alone.
function field(form, label, control, name) {
  const wrap = el("label", label);
  control.name = name;
  wrap.append(control);
  form.append(wrap);
  return control;
}

function whole(value, name) {
  if (!/^[0-9]+$/.test(value)) throw new Error(`${name} must be a whole number`);
  return Number(value);
}

function price(value, name) {
  if (!/^[0-9]+(\.[0-9]+)?$/.test(value)) throw new Error(`${name} must be a number`);
  return Number(value);
}

// The settings document from the filled fields, or an Error naming the field.
function collect(inputs) {
  const out = {};
  const text = (name) => inputs[name].value.trim();
  const set = (setting, path, value) => {
    out[setting] = out[setting] || {};
    let node = out[setting];
    path.slice(0, -1).forEach((key) => (node = node[key] = node[key] || {}));
    node[path[path.length - 1]] = value;
  };
  if (text("developers")) out.developers = whole(text("developers"), "developers");
  if (text("model")) out.model = text("model");
  if (text("run_team")) set("run_limits", ["team"], whole(text("run_team"), "team runs"));
  if (text("usd_team")) set("spend_limits", ["team_usd"], price(text("usd_team"), "team USD"));
  if (text("usd_run")) set("spend_limits", ["per_run_usd"], price(text("usd_run"), "USD per run"));
  ROLES.forEach((role) => {
    if (text(`runs_${role}`)) set("run_limits", ["roles", role], whole(text(`runs_${role}`), `${role} runs`));
    if (text(`usd_${role}`)) set("spend_limits", ["roles_usd", role], price(text(`usd_${role}`), `${role} USD`));
    if (text(`cron_${role}`)) {
      set("schedules", [role], text(`cron_${role}`).split("\n").map((line) => line.trim()).filter(Boolean));
    }
  });
  if (text("goals")) {
    out.product_goals = { goals: text("goals") };
    if (text("non_goals")) out.product_goals.non_goals = text("non_goals");
  }
  if (inputs.paused.value) {
    out.paused = { paused: inputs.paused.value === "pause" };
    if (out.paused.paused && text("reason")) out.paused.reason = text("reason");
  }
  return out;
}

// Goals arrive as sizes ({bytes}), never as text.
function shownValue(value) {
  if (value === null || value === undefined) return "unset";
  if (typeof value === "object" && !Array.isArray(value)) return `${value.bytes} bytes`;
  return JSON.stringify(value);
}

function settingsView(team) {
  const root = document.getElementById("settings");
  if (!team.editable) return root.replaceChildren(note("Settings are not offered for this team."));
  const form = el("form");
  const inputs = {};
  const add = (label, name, control) => (inputs[name] = field(form, label, control, name));
  const number = () => Object.assign(el("input"), { inputMode: "decimal", maxLength: 12 });
  add("Developers", "developers", number());
  add("Model", "model", Object.assign(el("input"), { maxLength: 128 }));
  add("Team runs", "run_team", number());
  add("Team USD", "usd_team", number());
  add("USD per run", "usd_run", number());
  ROLES.forEach((role) => {
    add(`${role} runs`, `runs_${role}`, number());
    add(`${role} USD`, `usd_${role}`, number());
    add(`${role} schedules (cron, one per line)`, `cron_${role}`, el("textarea"));
  });
  add("Goals (shown by size only)", "goals", el("textarea"));
  add("Non-goals", "non_goals", el("textarea"));
  const paused = el("select");
  [["", "No change"], ["pause", "Pause the team"], ["resume", "Resume the team"]].forEach(([value, label]) => {
    const option = el("option", label);
    option.value = value;
    paused.append(option);
  });
  add("Pause", "paused", paused);
  add("Reason for pausing", "reason", Object.assign(el("input"), { maxLength: 200 }));
  const out = el("div");
  const preview = el("button", "Preview");
  preview.type = "button";
  const show = (text, className) => out.replaceChildren(el("p", text, className));
  preview.addEventListener("click", async () => {
    let settings;
    try {
      settings = collect(inputs);
      if (!Object.keys(settings).length) throw new Error("nothing to change");
    } catch (error) {
      return show(error.message, "bad");
    }
    try {
      const found = await post("/api/settings/preview", { team: team.name, settings });
      review(team, settings, found, out);
    } catch (error) {
      show(`Cannot preview: ${error.message}`, "bad");
    }
  });
  form.append(preview);
  root.replaceChildren(form, out);
}

// The preview, then Confirm (which sends what was previewed with its confirm code) or Cancel.
function review(team, settings, found, out) {
  const lines = found.changes.map((c) => el("li", `${c.field}: ${shownValue(c.from)} -> ${shownValue(c.to)}`));
  out.replaceChildren(el("h4", found.changes.length ? "Review the change" : "No change"));
  if (lines.length) out.append(el("ul", null), note(found.authority));
  if (lines.length) out.querySelector("ul").append(...lines);
  found.problems.forEach((p) => out.append(el("p", `Problem: ${p}`, "bad")));
  if (!lines.length || found.problems.length) return;
  const row = el("div", null, "row");
  const confirm = el("button", "Confirm and apply");
  const cancel = el("button", "Cancel");
  [confirm, cancel].forEach((b) => (b.type = "button"));
  cancel.addEventListener("click", () => out.replaceChildren(note("Cancelled; nothing changed.")));
  confirm.addEventListener("click", async () => {
    row.replaceChildren(note("Applying…"));
    try {
      const done = await post("/api/settings/apply", { team: team.name, settings, confirm: found.confirm });
      out.replaceChildren(el("p", done.status === "applied" ? "Applied." : "Already in effect.", "ok"));
      refresh();
    } catch (error) {
      out.replaceChildren(el("p", `Not applied: ${error.message}`, "bad"));
    }
  });
  row.append(confirm, cancel);
  out.append(row);
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
  settingsView(team);
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
    } else {
      draw();
    }
  } catch (error) {
    document.getElementById("tabs").replaceChildren(el("p", `Cannot read the dashboard: ${error.message}`, "bad"));
  }
}

refresh();
setInterval(refresh, 15000);
