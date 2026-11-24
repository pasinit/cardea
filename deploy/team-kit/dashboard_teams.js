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

const ROLE_WORDS = { dev: "developer", pm: "product manager", tl: "team lead", pe: "principal engineer", ux: "designer" };

function shortName(id) {
  const parts = String(id).split(".");
  return parts.length > 1 ? parts.slice(1).join(".") : String(id);
}

function allMembers(view) {
  return view.pools.flatMap((pool) => pool.members.map((m) => ({ pool: pool.name, m })));
}

function ago(iso) {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "-";
  const s = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

// What needs the owner: approvals waiting, dead-lettered mail, failed last runs.
function needsYou(view) {
  const items = [];
  view.approvals.items
    .filter((a) => a.state === "pending")
    .forEach((a) => items.push(`${a.operation} on ${a.resource} waits for your approval.`));
  view.mailbox.addresses
    .filter((a) => a.dead > 0)
    .forEach((a) => items.push(`${a.dead} undeliverable mail for ${a.address}.`));
  allMembers(view)
    .filter(({ m }) => !m.active_task_id && m.last_run && m.last_run.outcome === "failed")
    .forEach(({ m }) => items.push(`${shortName(m.principal_id)}: last run failed (${m.last_run.failure_code || "unknown"}).`));
  const box = el("section", null, "needs");
  box.setAttribute("role", "status");
  box.append(el("h3", items.length ? `Needs you (${items.length})` : "Needs you"));
  if (!items.length) return box.append(el("p", "Nothing needs you right now.")), box;
  const list = el("ul");
  items.forEach((text) => list.append(el("li", text)));
  box.append(list);
  return box;
}

function summary(view) {
  const members = allMembers(view);
  const working = members.filter(({ m }) => m.active_task_id).length;
  let open = 0;
  let review = 0;
  view.collections.forEach((c) => {
    if (c.status !== "ok") return;
    const by = c.by_state || {};
    Object.entries(by).forEach(([k, v]) => {
      if (k === "in_review") review += v;
      if (k === "open" || k === "ready" || k === "in_progress" || k === "in_review") open += v;
    });
  });
  const mails = view.mailbox.addresses.reduce((n, a) => n + a.pending + a.in_flight, 0);
  const day = view.spend.accounts.find((a) => a.account === "group" && a.window === "day");
  const cards = el("div", null, "cards");
  cards.setAttribute("role", "group");
  cards.setAttribute("aria-label", "Summary");
  const card = (big, small) => {
    const c = el("div", null, "card");
    c.append(el("b", big), el("span", small));
    return c;
  };
  cards.append(card(`${working} of ${members.length}`, "members working"));
  cards.append(card(String(open), `work items open, ${review} in review`));
  cards.append(card(String(mails), "mails waiting"));
  if (day && day.usd !== null && day.usd > 0) {
    const c = card(`$${day.used_usd} of $${day.usd}`, "spent today");
    const meter = el("div", null, "meter");
    meter.setAttribute("role", "img");
    const pct = Math.min(100, Math.round((100 * day.used_usd) / day.usd));
    meter.setAttribute("aria-label", `${pct} percent of the daily limit`);
    const bar = el("i");
    bar.style.width = `${pct}%`;
    meter.append(bar);
    c.append(meter);
    cards.append(c);
  } else if (day) {
    cards.append(card(`$${day.used_usd}`, "spent today"));
  }
  return cards;
}

function memberCard(pool, m) {
  const card = el("div", null, "member");
  const running = Boolean(m.active_task_id);
  const state = m.disabled ? "paused" : running ? "running" : "idle";
  const head = el("div");
  head.append(el("b", shortName(m.principal_id)), document.createTextNode(" "), el("span", ROLE_WORDS[pool] || pool, "muted"));
  head.append(el("span", state, `state state-${state}`));
  card.append(head);
  const claim = m.claims && m.claims[0];
  card.append(el("div", running ? (claim ? `Working on ${claim}` : "Working") : "Waiting for work"));
  if (m.last_run) {
    const outcome = m.last_run.outcome + (m.last_run.failure_code ? ` (${m.last_run.failure_code})` : "");
    card.append(el("div", `Last run: ${outcome}, ${ago(m.last_run.finished_at)}`, m.last_run.outcome === "failed" ? "bad" : "muted"));
  } else {
    card.append(el("div", "Has not run yet", "muted"));
  }
  return card;
}

function overview(team) {
  const box = el("div");
  if (team.error) {
    box.append(el("p", `Cannot read this team's status: ${team.error}`, "bad"));
    return box;
  }
  const view = team.view;
  if (view.state !== "active") {
    const banner = el("p", "Team paused. Work and mail below are the last known state.", "banner");
    banner.setAttribute("role", "status");
    box.append(banner);
  }
  box.append(note(`${view.group} (${view.namespace}): ${team.state}. Updated ${view.as_of}. Refreshes every 15 seconds.`));
  box.append(needsYou(view), summary(view));
  const grid = el("div", null, "grid");
  allMembers(view).forEach(({ pool, m }) => grid.append(memberCard(pool, m)));
  box.append(el("h3", "Members"), allMembers(view).length ? grid : note("No members."));
  box.append(...section("Work", work(view)), ...section("Mail", mail(view)));
  box.append(...section("Approvals", approvals(view)), ...section("Spend", spend(view)), ...section("Latest events", events(view)));
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
