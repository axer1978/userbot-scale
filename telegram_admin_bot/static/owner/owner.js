"use strict";

/* The client dashboard (owner_api.py). Read-only apart from "mark reviewed"
   and the owner's own password and authenticator: nothing here can change
   how a bot behaves.

   Built for a strict Content-Security-Policy (script-src 'self', no inline
   styles): every node is made with createElement and filled with
   textContent, never innerHTML, and styles come from owner.css; the only
   style set from here is a bar's height, through the CSSOM. */

const $ = (id) => document.getElementById(id);

const st = {
  me: null,
  view: null,          // "all", or a tenant id
  chart: "bookings",   // or "messages"
  queue: "open",       // open | reviewed | all
  renderSeq: 0,        // drops a slow answer that arrives after a newer view was asked for
  signupOptions: null, // GET /api/owner/signup-options, once it answered
  terms: null,         // the terms version on the accept-terms screen
};

const VIEW_KEY = "owner.view";
const CHANGE_PASSWORD = "change_password";
const PENDING_APPROVAL = "pending_approval";
const REJECTED = "rejected";
const ACCEPT_TERMS = "accept_terms";
const GATES = [CHANGE_PASSWORD, PENDING_APPROVAL, REJECTED, ACCEPT_TERMS];
const SCREENS = ["screen-login", "screen-signup", "screen-change", "screen-pending", "screen-rejected",
                 "screen-terms", "screen-app"];
const CODE_REQUIRED = "code_required";
const MIN_PASSWORD = 10;

const BOOKING_STATES = {
  requested: "new request", pending: "waiting for you", confirmed: "confirmed",
  cancelled: "cancelled", no_show: "no-show", completed: "done",
};
const QUEUE_REASONS = {
  skipped: "Not answered (a reply limit or a no-reply rule)",
  ai_error: "The bot could not write a reply",
  soft_off: "The bot was switched off",
  paused: "The chat was paused or taken over by a person",
  escalated: "Passed on to you",
  policy_hold: "The reply was held back for a check",
  fallback: "The bot did not know the answer",
  staging: "Test mode: not answered",
};
const HEALTH = {
  ok: ["Telegram connected", "good"], unknown: ["No report yet", "meh"],
  not_running: ["Not running", "bad"], disconnected: ["Not connected", "bad"],
  logged_out: ["Logged out of Telegram", "bad"], rate_limited: ["Slowed down by Telegram", "meh"],
  revoked: ["Switched off", "bad"], stopped: ["Stopped", "bad"],
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
    const err = new Error(detail);
    err.status = res.status;
    err.detail = detail;
    throw err;
  }
  return res.status === 204 ? null : res.json();
}

function readView() {
  try { return localStorage.getItem(VIEW_KEY); } catch (_) { return null; }
}

function writeView(value) {
  try { localStorage.setItem(VIEW_KEY, String(value)); } catch (_) {}
}

