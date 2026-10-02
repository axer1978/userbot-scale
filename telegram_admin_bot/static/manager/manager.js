"use strict";

/* The moderator panel (manager_api.py). A manager looks after the clients'
   bots while the admin is away: pause or resume a bot (manual and anomaly
   holds only), read conversations, pause one chat, acknowledge alerts and
   review client sign-ups. Every action asks for a reason, which the admin
   sees in the audit log. Nothing here can send a message or change a config.

   Built for a strict Content-Security-Policy (script-src 'self', no inline
   styles): every node is made with createElement and filled with
   textContent, never innerHTML, and styles come from manager.css. */

const $ = (id) => document.getElementById(id);

const st = {
  me: null,            // GET /api/manager/account
  overview: null,      // GET /api/manager/overview
  tab: "clients",      // clients | signups | alerts
  client: null,        // tenant id whose conversations are open, or null
  chat: null,          // chat id open in that client, or null
  convs: [],           // that client's conversations
  alertsAll: false,    // false: open alerts only
  seq: 0,              // drops a slow answer that arrives after a newer view was asked for
  threadSeq: 0,
};

const TAB_KEY = "manager.tab";
const TABS = ["clients", "signups", "alerts"];
const CHANGE_PASSWORD = "change_password";
const SETUP_TOTP = "setup_totp";
const CODE_REQUIRED = "code_required";
const MIN_PASSWORD = 10;
const NEED_REASON = "Give a reason: the admin sees it in the audit log.";
const NEED_REASON_APPLICANT = "Give a reason: the applicant sees it.";

const CHANNELS = { telegram: "Telegram", whatsapp: "WhatsApp" };
const HEALTH = {
  ok: ["connected", "good"], unknown: ["No report yet", "meh"],
  not_running: ["Not running", "bad"], disconnected: ["Not connected", "bad"],
  logged_out: ["Logged out", "bad"], rate_limited: ["Slowed down", "meh"],
  revoked: ["Switched off", "bad"], stopped: ["Stopped", "bad"],
};
const MESSAGE_STATUS = {
  received: "received", sent: "sent", pending_approval: "draft, waiting for approval",
  rejected: "rejected draft", error: "error", note: "note",
};
const CLIENT_STATUS = {
  pending: ["Waiting for approval", "meh"], active: ["Approved", "good"], rejected: ["Rejected", "bad"],
};

/* ---------------------------------------------------------------- utils */

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

function button(label, className, onClick) {
  const b = el("button", "btn " + (className || ""), label);
  b.type = "button";
  if (onClick) b.addEventListener("click", onClick);
  return b;
}

function toast(text, kind) {
  const node = el("div", "toast" + (kind === "info" ? " info" : ""), text);
  $("toasts").appendChild(node);
  setTimeout(() => node.remove(), kind === "info" ? 3500 : 8000);
}

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    credentials: "same-origin",
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = res.statusText || "Request failed";
    try {
      const data = await res.json();
      if (data && data.detail) detail = typeof data.detail === "string" ? data.detail : "Please check the form.";
    } catch (_) {}
    if (res.status === 429 && detail === (res.statusText || "Request failed")) {
      detail = "Too many attempts. Please wait a few minutes.";
    }
    const err = new Error(detail);
    err.status = res.status;
    err.detail = detail;
    throw err;
  }
  return res.status === 204 ? null : res.json();
}

function withBusy(btn, fn) {
  return async (...args) => {
    if (btn.disabled) return;
    btn.disabled = true;
    try { await fn(...args); } finally { btn.disabled = false; }
  };
}

function readTab() {
  try { return localStorage.getItem(TAB_KEY); } catch (_) { return null; }
}

function writeTab(value) {
  try { localStorage.setItem(TAB_KEY, value); } catch (_) {}
}

// Dates in the moderator's own timezone: clients may be anywhere.
function fmtWhen(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return "";
  const sameYear = d.getFullYear() === new Date().getFullYear();
  return d.toLocaleString([], Object.assign(
    { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }, sameYear ? {} : { year: "numeric" }));
}

function pill(text, kind, title) {
  const p = el("span", "pill " + (kind || ""));
  p.appendChild(el("span", "dot"));
  p.appendChild(el("span", "txt", text));
  if (title) p.title = title;
  return p;
}

function section(title, extra) {
  const s = el("section", "section");
  const head = el("div", "section-head");
  head.appendChild(el("h2", null, title));
  if (extra) head.appendChild(extra);
  s.appendChild(head);
  return s;
}

function kv(pairs) {
  const dl = el("dl", "kv");
  for (const [k, v] of pairs) {
    if (v === undefined || v === null || v === "") continue;
    dl.append(el("dt", null, k), el("dd", null, v));
  }
  return dl;
}

function actorName(actor) {
  return actor ? String(actor).replace(/^(manager|owner):/, "") : "";
}

/* -------------------------------------------------------------- screens */

