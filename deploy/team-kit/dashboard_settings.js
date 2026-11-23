"use strict";

// Everything from the API is untrusted text: it is only ever put in with textContent or value.
const ROLES = ["pm", "tl", "dev", "pe", "ux"];
const inputs = {};
let current = null;
let pending = null; // the settings and token of the preview now on screen

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}

function say(text, className) {
  document.getElementById("status").replaceChildren(el("p", text, className));
}

async function call(method, path, body) {
  const options = { method, cache: "no-store" };
  if (body !== undefined) {
    options.headers = { "Content-Type": "application/json" };
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const found = await response.json();
  if (!response.ok) throw new Error(found.code);
  return found;
}

function field(key, label, kind, wide) {
  const box = el("div");
  box.append(el("label", label));
  const input = el(kind === "text" ? "textarea" : "input");
  if (kind === "number") {
    input.type = "number";
    input.step = "any";
  }
  inputs[key] = input;
  input.addEventListener("input", dropPreview);
  box.append(input);
  return box;
}

function group(title, ...boxes) {
  const row = el("div", null, "row");
  row.append(...boxes);
  return [el("h3", title), row];
}

function build() {
  const form = document.getElementById("form");
  const roles = (prefix, label, kind) => ROLES.map((r) => field(`${prefix}.${r}`, `${label} ${r}`, kind));
  const pause = el("input");
  pause.type = "checkbox";
  pause.style.width = "auto";
  pause.addEventListener("input", dropPreview);
  inputs["paused.paused"] = pause;
  const pauseBox = el("div");
  pauseBox.append(el("label", "Paused"), pause);
  form.replaceChildren(
    ...group("Run limits", field("run_limits.team", "Team runs", "number"), ...roles("run_limits.roles", "Runs", "number")),
    ...group("Spend limits (USD)", field("spend_limits.team_usd", "Team", "number"), field("spend_limits.per_run_usd", "Per run", "number"), ...roles("spend_limits.roles_usd", "USD", "number")),
    ...group("Pause", pauseBox, field("paused.reason", "Reason (when pausing)", "line")),
    ...group("Schedules (one cron expression per line)", ...ROLES.map((r) => field(`schedules.${r}`, r, "text"))),
    ...group("Team", field("developers", "Developers", "number"), field("model", "Model", "line")),
    ...group("Product", field("product_goals.goals", "Goals", "text"), field("product_goals.non_goals", "Non-goals", "text")),
  );
}

function fill(settings) {
  const run = settings.run_limits;
  const spend = settings.spend_limits;
  const set = (key, value) => {
    inputs[key].value = value === null || value === undefined ? "" : value;
  };
  set("run_limits.team", run.team);
  set("spend_limits.team_usd", spend.team_usd);
  set("spend_limits.per_run_usd", spend.per_run_usd);
  ROLES.forEach((r) => {
    set(`run_limits.roles.${r}`, run.roles[r]);
    set(`spend_limits.roles_usd.${r}`, spend.roles_usd[r]);
    set(`schedules.${r}`, (settings.schedules[r] || []).join("\n"));
  });
  inputs["paused.paused"].checked = settings.paused.paused;
  set("paused.reason", "");
  set("developers", settings.developers);
  set("model", settings.model);
  set("product_goals.goals", settings.product_goals.goals);
  set("product_goals.non_goals", settings.product_goals.non_goals);
}

// Only what differs from the settings now is sent; a blank number or text means "no change".
function changes() {
  const out = {};
  const num = (key, before, put) => {
    const raw = inputs[key].value.trim();
    if (raw !== "" && Number(raw) !== before) put(Number(raw));
  };
  const nest = (setting, ...path) => (value) => {
    let node = (out[setting] = out[setting] || {});
    path.slice(0, -1).forEach((k) => (node = node[k] = node[k] || {}));
    node[path[path.length - 1]] = value;
  };
  const run = current.run_limits;
  const spend = current.spend_limits;
  num("run_limits.team", run.team, nest("run_limits", "team"));
  num("spend_limits.team_usd", spend.team_usd, nest("spend_limits", "team_usd"));
  num("spend_limits.per_run_usd", spend.per_run_usd, nest("spend_limits", "per_run_usd"));
  ROLES.forEach((r) => {
    num(`run_limits.roles.${r}`, run.roles[r], nest("run_limits", "roles", r));
    num(`spend_limits.roles_usd.${r}`, spend.roles_usd[r], nest("spend_limits", "roles_usd", r));
    const lines = inputs[`schedules.${r}`].value.split("\n").map((l) => l.trim()).filter(Boolean);
    if (JSON.stringify(lines) !== JSON.stringify(current.schedules[r] || [])) nest("schedules", r)(lines);
  });
  const paused = inputs["paused.paused"].checked;
  if (paused !== current.paused.paused) {
    const reason = inputs["paused.reason"].value.trim();
    out.paused = paused && reason ? { paused, reason } : { paused };
  }
  num("developers", current.developers, (v) => (out.developers = v));
  const model = inputs.model.value.trim();
  if (model && model !== current.model) out.model = model;
  const goals = inputs["product_goals.goals"].value;
  const non = inputs["product_goals.non_goals"].value;
  const before = current.product_goals;
  if (goals !== (before.goals || "") || non !== (before.non_goals || "")) {
    out.product_goals = { goals, non_goals: non };
  }
  return out;
}

function dropPreview() {
  pending = null;
  actions();
}

function actions() {
  const bar = document.getElementById("actions");
  const preview = el("button", "Preview");
  preview.type = "button";
  preview.addEventListener("click", showPreview);
  bar.replaceChildren(preview);
  if (!pending) return;
  const confirm = el("button", "Confirm and apply");
  confirm.type = "button";
  confirm.addEventListener("click", applyChange);
  const cancel = el("button", "Cancel");
  cancel.type = "button";
  cancel.addEventListener("click", cancelChange);
  bar.append(confirm, cancel);
}

function team() {
  return document.getElementById("team").value;
}

async function showPreview() {
  if (!current) return;
  const settings = changes();
  pending = null;
  if (!Object.keys(settings).length) {
    document.getElementById("preview").textContent = "Nothing to change.";
    return actions();
  }
  try {
    const found = await call("POST", "/api/settings/preview", { team: team(), settings });
    document.getElementById("preview").textContent = found.lines.join("\n");
    if (found.status === "unchanged") say("Already in effect; nothing to apply.", "muted");
    else {
      pending = { settings, confirm: found.confirm };
      say("Review the preview, then confirm or cancel. Nothing has changed yet.", "muted");
    }
  } catch (error) {
    document.getElementById("preview").textContent = "No preview.";
    say(`Refused: ${error.message}`, "bad");
  }
  actions();
}

async function applyChange() {
  const sent = pending;
  pending = null;
  actions();
  try {
    await call("POST", "/api/settings/apply", { team: team(), settings: sent.settings, confirm: sent.confirm });
    say("Applied.", "ok");
    await load();
  } catch (error) {
    say(`Not applied: ${error.message}`, "bad");
  }
}

// Cancel sends nothing: the token is dropped and the form goes back to the settings now.
function cancelChange() {
  pending = null;
  fill(current);
  document.getElementById("preview").textContent = "Cancelled; nothing was changed.";
  say("Cancelled.", "muted");
  actions();
}

async function load() {
  try {
    current = (await call("GET", `/api/settings?team=${encodeURIComponent(team())}`)).settings;
    fill(current);
    document.getElementById("preview").textContent = "No preview yet.";
  } catch (error) {
    current = null;
    say(`Cannot read this team's settings: ${error.message}`, "bad");
  }
  actions();
}

async function start() {
  build();
  actions();
  try {
    const found = await call("GET", "/api/status");
    const select = document.getElementById("team");
    found.teams.forEach((t) => {
      const option = el("option", t.name);
      option.value = t.name;
      select.append(option);
    });
    select.addEventListener("change", load);
    await load();
  } catch (error) {
    say(`Cannot read the dashboard: ${error.message}`, "bad");
  }
}

start();