// Dates in the business's own timezone, whatever the phone's.
function fmt(iso, tz, options) {
  const d = new Date(iso);
  if (isNaN(d)) return "";
  try { return d.toLocaleString([], Object.assign({}, options, { timeZone: tz })); }
  catch (_) { return d.toLocaleString([], options); }
}
const fmtTime = (iso, tz) => fmt(iso, tz, { hour: "2-digit", minute: "2-digit" });
const fmtDayKey = (iso, tz) => fmt(iso, tz, { year: "numeric", month: "2-digit", day: "2-digit" });
const fmtDay = (iso, tz) => fmt(iso, tz, { weekday: "long", day: "numeric", month: "long" });
const fmtShortDay = (iso, tz) => fmt(iso, tz, { day: "numeric", month: "short" });
const fmtWhen = (iso, tz) => fmt(iso, tz, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

const fmtDate = (iso) => fmt(iso, undefined, { day: "numeric", month: "long", year: "numeric" });

function withBusy(button, fn) {
  return async (...args) => {
    if (button.disabled) return;
    button.disabled = true;
    try { await fn(...args); } finally { button.disabled = false; }
  };
}

/* Terms text: "## " starts a heading, "- " a bullet (consecutive bullets
   share one list), a blank line ends the paragraph or list, and other
   consecutive lines join into one paragraph. Text only, never markup. */
function renderTerms(container, body) {
  let para = null;
  let list = null;
  const flush = () => {
    if (para) container.appendChild(el("p", null, para.join(" ")));
    para = null;
  };
  for (const raw of String(body || "").split("\n")) {
    const line = raw.replace(/\s+$/, "");
    if (!line.trim()) {
      flush();
      list = null;
    } else if (line.startsWith("## ")) {
      flush();
      list = null;
      container.appendChild(el("h2", null, line.slice(3).trim()));
    } else if (line.startsWith("- ")) {
      flush();
      if (!list) list = container.appendChild(el("ul"));
      list.appendChild(el("li", null, line.slice(2).trim()));
    } else {
      list = null;
      if (!para) para = [];
      para.push(line.trim());
    }
  }
  flush();
  return container;
}

function termsMeta(terms) {
  const date = fmtDate(terms.published_at);
  return `Version ${terms.version}` + (date ? `, published ${date}` : "");
}

/* -------------------------------------------------------------- screens */

function show(id) {
  for (const s of SCREENS) $(s).hidden = s !== id;
  if (id !== "screen-app") $("settings").hidden = true;
  if (id !== "screen-signup") hideSignupTerms();
}

function notice(id, text, kind) {
  const n = $(id);
  n.textContent = text || "";
  n.className = "notice" + (kind === "info" ? " info" : "");
  n.hidden = !text;
}

function showLogin(message) {
  st.me = null;
  show("screen-login");
  notice("login-notice", message || "");
  $("login-code-field").hidden = true;
  $("login-code").value = "";
  $("login-username").focus();
  loadSignupOptions().then(updateSignupLink);
}

function showChange(message) {
  show("screen-change");
  notice("change-notice", message || "");
  for (const id of ["change-current", "change-new", "change-repeat"]) $(id).value = "";
  $("change-current").focus();
}

function showPending(account, message) {
  show("screen-pending");
  const name = account.display_name || account.username || "";
  $("pending-text").textContent = (name ? `Thanks, ${name}. ` : "Thanks. ") +
    "Your account is waiting for approval. We check every new account before it opens. " +
    "Please come back later, or tap Refresh status.";
  notice("pending-notice", message || "", "info");
}

function showRejected(account) {
  show("screen-rejected");
  const reason = (account.review_reason || "").trim();
  $("rejected-reason").textContent = reason ? "Reason: " + reason : "";
  $("rejected-reason").hidden = !reason;
}

// What stands between a signed-in login and the dashboard: a password
// change, an approval, a rejection or new terms. Asks the server, which
// knows, and opens the matching screen.
async function routeGate(cause) {
  let account;
  try {
    account = await api("GET", "/api/owner/account");
  } catch (err) {
    if (err.status === 401) showLogin("You were signed out. Please sign in again.");
    else showLogin("Could not reach the server: " + err.message);
    return;
  }
  if (account.gate === CHANGE_PASSWORD) showChange();
  else if (account.gate === PENDING_APPROVAL) showPending(account);
  else if (account.gate === REJECTED) showRejected(account);
  else if (account.gate === ACCEPT_TERMS) await showTerms();
  else showLogin("Could not open the dashboard: " + (cause ? cause.message : "please try again."));
}

// A 401 or a gate can come back from any call: the session ran out, the
// admin disabled the login or reset its password, or new terms came out.
function authProblem(err) {
  if (err && err.status === 401) { showLogin("You were signed out. Please sign in again."); return true; }
  if (err && err.status === 403 && GATES.includes(err.detail)) { routeGate(err); return true; }
  return false;
}

async function boot() {
  try {
    st.me = await api("GET", "/api/owner/me");
  } catch (err) {
    if (err.status === 401) showLogin();
    else if (err.status === 403) await routeGate(err);
    else showLogin("Could not reach the server: " + err.message);
    return;
  }
  startApp();
}

/* -------------------------------------------------------------- sign up */

let signupOptionsLoading = null;

// Asked once; a failed answer is asked again next time. `force` re-reads
// it (the terms changed during a sign-up).
function loadSignupOptions(force) {
  if (st.signupOptions && !force) return Promise.resolve(st.signupOptions);
  if (!signupOptionsLoading) {
    signupOptionsLoading = api("GET", "/api/owner/signup-options")
      .then((o) => { st.signupOptions = o; return o; })
      .catch(() => st.signupOptions)
      .finally(() => { signupOptionsLoading = null; });
  }
  return signupOptionsLoading;
}

function signupAvailable() {
  const o = st.signupOptions;
  return Boolean(o && o.open && o.terms);
}

function updateSignupLink() {
  $("login-signup").hidden = !signupAvailable();
}

function applySignupTerms() {
  const terms = st.signupOptions && st.signupOptions.terms;
  $("signup-terms-link").textContent = terms && terms.title ? terms.title : "terms of service";
}

function showSignup() {
  show("screen-signup");
  notice("signup-notice", signupAvailable() ? "" : "Sign-up is not open right now. Please contact us for an account.");
  applySignupTerms();
  $("signup-name").focus();
}

function hideSignupTerms() {
  $("signup-terms-box").hidden = true;
  $("signup-read-terms").textContent = "Read the terms";
}

// The terms the person reads here are the ones they accept: if they are
// newer than what the sign-up options said, the tick is cleared.
async function loadSignupTerms() {
  const box = clear($("signup-terms-box"));
  box.appendChild(el("div", "loading", "Loading…"));
  let terms;
  try {
    terms = await api("GET", "/api/terms");
  } catch (err) {
    clear(box).appendChild(el("div", "error-box", err.status === 404 ? "No terms are published yet." :
      "Could not load the terms: " + err.message));
    return;
  }
  const known = st.signupOptions && st.signupOptions.terms;
  if (st.signupOptions && (!known || known.version !== terms.version)) {
    st.signupOptions.terms = { version: terms.version, title: terms.title };
    $("signup-accept").checked = false;
    applySignupTerms();
  }
  clear(box);
  box.appendChild(el("p", "muted small", termsMeta(terms)));
  renderTerms(box, terms.body);
  box.scrollTop = 0;
}

$("open-signup").addEventListener("click", showSignup);
$("signup-back").addEventListener("click", () => showLogin());

$("signup-read-terms").addEventListener("click", () => {
  const box = $("signup-terms-box");
  if (!box.hidden) { hideSignupTerms(); return; }
  box.hidden = false;
  $("signup-read-terms").textContent = "Hide the terms";
  loadSignupTerms();
});

$("signup-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const password = $("signup-password").value;
  if (!signupAvailable()) {
    notice("signup-notice", "Sign-up is not open right now. Please contact us for an account.");
    return;
  }
  if (password.length < MIN_PASSWORD) { notice("signup-notice", `Password: at least ${MIN_PASSWORD} characters, please.`); return; }
  if (password !== $("signup-repeat").value) { notice("signup-notice", "The two passwords are not the same."); return; }
  if (!$("signup-accept").checked) { notice("signup-notice", "Please tick the box to accept the terms."); return; }
  const button = ev.target.querySelector("button[type=submit]");
  button.disabled = true;
  try {
    await api("POST", "/api/owner/signup", {
      email: $("signup-email").value.trim(),
      password,
      display_name: $("signup-name").value.trim(),
      company: $("signup-company").value.trim(),
      phone: $("signup-phone").value.trim(),
      terms_version: st.signupOptions.terms.version,
      accept_terms: true,
    });
    for (const id of ["signup-password", "signup-repeat"]) $(id).value = "";
    $("signup-accept").checked = false;
    notice("signup-notice", "");
    await boot();
  } catch (err) {
    notice("signup-notice", err.detail || err.message);
    if (err.status === 403 && st.signupOptions) st.signupOptions.open = false;
    if (err.status === 409) {
      // Either the e-mail is taken or the terms changed: re-read the terms to tell.
      const before = st.signupOptions && st.signupOptions.terms ? st.signupOptions.terms.version : null;
      const o = await loadSignupOptions(true);
      const after = o && o.terms ? o.terms.version : null;
      if (after !== before) {
        $("signup-accept").checked = false;
        applySignupTerms();
        if (!$("signup-terms-box").hidden) loadSignupTerms();
      }
    }
  } finally {
    button.disabled = false;
  }
});