function show(id) {
  for (const s of ["screen-login", "screen-change", "screen-totp", "screen-app"]) $(s).hidden = s !== id;
}

function notice(id, text, kind) {
  const n = $(id);
  n.textContent = text || "";
  n.className = "notice" + (kind === "info" ? " info" : "");
  n.hidden = !text;
}

function resetState() {
  st.me = null;
  st.overview = null;
  st.client = null;
  st.chat = null;
  st.convs = [];
  st.seq++;
  st.threadSeq++;
}

function showLogin(message) {
  resetState();
  show("screen-login");
  notice("login-notice", message || "");
  $("login-code-field").hidden = true;
  $("login-code").value = "";
  $("login-username").focus();
}

function showChange(message) {
  show("screen-change");
  notice("change-notice", message || "");
  for (const id of ["change-current", "change-new", "change-repeat"]) $(id).value = "";
  $("change-current").focus();
}

function showTotp(message) {
  show("screen-totp");
  notice("totp-notice", message || "");
  startTotp();
}

// A 401 or a pending gate can come back from any call: the session ran
// out, the admin disabled the login or reset its password or authenticator.
function authProblem(err) {
  if (!err) return false;
  if (err.status === 401) { showLogin("You were signed out. Please sign in again."); return true; }
  if (err.status === 403 && err.detail === CHANGE_PASSWORD) { showChange(); return true; }
  if (err.status === 403 && err.detail === SETUP_TOTP) { showTotp(); return true; }
  return false;
}

function routeGate(gate) {
  if (gate === CHANGE_PASSWORD) showChange();
  else if (gate === SETUP_TOTP) showTotp();
  else startApp();
}

async function boot() {
  try {
    st.me = await api("GET", "/api/manager/account");
  } catch (err) {
    if (err.status === 401) showLogin();
    else if (!authProblem(err)) showLogin("Could not reach the server: " + err.message);
    return;
  }
  routeGate(st.me.gate);
}

/* ---------------------------------------------------------------- login */

$("login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const btn = ev.target.querySelector("button[type=submit]");
  btn.disabled = true;
  try {
    await api("POST", "/api/manager/login", {
      username: $("login-username").value.trim(),
      password: $("login-password").value,
      code: $("login-code").value.trim(),
    });
    $("login-password").value = "";
    $("login-code").value = "";
    notice("login-notice", "");
    await boot();
  } catch (err) {
    if (err.detail === CODE_REQUIRED) {
      $("login-code-field").hidden = false;
      notice("login-notice", "Enter the 6-digit code from your authenticator app.", "info");
      $("login-code").focus();
    } else {
      notice("login-notice", err.message);
      if (!$("login-code-field").hidden) { $("login-code").value = ""; $("login-code").focus(); }
    }
  } finally {
    btn.disabled = false;
  }
});

$("change-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const current = $("change-current").value;
  const next = $("change-new").value;
  if (next.length < MIN_PASSWORD) { notice("change-notice", `At least ${MIN_PASSWORD} characters, please.`); return; }
  if (next !== $("change-repeat").value) { notice("change-notice", "The two new passwords are not the same."); return; }
  const btn = ev.target.querySelector("button[type=submit]");
  btn.disabled = true;
  try {
    await api("POST", "/api/manager/password", { current, new: next });
    for (const id of ["change-current", "change-new", "change-repeat"]) $(id).value = "";
    toast("Password saved.", "info");
    await boot();
  } catch (err) {
    if (err.status === 401) showLogin("You were signed out. Please sign in again.");
    else notice("change-notice", err.message);
  } finally {
    btn.disabled = false;
  }
});

async function logout() {
  try { await api("POST", "/api/manager/logout"); } catch (_) {}
  showLogin("You are signed out.");
}

$("change-logout").addEventListener("click", logout);
$("totp-logout").addEventListener("click", logout);
$("logout").addEventListener("click", logout);

/* ------------------------------------------------- authenticator setup */

function groupSecret(secret) {
  return secret.replace(/(.{4})/g, "$1 ").trim();
}

function codeField(id) {
  const label = el("label", "field", "Code from the app");
  const input = el("input");
  input.id = id;
  input.type = "text";
  input.inputMode = "numeric";
  input.autocomplete = "one-time-code";
  input.maxLength = 7;
  input.placeholder = "6 digits";
  input.required = true;
  label.appendChild(input);
  return [label, input];
}

