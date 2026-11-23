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

const READ_ONLY_NOTE = "Read-only while paused";
const LOAD_ERROR = "We could not load the team status. Try again in a moment.";

function sectionError(what) {
  return el("p", `We could not load ${what}. Try again in a moment.`, "bad");
}

// A heading with, while the team is paused, the small note that the section is read-only.
function heading(title, paused) {
  return paused ? [el("h3", title), el("p", READ_ONLY_NOTE, "muted small")] : [el("h3", title)];
}

function work(view) {
  const box = el("div");
  view.collections.forEach((c) => {
    if (c.status !== "ok") return box.append(el("p", `${c.name}: unavailable`, "bad"));
    const counts = Object.entries(c.by_state || {}).map(([k, v]) => `${k} ${v}`).join(", ");
    box.append(el("p", `${c.name}: ${c.total} records: ${counts || "none"}`));
  });
  return view.collections.length ? box : note("No work items.");
}

function mail(view) {
  const rows = view.mailbox.addresses.map((a) => [a.address, a.pending, a.in_flight, a.dead]);
  return rows.length ? table(["Address", "Pending", "In flight", "Dead"], rows) : note("No messages.");
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
  const paused = team.state === "paused";
  if (!team.view) {
    // Nothing could be read: each section says so, and the banner offers the retry.
    box.append(...heading("Work", paused), sectionError("work"), ...heading("Mail", paused), sectionError("mail"));
    return box;
  }
  const view = team.view;
  box.append(note(`${view.group} (${view.namespace}): ${view.state}; as of ${view.as_of}`));
  box.append(...section("Members", members(view)));
  box.append(...heading("Work", paused), work(view), ...heading("Mail", paused), mail(view));
  box.append(...section("Approvals", approvals(view)), ...section("Spend", spend(view)), ...section("Latest events", events(view)));
  return box;
}

function when(iso) {
  const date = new Date(iso);
  if (typeof iso !== "string" || Number.isNaN(date.getTime())) return null;
  const time = el("time", `${date.toLocaleString("en-US", { dateStyle: "medium", timeStyle: "short", timeZone: "UTC" })} UTC`);
  time.dateTime = date.toISOString();
  return time;
}

// The status banner above Work and Mail. Only fixed sentences and text from the API, as text.
function banner(team) {
  const box = el("div", null, "banner");
  box.setAttribute("role", "status");
  if (team.state !== "paused") {
    if (!team.error && team.view) return null;
    box.className = "banner banner-error";
    box.append(el("p", LOAD_ERROR));
    const retry = el("button", "Try again", "retry");
    retry.type = "button";
    retry.addEventListener("click", retryNow);
    box.append(retry);
    return box;
  }
  box.className = "banner banner-paused";
  const icon = el("span", null, "pause-icon");
  icon.setAttribute("aria-hidden", "true");
  icon.append(el("span"), el("span"));
  const text = el("div");
  const pause = team.pause || {};
  const line = el("p", "Paused since ");
  const at = when(pause.since);
  line.append(at || "an unknown time", `. Reason: ${pause.reason || "not given"}.`);
  text.append(el("h2", "Team paused"), line, el("p", "Work and mail below are the last known state. Nothing is being processed."));
  box.append(icon, text);
  return box;
}

async function get(path) {
  const response = await fetch(path, { cache: "no-store" });
  const body = await response.json();
  if (!response.ok) throw new Error(body.code);
  return body;
}

let teams = [];
let selected = null;
let filter = { member: "", item: "" };
let mailRun = 0;
let drawn = false;

function retryNow() {
  showLoading();
  refresh();
}

function showLoading() {
  const box = el("div", "Checking team status…", "banner");
  box.setAttribute("role", "status");
  document.getElementById("team").replaceChildren(box);
  drawn = false;
}

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
      const state = team.state || "unreadable";
      tab.append(el("span", state, `state state-${team.state || "stalled"}`));
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
    if (run === mailRun) more.replaceChildren(sectionError("mail"));
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
  const paused = team.state === "paused";
  document.getElementById("mail-note").hidden = !paused;
  root.replaceChildren(el("div", null, "filters"), list, more);
  root.firstChild.append(member, item, apply);
  loadMail(team, "", list, more, run);
}

function paint(team) {
  const found = banner(team);
  const top = document.querySelector("#team .status");
  top.replaceChildren(...(found ? [found] : []));
  document.querySelector("#team .scroll").replaceChildren(overview(team));
}

function draw() {
  tabs();
  const team = teams.find((t) => t.name === selected);
  const target = document.getElementById("team");
  if (!team) {
    drawn = false;
    return target.replaceChildren(note("No team."));
  }
  target.replaceChildren(el("h2", team.name), el("div", null, "status"), el("div", null, "scroll"));
  drawn = true;
  paint(team);
  mailView(team);
}

async function refresh() {
  try {
    teams = (await get("/api/status")).teams;
    const before = selected;
    if (!teams.some((t) => t.name === selected)) selected = teams.length ? teams[0].name : null;
    if (before === selected && drawn) {
      // Keep the open mail view and its filters; only the tabs, banner and overview are redrawn.
      tabs();
      const team = teams.find((t) => t.name === selected);
      if (team) paint(team);
    } else {
      draw();
    }
  } catch (error) {
    drawn = false;
    document.getElementById("tabs").replaceChildren();
    const failed = banner({ error: "unreadable" });
    document.getElementById("team").replaceChildren(failed);
  }
}

refresh();
setInterval(refresh, 15000);