/* --------------------------------------------------- approval and terms */

$("pending-refresh").addEventListener("click", withBusy($("pending-refresh"), async () => {
  let account;
  try {
    account = await api("GET", "/api/owner/account");
  } catch (err) {
    if (!authProblem(err)) notice("pending-notice", "Could not check: " + err.message);
    return;
  }
  if (account.gate === PENDING_APPROVAL) {
    showPending(account, "Still waiting for approval. Please check again later.");
    return;
  }
  await boot();
}));

async function showTerms(message) {
  let data;
  try {
    data = await api("GET", "/api/owner/terms");
  } catch (err) {
    if (!authProblem(err)) showLogin("Could not load the terms of service: " + err.message);
    return;
  }
  show("screen-terms");
  const t = data.terms;
  st.terms = t;
  $("terms-accept-check").checked = false;
  $("terms-accept").disabled = true;
  $("terms-accept-check").disabled = !t;
  notice("terms-notice", message || "", "info");
  const box = clear($("terms-body"));
  if (!t) {
    $("terms-title").textContent = "Terms of service";
    $("terms-intro").textContent = "";
    $("terms-meta").textContent = "";
    $("terms-change").hidden = true;
    box.appendChild(el("div", "empty", "No terms are published yet."));
    return;
  }
  $("terms-title").textContent = t.title || "Terms of service";
  $("terms-intro").textContent = data.accepted
    ? "Our terms of service changed. Please read the new version and accept it to keep using the dashboard."
    : "Please read our terms of service and accept them to use the dashboard.";
  $("terms-meta").textContent = termsMeta(t);
  const note = (t.change_note || "").trim();
  $("terms-change").textContent = note ? "What changed: " + note : "";
  $("terms-change").hidden = !note;
  renderTerms(box, t.body);
  box.scrollTop = 0;
}