// Mandatory: there is no way past this screen but a confirmed code (or
// signing out). The server keeps the new key for ten minutes.
async function startTotp() {
  const box = clear($("totp-box"));
  box.appendChild(el("div", "loading", "Getting a key…"));
  let setup;
  try {
    setup = await api("POST", "/api/manager/totp", { code: "" });
  } catch (err) {
    if (err.status === 409) { await boot(); return; }   // already set up
    if (err.status === 403 && err.detail === CHANGE_PASSWORD) { showChange(); return; }
    if (err.status === 401) { showLogin("You were signed out. Please sign in again."); return; }
    clear(box);
    notice("totp-notice", "Could not start the setup: " + err.message);
    box.appendChild(button("Try again", "primary block", startTotp));
    return;
  }
  clear(box);
  box.appendChild(el("p", "hint", "1. In your authenticator app, add an account and type in this key:"));
  box.appendChild(el("div", "secret key", groupSecret(setup.secret)));
  box.appendChild(el("p", "hint", "Or, on the phone that has the app, use this setup link:"));
  box.appendChild(el("div", "secret", setup.uri));
  box.appendChild(el("p", "hint", "2. Enter the code the app shows now:"));
  const form = el("form");
  form.noValidate = true;
  const [label, input] = codeField("totp-code");
  const confirm = el("button", "btn primary block", "Confirm and continue");
  confirm.type = "submit";
  form.append(label, confirm);
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const code = input.value.trim();
    if (!code) { notice("totp-notice", "Enter the 6-digit code from the app."); input.focus(); return; }
    if (confirm.disabled) return;
    confirm.disabled = true;
    try {
      await api("POST", "/api/manager/totp", { code });
      notice("totp-notice", "");
      toast("Authenticator set up. Signing in will ask for a code from now on.", "info");
      await boot();
    } catch (err) {
      if (err.status === 409) { await boot(); return; }
      if (err.status === 401) { showLogin("You were signed out. Please sign in again."); return; }
      if (err.status === 403 && err.detail === CHANGE_PASSWORD) { showChange(); return; }
      notice("totp-notice", err.message);
      input.value = "";
      input.focus();
    } finally {
      confirm.disabled = false;
    }
  });
  box.appendChild(form);
  box.appendChild(button("Get a new key", "ghost block", () => { notice("totp-notice", ""); startTotp(); }));
  input.focus();
}

/* ------------------------------------------------------- reason form */

// An inline "why?" form opened in `slot` under a button. Empty reasons are
// refused here; the server's own refusal shows in the form.
function askReason(slot, opts) {
  if (slot.dataset.open === opts.key) { close(); return; }
  clear(slot);
  slot.dataset.open = opts.key;
  const form = el("form", "reason-form");
  form.noValidate = true;
  const label = el("label", "field", opts.label);
  const input = el("input");
  input.type = "text";
  input.maxLength = 500;
  input.autocomplete = "off";
  input.placeholder = opts.placeholder || "";
  label.appendChild(input);
  form.appendChild(label);
  const err = el("p", "err");
  err.hidden = true;
  err.setAttribute("role", "alert");
  const row = el("div", "row");
  const ok = el("button", "btn small " + (opts.danger ? "danger solid" : "primary"), opts.submit);
  ok.type = "submit";
  const cancel = button("Cancel", "small ghost", close);
  row.append(ok, cancel);
  form.append(err, row);

  function close() {
    clear(slot);
    delete slot.dataset.open;
  }
  function showErr(text) {
    err.textContent = text;
    err.hidden = !text;
  }

  input.addEventListener("keydown", (ev) => { if (ev.key === "Escape") close(); });
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const reason = input.value.trim();
    if (!reason) { showErr(opts.emptyText || NEED_REASON); input.focus(); return; }
    if (ok.disabled) return;
    ok.disabled = true;
    showErr("");
    try {
      await opts.onSubmit(reason);
      close();
    } catch (e) {
      if (authProblem(e)) return;
      showErr(e.message);
      if (e.status === 403) toast(e.message);
    } finally {
      ok.disabled = false;
    }
  });
  slot.appendChild(form);
  input.focus();
}

/* ------------------------------------------------------------ the panel */

function startApp() {
  show("screen-app");
  $("app-user").textContent = st.me ? (st.me.display_name || st.me.username) : "";
  const saved = readTab();
  st.tab = TABS.includes(saved) ? saved : "clients";
  st.client = null;
  st.chat = null;
  refresh();
}

async function loadOverview() {
  st.overview = await api("GET", "/api/manager/overview");
  renderChrome();
}

function renderChrome() {
  const o = st.overview;
  if (!o) return;
  if (o.me) $("app-user").textContent = o.me.display_name || o.me.username;
  const banner = clear($("global-stop"));
  const stop = o.global_stop || {};
  banner.hidden = !stop.on;
  if (stop.on) {
    banner.appendChild(document.createTextNode("Global stop is ON (set by the admin) — no bot sends anything"));
    const why = [stop.reason, stop.at ? "since " + fmtWhen(stop.at) : ""].filter(Boolean).join(" · ");
    if (why) banner.appendChild(el("span", "why", why));
  }
  setBadge("badge-clients", o.tenants.length, "");
  setBadge("badge-signups", o.pending_signups, "warm");
  const alerts = o.alerts || { total: 0, critical: 0 };
  setBadge("badge-alerts", alerts.total, alerts.critical > 0 ? "hot" : "warm");
  for (const tab of document.querySelectorAll(".tab")) {
    const on = tab.dataset.tab === st.tab;
    tab.classList.toggle("on", on);
    if (on) tab.setAttribute("aria-current", "page"); else tab.removeAttribute("aria-current");
  }
}