$("terms-accept-check").addEventListener("change", () => {
  $("terms-accept").disabled = !$("terms-accept-check").checked;
});

$("terms-accept").addEventListener("click", async () => {
  const button = $("terms-accept");
  if (!st.terms || !$("terms-accept-check").checked || button.disabled) return;
  button.disabled = true;
  try {
    await api("POST", "/api/owner/terms/accept", { version: st.terms.version });
    toast("Thank you. The terms are accepted.", "info");
    await boot();
  } catch (err) {
    if (err.status === 409) await showTerms(err.detail || "The terms changed in the meantime. Please read the new version.");
    else if (!authProblem(err)) {
      notice("terms-notice", err.message);
      button.disabled = !$("terms-accept-check").checked;
    }
  }
});

/* ---------------------------------------------------------------- login */

$("login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const button = ev.target.querySelector("button[type=submit]");
  button.disabled = true;
  try {
    const r = await api("POST", "/api/owner/login", {
      username: $("login-username").value.trim(),
      password: $("login-password").value,
      code: $("login-code").value.trim(),
    });
    $("login-password").value = "";
    $("login-code").value = "";
    notice("login-notice", "");
    if (r.must_change_password) showChange();
    else await boot();
  } catch (err) {
    if (err.detail === CODE_REQUIRED) {
      $("login-code-field").hidden = false;
      notice("login-notice", "Enter the 6-digit code from your authenticator app.", "info");
      $("login-code").focus();
    } else {
      notice("login-notice", err.message);
    }
  } finally {
    button.disabled = false;
  }
});

$("change-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const current = $("change-current").value;
  const next = $("change-new").value;
  if (next.length < MIN_PASSWORD) { notice("change-notice", `At least ${MIN_PASSWORD} characters, please.`); return; }
  if (next !== $("change-repeat").value) { notice("change-notice", "The two new passwords are not the same."); return; }
  const button = ev.target.querySelector("button[type=submit]");
  button.disabled = true;
  try {
    await api("POST", "/api/owner/password", { current, new: next });
    for (const id of ["change-current", "change-new", "change-repeat"]) $(id).value = "";
    toast("Password saved.", "info");
    await boot();
  } catch (err) {
    if (err.status === 401) showLogin("You were signed out. Please sign in again.");
    else notice("change-notice", err.message);
  } finally {
    button.disabled = false;
  }
});