function setBadge(id, count, kind) {
  const b = $(id);
  b.textContent = String(count || 0);
  b.className = "badge" + (count > 0 && kind ? " " + kind : "");
  b.hidden = !count;
}

async function refresh() {
  const box = $("content");
  if (!st.overview) clear(box).appendChild(el("div", "loading", "Loading…"));
  try {
    await loadOverview();
  } catch (err) {
    if (authProblem(err)) return;
    clear(box).appendChild(el("div", "error-box", "Could not load: " + err.message));
    return;
  }
  render();
}

// After an action: fresh counts and banner, without redrawing the view.
async function refreshQuiet() {
  try { await loadOverview(); } catch (err) { authProblem(err); }
}

$("refresh").addEventListener("click", withBusy($("refresh"), refresh));

for (const tab of document.querySelectorAll(".tab")) {
  tab.addEventListener("click", () => {
    st.tab = tab.dataset.tab;
    writeTab(st.tab);
    st.client = null;
    st.chat = null;
    renderChrome();
    window.scrollTo(0, 0);
    render();
  });
}

function render() {
  renderChrome();
  const box = clear($("content"));
  const seq = ++st.seq;
  st.threadSeq++;
  if (st.tab === "signups") renderSignups(box, seq);
  else if (st.tab === "alerts") renderAlerts(box, seq);
  else if (st.client !== null) renderClient(box, seq);
  else renderClients(box);
}

function tenantById(id) {
  return st.overview ? st.overview.tenants.find((t) => t.id === id) || null : null;
}

/* -------------------------------------------------------------- clients */

function channelName(channel) {
  return CHANNELS[channel] || channel || "—";
}

function statePills(t) {
  const line = el("div", "pills");
  const stop = st.overview && st.overview.global_stop && st.overview.global_stop.on;
  if (t.holds.length) line.appendChild(pill("Bot is paused", "bad"));
  else if (stop) line.appendChild(pill("Stopped by the global stop", "bad"));
  else line.appendChild(pill("Bot is answering", "good"));
  const health = t.health || {};
  const h = HEALTH[health.status] || [health.status || "unknown", "meh"];
  const label = health.status === "ok" ? channelName(t.channel) + " " + h[0] : h[0];
  line.appendChild(pill(label, h[1], health.reason || ""));
  if (t.billing && t.billing !== "active") {
    line.appendChild(pill("Billing: " + t.billing, t.billing === "suspended" ? "bad" : "meh"));
  }
  if (!t.session_state) line.appendChild(pill("No messaging account", "meh"));
  return line;
}

function renderClients(box) {
  const tenants = st.overview.tenants;
  const s = section("Clients", el("span", "muted small", tenants.length + (tenants.length === 1 ? " client" : " clients")));
  s.appendChild(el("p", "hint", "Pausing a bot keeps receiving and storing messages; nothing is sent until the hold " +
    "is lifted. You can lift a pause or an anomaly hold; billing, AI-limit and connection holds stay with the admin."));
  if (!tenants.length) {
    s.appendChild(el("div", "empty", "No clients yet."));
  } else {
    const grid = el("div", "grid");
    for (const t of tenants) grid.appendChild(tenantCard(t));
    s.appendChild(grid);
  }
  box.appendChild(s);
}

function applyControls(t, res) {
  t.holds = res.holds || [];
  if (st.overview && res.global_stop) st.overview.global_stop = res.global_stop;
  renderChrome();
}

function tenantCard(t) {
  const card = el("div", "item" + (t.holds.length ? " paused" : ""));
  const top = el("div", "top");
  top.appendChild(el("span", "title", t.name));
  top.appendChild(el("span", "chan", channelName(t.channel)));
  card.appendChild(top);
  card.appendChild(statePills(t));

  const health = t.health || {};
  const meta = [];
  if (health.reason) meta.push(health.reason);
  if (health.last_seen_at) meta.push("last report " + fmtWhen(health.last_seen_at));
  if (meta.length) card.appendChild(el("div", "meta", meta.join(" · ")));

  if (t.holds.length) {
    const list = el("ul", "holds");
    for (const h of t.holds) list.appendChild(holdItem(t, card, h));
    card.appendChild(list);
  }

  const nums = el("div", "nums");
  for (const [value, label] of [[t.open_alerts, "open alerts"], [t.unanswered_open, "unanswered"]]) {
    const cell = el("div");
    cell.appendChild(el("b", value > 0 ? "alert" : null, value));
    cell.appendChild(el("span", null, label));
    nums.appendChild(cell);
  }
  card.appendChild(nums);

  const actions = el("div", "actions");
  const slot = el("div");
  if (!t.holds.some((h) => h.kind === "manual")) {
    actions.appendChild(button("Pause bot", "small danger", () => askReason(slot, {
      key: "pause", label: "Why pause this bot? The admin sees it.", submit: "Pause bot", danger: true,
      placeholder: "e.g. the client asked to stop for today",
      onSubmit: async (reason) => {
        const res = await api("POST", `/api/manager/tenants/${encodeURIComponent(t.id)}/pause`, { reason });
        applyControls(t, res);
        card.replaceWith(tenantCard(t));
        toast(`${t.name}: bot paused.`, "info");
      },
    })));
  }
  if (t.session_state) actions.appendChild(button("Conversations", "small", () => openClient(t.id)));
  card.appendChild(actions);
  card.appendChild(slot);
  return card;
}