async function logout() {
  try { await api("POST", "/api/owner/logout"); } catch (_) {}
  showLogin("You are signed out.");
}

$("change-logout").addEventListener("click", logout);
for (const id of ["pending-logout", "rejected-logout", "terms-logout"]) $(id).addEventListener("click", logout);
$("logout").addEventListener("click", logout);

/* ------------------------------------------------------------ dashboard */

function startApp() {
  show("screen-app");
  const me = st.me;
  $("app-user").textContent = me.display_name || me.username;
  const tenants = me.tenants;
  const select = clear($("switcher"));
  $("switcher-row").hidden = tenants.length < 2;
  if (tenants.length > 1) {
    select.appendChild(new Option("All my businesses", "all"));
    for (const t of tenants) select.appendChild(new Option(t.name, String(t.id)));
  }
  const saved = readView();
  if (tenants.length === 1) st.view = tenants[0].id;
  else if (saved && (saved === "all" || tenants.some((t) => String(t.id) === saved))) {
    st.view = saved === "all" ? "all" : Number(saved);
  } else st.view = tenants.length ? "all" : null;
  if (tenants.length > 1) select.value = String(st.view);
  renderView();
}

$("switcher").addEventListener("change", () => {
  const v = $("switcher").value;
  setView(v === "all" ? "all" : Number(v));
});

function setView(view) {
  st.view = view;
  writeView(view);
  if (st.me && st.me.tenants.length > 1) $("switcher").value = String(view);
  window.scrollTo(0, 0);
  renderView();
}

$("refresh").addEventListener("click", () => renderView());

async function renderView() {
  const box = clear($("content"));
  if (!st.me.tenants.length) {
    box.appendChild(el("div", "empty", "No business is linked to your login yet. Please contact your provider."));
    return;
  }
  box.appendChild(el("div", "loading", "Loading…"));
  const seq = ++st.renderSeq;
  try {
    if (st.view === "all") {
      const data = await api("GET", "/api/owner/overview");
      if (seq === st.renderSeq) renderOverview(clear(box), data);
    } else {
      const data = await api("GET", `/api/owner/dashboard?tenant_id=${encodeURIComponent(st.view)}`);
      if (seq === st.renderSeq) renderTenant(clear(box), data);
    }
  } catch (err) {
    if (authProblem(err) || seq !== st.renderSeq) return;
    if (err.status === 404) { writeView("all"); }
    clear(box).appendChild(el("div", "error-box", "Could not load: " + err.message));
  }
}

function statusLine(tenant) {
  const line = el("div", "status-line");
  const bot = tenant.bot || {};
  const sending = el("span", "pill " + (bot.sending ? "good" : "bad"));
  sending.appendChild(el("span", "dot"));
  sending.appendChild(el("span", "txt", bot.sending ? "Bot is answering" : "Bot is off: " + (bot.off_reason || "")));
  if (!bot.sending) sending.title = bot.off_reason || "";
  const h = HEALTH[(tenant.health || {}).status] || [tenant.health ? tenant.health.status : "unknown", "meh"];
  const health = el("span", "pill " + h[1]);
  health.appendChild(el("span", "dot"));
  health.appendChild(el("span", "txt", h[0]));
  line.append(sending, health);
  return line;
}

function section(title, extra) {
  const s = el("section", "section");
  const head = el("div", "section-head");
  head.appendChild(el("h2", null, title));
  if (extra) head.appendChild(extra);
  s.appendChild(head);
  return s;
}

function tile(value, label, alert) {
  const t = el("div", "tile" + (alert ? " alert" : ""));
  t.appendChild(el("div", "num", value));
  t.appendChild(el("div", "lbl", label));
  return t;
}

function percent(rate) {
  return rate === null || rate === undefined ? "—" : Math.round(rate * 100) + "%";
}

/* ------------------------------------------------------ one business */

function renderTenant(box, d) {
  const tz = d.tenant.timezone;
  box.appendChild(el("h1", null, d.tenant.name));
  box.appendChild(statusLine(d.tenant));

  // This week's numbers
  const week = d.summary.week;
  const s1 = section("This week");
  const tiles = el("div", "tiles");
  tiles.append(
    tile(week.bookings.booked, "Bookings"),
    tile(percent(week.bookings.no_show_rate), "No-show rate"),
    tile(week.messages.received, "Messages received"),
    tile(week.messages.sent_by_bot, "Answered by the bot"),
    tile(week.messages.sent_by_hand, "Answered by hand"),
    tile(week.unanswered, "Not answered", week.unanswered > 0),
  );
  s1.appendChild(tiles);
  box.appendChild(s1);

  // Today
  const s2 = section("Today", el("span", "muted small", fmtDay(d.summary.today.since, tz)));
  s2.appendChild(bookingList(d.bookings_today, tz, "No bookings today."));
  box.appendChild(s2);

  // Next days
  const s3 = section("Next 7 days");
  if (!d.bookings_upcoming.length) s3.appendChild(el("div", "empty", "Nothing booked for the coming week yet."));
  let lastDay = null;
  let list = null;
  for (const b of d.bookings_upcoming) {
    const key = fmtDayKey(b.starts_at, tz);
    if (key !== lastDay) {
      lastDay = key;
      s3.appendChild(el("div", "day-label", fmtDay(b.starts_at, tz)));
      list = el("div", "list");
      s3.appendChild(list);
    }
    list.appendChild(bookingRow(b, tz));
  }
  box.appendChild(s3);

  // 8 weeks
  const tabs = el("div", "seg");
  const s4 = section("Last 8 weeks", tabs);
  const chartBox = el("div");
  for (const [key, label] of [["bookings", "Bookings"], ["messages", "Messages"]]) {
    const b = el("button", st.chart === key ? "on" : "", label);
    b.type = "button";
    b.addEventListener("click", () => {
      st.chart = key;
      for (const other of tabs.children) other.classList.toggle("on", other === b);
      drawChart(clear(chartBox), d.weeks, tz);
    });
    tabs.appendChild(b);
  }
  drawChart(chartBox, d.weeks, tz);
  s4.appendChild(chartBox);
  box.appendChild(s4);

  // Unanswered
  const filter = el("div", "seg");
  const s5 = section("Unanswered messages", filter);
  const count = el("p", "hint", d.unanswered_open ? `${d.unanswered_open} waiting for a look.` :
    "Nothing waiting. Messages the bot did not answer show up here.");
  const qBox = el("div", "list");
  for (const [key, label] of [["open", "To review"], ["reviewed", "Reviewed"], ["all", "All"]]) {
    const b = el("button", st.queue === key ? "on" : "", label);
    b.type = "button";
    b.addEventListener("click", () => {
      st.queue = key;
      for (const other of filter.children) other.classList.toggle("on", other === b);
      loadQueue(qBox, d.tenant.id, tz);
    });
    filter.appendChild(b);
  }
  s5.append(count, qBox);
  box.appendChild(s5);
  loadQueue(qBox, d.tenant.id, tz);
}

function bookingList(bookings, tz, emptyText) {
  if (!bookings.length) return el("div", "empty", emptyText);
  const list = el("div", "list");
  for (const b of bookings) list.appendChild(bookingRow(b, tz));
  return list;
}

function bookingRow(b, tz) {
  const row = el("div", "booking st-" + b.state);
  row.appendChild(el("div", "time", fmtTime(b.starts_at, tz)));
  const body = el("div", "body");
  body.appendChild(el("div", "name", b.customer_name || "Customer"));
  const meta = [`#${b.number}`];
  if (b.service) meta.push(b.service);
  meta.push(`until ${fmtTime(b.ends_at, tz)}`);
  body.appendChild(el("div", "meta", meta.join(" · ")));
  if (b.proposed_starts_at) {
    body.appendChild(el("div", "meta", "Proposed new time: " + fmtWhen(b.proposed_starts_at, tz)));
  }
  row.appendChild(body);
  row.appendChild(el("span", "state st-" + b.state, BOOKING_STATES[b.state] || b.state));
  return row;
}