function holdItem(t, card, h) {
  const li = el("li", "hold");
  const body = el("div", "body");
  body.appendChild(el("div", "label", h.label || h.kind));
  if (h.reason) body.appendChild(el("div", "reason", h.reason));
  if (h.created_at) body.appendChild(el("div", "when", "since " + fmtWhen(h.created_at)));
  li.appendChild(body);
  const slot = el("div", "slot");
  if (h.resumable) {
    li.appendChild(button("Lift", "small", () => askReason(slot, {
      key: "lift", label: `Why lift "${h.label || h.kind}"? The admin sees it.`, submit: "Lift hold",
      placeholder: "e.g. checked with the client, all fine",
      onSubmit: async (reason) => {
        const res = await api("POST", `/api/manager/tenants/${encodeURIComponent(t.id)}/resume`,
          { kind: h.kind, reason });
        applyControls(t, res);
        card.replaceWith(tenantCard(t));
        refreshQuiet();
        toast(`${t.name}: "${h.label || h.kind}" lifted.`, "info");
      },
    })));
  } else {
    li.appendChild(el("span", "muted small", "Only the admin can lift this."));
  }
  li.appendChild(slot);
  return li;
}

/* ----------------------------------------------------- one client's chats */

const cv = { split: null, listPane: null, threadPane: null, tenant: null };

function openClient(id) {
  st.client = id;
  st.chat = null;
  window.scrollTo(0, 0);
  render();
}

function backToClients() {
  st.client = null;
  st.chat = null;
  window.scrollTo(0, 0);
  render();
}

function convName(c) {
  return c.display_name || (c.username ? "@" + c.username : "Chat " + c.chat_id);
}

function convSub(c) {
  const parts = [];
  if (c.username && c.display_name) parts.push("@" + c.username);
  parts.push("id " + c.chat_id);
  return parts.join(" · ");
}

function takeoverOn(c) {
  return c.human_takeover_until && new Date(c.human_takeover_until) > new Date();
}

function convPills(c) {
  const line = el("div", "pills");
  if (c.automation_paused) line.appendChild(pill("Bot paused here", "bad", c.paused_reason || ""));
  if (takeoverOn(c)) line.appendChild(pill("A person is answering until " + fmtWhen(c.human_takeover_until), "meh"));
  if (c.is_bot) line.appendChild(pill("Bot account", ""));
  if (c.unread > 0) line.appendChild(pill(c.unread + " unread", ""));
  return line;
}

async function renderClient(box, seq) {
  const t = tenantById(st.client);
  if (!t) { st.client = null; renderClients(box); return; }
  cv.tenant = t;
  box.appendChild(button("Back to all clients", "small ghost back", backToClients));
  const head = el("div", "client-head");
  const top = el("div", "section-head");
  top.appendChild(el("h1", null, t.name));
  top.appendChild(el("span", "chan", channelName(t.channel)));
  head.appendChild(top);
  head.appendChild(statePills(t));
  box.appendChild(head);

  cv.split = el("div", "split" + (st.chat !== null ? " show-thread" : ""));
  cv.listPane = el("div", "pane-list");
  cv.threadPane = el("div", "pane-thread");
  cv.split.append(cv.listPane, cv.threadPane);
  box.appendChild(cv.split);
  cv.listPane.appendChild(el("div", "loading", "Loading…"));
  threadPlaceholder();

  let convs;
  try {
    convs = await api("GET", `/api/manager/tenants/${encodeURIComponent(t.id)}/conversations`);
  } catch (err) {
    if (authProblem(err) || seq !== st.seq) return;
    clear(cv.listPane).appendChild(el("div", "error-box", "Could not load the conversations: " + err.message));
    return;
  }
  if (seq !== st.seq) return;
  st.convs = convs;
  renderConvList();
  if (st.chat !== null) loadThread(st.chat);
}

function threadPlaceholder() {
  clear(cv.threadPane).appendChild(el("div", "empty", "Pick a conversation to read it."));
}

function renderConvList() {
  const pane = clear(cv.listPane);
  const head = el("div", "section-head");
  head.appendChild(el("h2", null, "Conversations"));
  head.appendChild(el("span", "muted small", String(st.convs.length)));
  pane.appendChild(head);
  if (!st.convs.length) {
    pane.appendChild(el("div", "empty", "No conversations yet."));
    return;
  }
  const list = el("div", "list");
  for (const c of st.convs) {
    const item = el("button", "conv" + (c.chat_id === st.chat ? " active" : ""));
    item.type = "button";
    const top = el("div", "top");
    top.appendChild(el("span", "name", convName(c)));
    if (c.last_message_at) top.appendChild(el("span", "when", fmtWhen(c.last_message_at)));
    item.appendChild(top);
    item.appendChild(el("div", "sub", c.last_message_preview || convSub(c)));
    const pills = convPills(c);
    if (pills.childNodes.length) item.appendChild(pills);
    item.addEventListener("click", () => selectChat(c.chat_id));
    list.appendChild(item);
  }
  pane.appendChild(list);
}

function selectChat(chatId) {
  st.chat = chatId;
  cv.split.classList.add("show-thread");
  renderConvList();
  if (window.matchMedia("(max-width: 959px)").matches) window.scrollTo(0, 0);
  loadThread(chatId);
}

function closeChat() {
  st.chat = null;
  st.threadSeq++;
  cv.split.classList.remove("show-thread");
  renderConvList();
  threadPlaceholder();
}

async function loadThread(chatId) {
  const t = cv.tenant;
  const seq = ++st.threadSeq;
  const pane = clear(cv.threadPane);
  pane.appendChild(button("Back to conversations", "small ghost back mobile-only", closeChat));
  pane.appendChild(el("div", "loading", "Loading…"));
  let data;
  try {
    data = await api("GET",
      `/api/manager/tenants/${encodeURIComponent(t.id)}/conversations/${encodeURIComponent(chatId)}/messages`);
  } catch (err) {
    if (authProblem(err) || seq !== st.threadSeq) return;
    pane.lastChild.replaceWith(el("div", "error-box", "Could not load this conversation: " + err.message));
    return;
  }
  if (seq !== st.threadSeq) return;
  pane.lastChild.remove();

  // After a pause or resume: redraw the head and the list entry.
  let headNode = null;
  const onChanged = (conv) => {
    const i = st.convs.findIndex((c) => c.chat_id === conv.chat_id);
    if (i >= 0) st.convs[i] = Object.assign({}, st.convs[i], conv);
    renderConvList();
    const fresh = threadHead(t, conv, onChanged);
    headNode.replaceWith(fresh);
    headNode = fresh;
  };
  headNode = threadHead(t, data.conversation, onChanged);
  pane.appendChild(headNode);

  const thread = el("div", "thread");
  if (!data.messages.length) thread.appendChild(el("div", "empty", "No messages stored for this chat."));
  let last = null;
  for (const m of data.messages) {
    last = messageNode(m);
    thread.appendChild(last);
  }
  pane.appendChild(thread);
  thread.scrollTop = thread.scrollHeight;
  if (last && window.matchMedia("(max-width: 959px)").matches) last.scrollIntoView({ block: "end" });
}

function threadHead(t, conv, onChanged) {
  const head = el("div", "thread-head");
  const top = el("div", "top");
  top.appendChild(el("span", "title", convName(conv)));
  const slot = el("div");
  const paused = conv.automation_paused;
  if (onChanged) {
    top.appendChild(button(paused ? "Resume this chat" : "Pause this chat", "small " + (paused ? "" : "danger"),
      () => askReason(slot, {
        key: "chat", submit: paused ? "Resume this chat" : "Pause this chat", danger: !paused,
        label: (paused ? "Why let the bot answer here again?" : "Why stop the bot in this chat?") +
          " The admin sees it.",
        onSubmit: async (reason) => {
          const updated = await api("POST",
            `/api/manager/tenants/${encodeURIComponent(t.id)}/conversations/${encodeURIComponent(conv.chat_id)}/pause`,
            { paused: !paused, reason });
          toast(updated.automation_paused ? "Chat paused: the bot stays quiet here." :
            "Chat resumed: the bot answers here again.", "info");
          onChanged(updated);
        },
      })));
  }
  head.appendChild(top);
  head.appendChild(el("div", "meta muted small", convSub(conv)));
  const pills = convPills(conv);
  if (conv.automation_paused && conv.paused_reason) {
    pills.appendChild(el("span", "muted small", "Reason: " + conv.paused_reason));
  }
  if (pills.childNodes.length) head.appendChild(pills);
  head.appendChild(el("div", "readonly",
    "Read-only: moderators can't send messages or approve drafts."));
  head.appendChild(slot);
  return head;
}