function drawChart(box, weeks, tz) {
  const values = weeks.map((w) => st.chart === "bookings" ? w.bookings.booked : w.messages.received);
  const max = Math.max(1, ...values);
  const chart = el("div", "chart");
  chart.setAttribute("role", "img");
  chart.setAttribute("aria-label", (st.chart === "bookings" ? "Bookings" : "Messages received") +
    " per week, oldest first: " + values.join(", "));
  const labels = el("div", "bar-labels");
  weeks.forEach((w, i) => {
    const col = el("div", "bar-col");
    col.appendChild(el("span", "bar-val", values[i]));
    const bar = el("div", "bar" + (i === weeks.length - 1 ? " current" : ""));
    bar.style.height = Math.round((values[i] / max) * 80) + "%";   // CSSOM, allowed under the CSP
    col.appendChild(bar);
    chart.appendChild(col);
    labels.appendChild(el("span", null, i === weeks.length - 1 ? "now" : fmtShortDay(w.since, tz)));
  });
  box.append(chart, labels);
}

async function loadQueue(box, tenantId, tz) {
  clear(box).appendChild(el("div", "loading", "Loading…"));
  let items;
  try {
    items = (await api("GET", `/api/owner/unanswered?tenant_id=${encodeURIComponent(tenantId)}` +
      `&status=${encodeURIComponent(st.queue)}`)).items;
  } catch (err) {
    if (authProblem(err)) return;
    clear(box).appendChild(el("div", "error-box", err.message));
    return;
  }
  clear(box);
  if (!items.length) {
    box.appendChild(el("div", "empty", st.queue === "open" ? "Nothing to review." : "Nothing here."));
    return;
  }
  for (const item of items) box.appendChild(queueItem(item, tz));
}

function queueItem(item, tz) {
  const card = el("div", "q-item" + (item.status === "open" ? "" : " done"));
  const top = el("div", "top");
  top.appendChild(el("span", "who", item.customer || `Chat ${item.chat_id}`));
  top.appendChild(el("span", "when", fmtWhen(item.created_at, tz)));
  card.appendChild(top);
  card.appendChild(el("div", "text", item.text || "(no text: a photo, a sticker or a deleted message)"));
  card.appendChild(el("div", "why", QUEUE_REASONS[item.reason] || item.reason));
  const actions = el("div", "actions");
  if (item.status === "open") {
    const b = el("button", "btn small", "Mark reviewed");
    b.type = "button";
    b.addEventListener("click", withBusy(b, async () => {
      try {
        const updated = await api("POST", `/api/owner/unanswered/${encodeURIComponent(item.id)}/reviewed`);
        if (st.queue === "open") card.remove();
        else card.replaceWith(queueItem(updated, tz));
        toast("Marked reviewed.", "info");
      } catch (err) {
        if (!authProblem(err)) toast(err.message);
      }
    }));
    actions.appendChild(b);
  } else {
    const by = item.reviewed_by ? item.reviewed_by.replace(/^owner:/, "") : "";
    actions.appendChild(el("span", "muted small",
      "Reviewed" + (by ? " by " + by : "") + (item.reviewed_at ? ", " + fmtWhen(item.reviewed_at, tz) : "")));
  }
  card.appendChild(actions);
  return card;
}

/* ------------------------------------------------- all my businesses */

function renderOverview(box, data) {
  box.appendChild(el("h1", null, "All my businesses"));
  box.appendChild(el("p", "hint", "Tap a business for its bookings and messages."));
  const grid = el("div", "biz-grid");
  for (const t of data.tenants) {
    const card = el("button", "biz");
    card.type = "button";
    card.appendChild(el("div", "name", t.name));
    card.appendChild(statusLine(t));
    const nums = el("div", "nums");
    for (const [value, label] of [[t.today.bookings.booked, "today"], [t.week.bookings.booked, "this week"],
                                  [t.unanswered_open, "to review"]]) {
      const cell = el("div");
      cell.appendChild(el("b", null, value));
      cell.appendChild(el("span", null, label));
      nums.appendChild(cell);
    }
    card.appendChild(nums);
    card.addEventListener("click", () => setView(t.id));
    grid.appendChild(card);
  }
  box.appendChild(grid);
}

/* ------------------------------------------------------------- settings */