function messageNode(m) {
  let cls = "msg";
  if (m.status === "note" || m.direction === "system") cls += " note";
  else if (m.status === "error") cls += " error";
  else if (m.direction === "in") cls += " in";
  else {
    cls += " out";
    if (m.status === "pending_approval") cls += " draft";
    if (m.status === "rejected") cls += " rejected";
  }
  const node = el("div", cls);
  const text = m.text || "";
  node.appendChild(el("div", "text" + (text ? "" : " muted"), text || "(no text)"));
  const meta = [fmtWhen(m.created_at)];
  if (m.direction === "in") meta.push("customer");
  else if (m.direction === "out") meta.push(MESSAGE_STATUS[m.status] || m.status);
  else if (m.status && m.status !== "note") meta.push(MESSAGE_STATUS[m.status] || m.status);
  const att = (m.attachments || []).length;
  if (att) meta.push(att === 1 ? "1 attachment" : att + " attachments");
  node.appendChild(el("div", "meta", meta.filter(Boolean).join(" · ")));
  return node;
}

/* ------------------------------------------------------------- sign-ups */

async function renderSignups(box, seq) {
  box.appendChild(el("p", "hint", "Approving opens the account. Linking it to a business is done by the admin."));
  const loading = el("div", "loading", "Loading…");
  box.appendChild(loading);
  let clients;
  try {
    clients = await api("GET", "/api/manager/clients");
  } catch (err) {
    if (authProblem(err) || seq !== st.seq) return;
    loading.replaceWith(el("div", "error-box", "Could not load the client logins: " + err.message));
    return;
  }
  if (seq !== st.seq) return;
  loading.remove();

  const pending = clients.filter((c) => c.status === "pending");
  const others = clients.filter((c) => c.status !== "pending");

  const s1 = section("Waiting for approval", el("span", "muted small", String(pending.length)));
  if (!pending.length) s1.appendChild(el("div", "empty", "No sign-ups waiting."));
  else {
    const grid = el("div", "grid two");
    for (const c of pending) grid.appendChild(signupCard(c));
    s1.appendChild(grid);
  }
  box.appendChild(s1);

  const s2 = section("All client logins", el("span", "muted small", String(others.length)));
  if (!others.length) s2.appendChild(el("div", "empty", "No other client logins."));
  else {
    const grid = el("div", "grid two");
    for (const c of others) grid.appendChild(clientCard(c));
    s2.appendChild(grid);
  }
  box.appendChild(s2);
}

async function afterReview() {
  await refreshQuiet();
  if (st.tab === "signups") render();
}

function signupCard(c) {
  const card = el("div", "item");
  const top = el("div", "top");
  top.appendChild(el("span", "title", c.display_name || c.username));
  if (c.created_at) top.appendChild(el("span", "when", fmtWhen(c.created_at)));
  card.appendChild(top);
  card.appendChild(kv([
    ["Username", c.username], ["Name", c.display_name], ["Email", c.email], ["Company", c.company],
    ["Phone", c.phone], ["Signed up", fmtWhen(c.created_at)],
    ["Terms", c.terms_accepted ? "accepted (version " + c.terms_accepted + ")" : "not accepted"],
  ]));
  const actions = el("div", "actions");
  const slot = el("div");
  actions.appendChild(button("Approve", "small primary", () => askReason(slot, {
    key: "approve", label: "Why approve? The admin sees it.", submit: "Approve",
    placeholder: "e.g. checked the company and the phone number",
    onSubmit: async (reason) => {
      await api("POST", `/api/manager/clients/${encodeURIComponent(c.id)}/approve`, { reason });
      toast(`${c.username} approved.`, "info");
      afterReview();
    },
  })));
  actions.appendChild(button("Reject", "small danger", () => askReason(slot, {
    key: "reject", label: "Why reject? The applicant sees this reason.", submit: "Reject", danger: true,
    emptyText: NEED_REASON_APPLICANT, placeholder: "e.g. we could not verify the business",
    onSubmit: async (reason) => {
      await api("POST", `/api/manager/clients/${encodeURIComponent(c.id)}/reject`, { reason });
      toast(`${c.username} rejected.`, "info");
      afterReview();
    },
  })));
  card.append(actions, slot);
  return card;
}

function clientCard(c) {
  const card = el("div", "item" + (c.disabled ? " done" : ""));
  const top = el("div", "top");
  top.appendChild(el("span", "title", c.display_name || c.username));
  card.appendChild(top);
  const pills = el("div", "pills");
  const s = CLIENT_STATUS[c.status] || [c.status, ""];
  pills.appendChild(pill(s[0], s[1]));
  if (c.disabled) pills.appendChild(pill("Disabled", "bad"));
  card.appendChild(pills);
  const tenants = (c.tenants || []).map((t) => t.name).join(", ");
  const review = c.review_reason
    ? c.review_reason + (c.reviewed_by ? " (" + actorName(c.reviewed_by) + ")" : "") : "";
  card.appendChild(kv([
    ["Username", c.username], ["Email", c.email], ["Company", c.company], ["Phone", c.phone],
    ["Businesses", tenants || "none linked yet"],
    ["Last sign-in", c.last_login_at ? fmtWhen(c.last_login_at) : "never"],
    ["Review", review],
  ]));
  const actions = el("div", "actions");
  const slot = el("div");
  const disabling = !c.disabled;
  actions.appendChild(button(disabling ? "Disable" : "Enable", "small" + (disabling ? " danger" : ""),
    () => askReason(slot, {
      key: "disabled", submit: disabling ? "Disable login" : "Enable login", danger: disabling,
      label: (disabling ? "Why disable this login? It is signed out everywhere at once." :
        "Why enable this login again?") + " The admin sees it.",
      onSubmit: async (reason) => {
        const row = await api("POST", `/api/manager/clients/${encodeURIComponent(c.id)}/disabled`,
          { disabled: disabling, reason });
        card.replaceWith(clientCard(row));
        toast(`${row.username} ${row.disabled ? "disabled" : "enabled"}.`, "info");
      },
    })));
  if (c.status === "rejected") {
    actions.appendChild(button("Approve after all", "small", () => askReason(slot, {
      key: "approve", label: "Why approve now? The admin sees it.", submit: "Approve",
      onSubmit: async (reason) => {
        await api("POST", `/api/manager/clients/${encodeURIComponent(c.id)}/approve`, { reason });
        toast(`${c.username} approved.`, "info");
        afterReview();
      },
    })));
  }
  card.append(actions, slot);
  return card;
}

/* --------------------------------------------------------------- alerts */

async function renderAlerts(box, seq) {
  const seg = el("div", "seg");
  for (const [all, label] of [[false, "Open"], [true, "All"]]) {
    const b = el("button", st.alertsAll === all ? "on" : "", label);
    b.type = "button";
    b.addEventListener("click", () => {
      if (st.alertsAll === all) return;
      st.alertsAll = all;
      render();
    });
    seg.appendChild(b);
  }
  const s = section("Alerts", seg);
  const listBox = el("div", "list");
  listBox.appendChild(el("div", "loading", "Loading…"));
  s.appendChild(listBox);
  box.appendChild(s);

  let alerts;
  try {
    alerts = await api("GET", `/api/manager/alerts?open=${st.alertsAll ? "false" : "true"}`);
  } catch (err) {
    if (authProblem(err) || seq !== st.seq) return;
    clear(listBox).appendChild(el("div", "error-box", "Could not load the alerts: " + err.message));
    return;
  }
  if (seq !== st.seq) return;
  clear(listBox);
  if (!alerts.length) {
    listBox.appendChild(el("div", "empty", st.alertsAll ? "No alerts." : "No open alerts. All quiet."));
    return;
  }
  for (const a of alerts) listBox.appendChild(alertCard(a, listBox));
}

function tenantName(id) {
  if (id === null || id === undefined) return "Platform";
  const t = tenantById(id);
  return t ? t.name : "Client #" + id;
}

function alertCard(a, listBox) {
  const done = !!a.acknowledged_at;
  const card = el("div", "item" + (done ? " done" : ""));
  const top = el("div", "top");
  top.appendChild(el("span", "sev " + (a.severity || ""), a.severity || "alert"));
  top.appendChild(el("span", "title", tenantName(a.tenant_id)));
  top.appendChild(el("span", "when", fmtWhen(a.last_at || a.created_at)));
  card.appendChild(top);
  card.appendChild(el("div", "kind", a.kind));
  card.appendChild(el("div", "text", a.message));
  const meta = [];
  if (a.count > 1) meta.push(`${a.count} times since ${fmtWhen(a.created_at)}`);
  if (done) {
    meta.push("Acknowledged" + (a.acknowledged_by ? " by " + actorName(a.acknowledged_by) : "") +
      ", " + fmtWhen(a.acknowledged_at));
  }
  if (meta.length) card.appendChild(el("div", "meta", meta.join(" · ")));
  if (!done) {
    const actions = el("div", "actions");
    const b = button("Acknowledge", "small primary");
    b.addEventListener("click", withBusy(b, async () => {
      let row;
      try {
        row = await api("POST", `/api/manager/alerts/${encodeURIComponent(a.id)}/ack`);
      } catch (err) {
        if (!authProblem(err)) toast(err.message);
        return;
      }
      countAck(a);
      if (st.alertsAll) card.replaceWith(alertCard(row, listBox));
      else {
        card.remove();
        if (!listBox.childNodes.length) listBox.appendChild(el("div", "empty", "No open alerts. All quiet."));
      }
      toast("Alert acknowledged.", "info");
    }));
    actions.appendChild(b);
    card.appendChild(actions);
  }
  return card;
}

function countAck(a) {
  const o = st.overview;
  if (!o) return;
  if (o.alerts) {
    o.alerts.total = Math.max(0, (o.alerts.total || 0) - 1);
    if (a.severity === "critical") o.alerts.critical = Math.max(0, (o.alerts.critical || 0) - 1);
  }
  const t = tenantById(a.tenant_id);
  if (t) t.open_alerts = Math.max(0, (t.open_alerts || 0) - 1);
  renderChrome();
}

boot();