function openSettings() {
  $("settings").hidden = false;
  renderTotp();
  $("pw-current").focus();
}

function closeSettings() {
  $("settings").hidden = true;
  for (const id of ["pw-current", "pw-new", "pw-repeat"]) $(id).value = "";
}

$("open-settings").addEventListener("click", openSettings);
$("close-settings").addEventListener("click", closeSettings);
$("settings").addEventListener("click", (ev) => { if (ev.target === $("settings")) closeSettings(); });
document.addEventListener("keydown", (ev) => { if (ev.key === "Escape" && !$("settings").hidden) closeSettings(); });

$("password-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const next = $("pw-new").value;
  if (next.length < MIN_PASSWORD) { toast(`At least ${MIN_PASSWORD} characters, please.`); return; }
  if (next !== $("pw-repeat").value) { toast("The two new passwords are not the same."); return; }
  const button = ev.target.querySelector("button[type=submit]");
  button.disabled = true;
  try {
    await api("POST", "/api/owner/password", { current: $("pw-current").value, new: next });
    for (const id of ["pw-current", "pw-new", "pw-repeat"]) $(id).value = "";
    toast("Password changed.", "info");
  } catch (err) {
    if (!authProblem(err)) toast(err.message);
  } finally {
    button.disabled = false;
  }
});

function codeField(id) {
  const label = el("label", "field", "Code from the app");
  const input = el("input");
  input.id = id;
  input.type = "text";
  input.inputMode = "numeric";
  input.autocomplete = "one-time-code";
  input.maxLength = 7;
  input.placeholder = "6 digits";
  label.appendChild(input);
  return [label, input];
}

function groupSecret(secret) {
  return secret.replace(/(.{4})/g, "$1 ").trim();
}

function renderTotp() {
  const box = clear($("totp-box"));
  if (st.me.totp) {
    box.appendChild(el("p", "hint", "On. Signing in asks for a code from your authenticator app. " +
      "To turn it off, enter a current code."));
    const [label, input] = codeField("totp-off-code");
    const b = el("button", "btn danger block", "Turn off two-step sign-in");
    b.type = "button";
    b.addEventListener("click", withBusy(b, async () => {
      try {
        await api("DELETE", "/api/owner/totp", { code: input.value.trim() });
        st.me.totp = false;
        toast("Two-step sign-in is off.", "info");
        renderTotp();
      } catch (err) {
        if (!authProblem(err)) toast(err.message);
      }
    }));
    box.append(label, b);
    return;
  }
  box.appendChild(el("p", "hint", "Off. With it on, signing in also asks for a 6-digit code from an app such as " +
    "Google Authenticator, Authy or 1Password, so a stolen password alone is not enough."));
  const start = el("button", "btn block", "Set up two-step sign-in");
  start.type = "button";
  start.addEventListener("click", withBusy(start, async () => {
    let setup;
    try { setup = await api("POST", "/api/owner/totp", {}); }
    catch (err) { if (!authProblem(err)) toast(err.message); return; }
    clear(box);
    box.appendChild(el("p", "hint", "1. In your authenticator app, add an account and type in this key:"));
    box.appendChild(el("div", "secret", groupSecret(setup.secret)));
    box.appendChild(el("p", "hint", "Or, on the phone that has the app, use this setup link:"));
    box.appendChild(el("div", "secret", setup.uri));
    box.appendChild(el("p", "hint", "2. Enter the code the app shows now:"));
    const [label, input] = codeField("totp-on-code");
    const confirmButton = el("button", "btn primary block", "Turn on");
    confirmButton.type = "button";
    confirmButton.addEventListener("click", withBusy(confirmButton, async () => {
      try {
        await api("POST", "/api/owner/totp", { code: input.value.trim() });
        st.me.totp = true;
        toast("Two-step sign-in is on.", "info");
        renderTotp();
      } catch (err) {
        if (!authProblem(err)) toast(err.message);
      }
    }));
    const cancel = el("button", "btn ghost block", "Cancel");
    cancel.type = "button";
    cancel.addEventListener("click", renderTotp);
    box.append(label, confirmButton, cancel);
    input.focus();
  }));
  box.appendChild(start);
}

boot();
