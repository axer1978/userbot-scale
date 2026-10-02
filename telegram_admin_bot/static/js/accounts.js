"use strict";

/* -------------------------------------------------------------- sign-in */

// The "Add account" dialog. It starts by asking which network the account
// is on. The server holds one Telegram sign-in in progress at a time, and
// this tab follows one WhatsApp pairing (whatsapp.js); opening the dialog
// picks either up at whatever step it is on.
async function openAddAccount() {
  $("session-menu").classList.remove("open");
  let auth;
  try { auth = await api("GET", "/api/auth"); }
  catch (err) { toast(err.message); return; }
  if (auth.step === "code" || auth.step === "password") applyAuth(auth);
  else if (waPairingInProgress()) showLoginStep("wa-pairing");
  else {
    state.auth = auth;
    showLoginNotice("");
    showLoginStep("l-channel");
  }
  $("login").classList.add("open");
}

// Shows one step of the dialog (Telegram's or WhatsApp's) and hides the rest.
function showLoginStep(id) {
  for (const node of document.querySelectorAll("#login .step")) node.classList.remove("on");
  $(id).classList.add("on");
}

function showLoginNotice(text, kind) {
  const notice = $("l-notice");
  notice.hidden = !text;
  notice.className = "notice" + (kind === "info" ? " info" : "");
  notice.textContent = text || "";
}

$("l-pick-telegram").addEventListener("click", async () => {
  try { applyAuth(await api("GET", "/api/auth")); }
  catch (err) { showLoginNotice(err.message); }
});
$("l-channel-close").addEventListener("click", () => $("login").classList.remove("open"));

async function closeAddAccount() {
  clearInterval(resendTimer);
  $("login").classList.remove("open");
  try { await api("POST", "/api/auth/cancel"); } catch (_) {}
}

$("l-close").addEventListener("click", closeAddAccount);

async function onAccountAdded(sessionId) {
  $("login").classList.remove("open");
  for (const id of ["l-label", "l-phone", "l-code"]) $(id).value = "";
  toast("Signed in. The server starts it within about 15 seconds. Replies wait for your approval " +
        "until you turn on auto-send in Settings.", "info");
  try { await fetchSessions(); } catch (_) {}
  await selectSession(sessionId);
}

function applyAuth(auth) {
  if (!auth) return;
  state.auth = auth;

  const notice = $("l-notice");
  notice.hidden = !auth.notice;
  notice.textContent = auth.notice || "";
  notice.className = "notice";

  for (const node of document.querySelectorAll("#login .step")) node.classList.remove("on");
  const stepForm = { credentials: "l-credentials", code: "l-code-form", password: "l-password-form" }[auth.step]
    || "l-credentials";
  $(stepForm).classList.add("on");
  if (auth.step !== "code") clearInterval(resendTimer);

  if (auth.step === "credentials") {
    $("l-api-hash").required = true;
    // Blank is accepted when re-signing-in a number that already has a key.
    $("l-deepseek").placeholder = "sk-…";
    setTimeout(() => ($("l-api-id").value ? $("l-phone") : $("l-api-id")).focus(), 0);
  } else if (auth.step === "code") {
    const where = $("l-delivery");
    where.textContent = "";
    where.appendChild(el("b", null, `Telegram sent the code for ${auth.phone || "your number"}:`));
    where.appendChild(document.createTextNode(auth.delivery || "in the Telegram app itself."));
    if (auth.code_length) $("l-code").maxLength = auth.code_length;
    startResendCountdown(auth);
    setTimeout(() => $("l-code").focus(), 0);
  } else if (auth.step === "password") {
    setTimeout(() => $("l-password").focus(), 0);
  }
}

// Disable the form while the request runs so a double click can't ask
// Telegram for two codes.
async function authStep(form, path, body) {
  const button = form.querySelector('button[type="submit"]');
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "…";
  try {
    applyAuth(await api("POST", path, body));
    return true;
  } catch (err) {
    // Some failures (an expired code) send the flow back to the start.
    try { applyAuth(await api("GET", "/api/auth")); } catch (_) {}
    const notice = $("l-notice");
    notice.hidden = false;
    notice.className = "notice";
    notice.textContent = err.message;
    return false;
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

$("l-credentials").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const ok = await authStep($("l-credentials"), "/api/auth/start", {
    api_id: $("l-api-id").value.trim(),
    api_hash: $("l-api-hash").value.trim(),
    phone: $("l-phone").value.trim(),
    deepseek_api_key: $("l-deepseek").value.trim(),
    label: $("l-label").value.trim(),
    proxy_url: $("l-proxy").value.trim(),
  });
  if (ok) {
    $("l-api-hash").value = "";
    $("l-deepseek").value = "";
    $("l-proxy").value = "";
    $("l-code").value = "";
  }
});

$("l-code-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const ok = await authStep($("l-code-form"), "/api/auth/code", { code: $("l-code").value.trim() });
  if (ok && state.auth.step === "done") onAccountAdded(state.auth.session_id);
});

$("l-password-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const ok = await authStep($("l-password-form"), "/api/auth/password",
                            { password: $("l-password").value });
  $("l-password").value = "";
  if (ok && state.auth.step === "done") onAccountAdded(state.auth.session_id);
});

// Telegram names the one channel a resend may use, and how long it must be
// asked to wait. Both come from the server; the button shows them rather than
// promising an SMS Telegram is not going to send.
let resendTimer = null;

function startResendCountdown(auth) {
  clearInterval(resendTimer);
  const button = $("l-resend");
  if (!auth.next_label) { button.hidden = true; return; }

  button.hidden = false;
  let left = auth.resend_in || 0;
  const tick = () => {
    if (left > 0) {
      button.disabled = true;
      button.textContent = `${auth.next_label} (${left}s)`;
      left -= 1;
    } else {
      button.disabled = false;
      button.textContent = auth.next_label;
      clearInterval(resendTimer);
    }
  };
  tick();
  resendTimer = setInterval(tick, 1000);
}

$("l-resend").addEventListener("click", async () => {
  const button = $("l-resend");
  button.disabled = true;
  try {
    const auth = await api("POST", "/api/auth/resend");
    applyAuth(auth);
    const notice = $("l-notice");
    notice.hidden = false;
    notice.className = "notice info";
    notice.textContent = "Code sent again.";
  } catch (err) {
    button.disabled = false;
    const notice = $("l-notice");
    notice.hidden = false;
    notice.className = "notice";
    notice.textContent = err.message;
  }
});

for (const id of ["l-restart", "l-restart-2"]) {
  $(id).addEventListener("click", async () => {
    try { applyAuth(await api("POST", "/api/auth/cancel")); }
    catch (err) { toast(err.message); }
  });
}

$("logout").addEventListener("click", async () => {
  if (!confirm("Sign out of the admin panel?")) return;
  teardownSocket();
  try { await api("POST", "/api/logout"); } catch (err) { toast(err.message); }
  state.sessionId = null;
  state.sessions = [];
  state.me = null;
  showGate();
});

/* -------------------------------------------------------- session picker */

function sessionLabel(s) {
  return s.label && s.label.trim() ? s.label : s.session_id;
}

// What only one network has: outreach (writing first to contacts) is
// Telegram-only; on WhatsApp that is what gets numbers banned.
function applyChannel() {
  const whatsapp = currentChannel() === "whatsapp";
  $("open-outreach").hidden = whatsapp;
  if (whatsapp) $("outreach").classList.remove("open");
}

// grey = not running anywhere, red = running but its network (Telegram or
// WhatsApp; status.telegram_connected covers both) is not connected,
// green = running and connected — per the plan's item 12/13.
function sessionDotClass(s) {
  if (!s.running_here) return "dot grey";
  if (s.status && s.status.telegram_connected) return "dot on";
  return "dot";
}

function renderSessionSwitcher() {
  const current = state.sessions.find((s) => s.session_id === state.sessionId);
  $("session-btn-label").textContent = current
    ? sessionLabel(current)
    : (state.sessions.length ? "Select session…" : "No sessions");
  $("session-btn-dot").className = current ? sessionDotClass(current) : "dot grey";

  const menu = $("session-menu");
  menu.textContent = "";
  if (!state.sessions.length) {
    menu.appendChild(el("div", "s-empty", "No accounts yet."));
  }
  for (const s of state.sessions) {
    const row = el("div", "s-row" + (s.session_id === state.sessionId ? " active" : ""));
    row.appendChild(el("span", sessionDotClass(s)));
    row.appendChild(el("span", "s-name", sessionLabel(s)));
    const whatsapp = s.channel === "whatsapp";
    row.appendChild(el("span", "badge channel" + (whatsapp ? " wa" : " tg"), whatsapp ? "WA" : "TG"));
    row.title = `${channelName(s.channel)} · ${s.session_id}`;
    row.addEventListener("click", () => {
      menu.classList.remove("open");
      selectSession(s.session_id);
    });
    menu.appendChild(row);
  }
  if (can("accounts.connect")) {
    const add = el("div", "s-row s-add");
    add.appendChild(el("span", "s-name", "+ Add account"));
    add.addEventListener("click", openAddAccount);
    menu.appendChild(add);
  }
  applyChannel();
}

$("session-btn").addEventListener("click", (ev) => {
  ev.stopPropagation();
  $("session-menu").classList.toggle("open");
});
document.addEventListener("click", (ev) => {
  if (!$("session-switcher").contains(ev.target)) $("session-menu").classList.remove("open");
});

async function fetchSessions() {
  const sessions = await api("GET", "/api/sessions");
  state.sessions = sessions;
  renderSessionSwitcher();
  return sessions;
}

// Pulls this session's conversations/config/media over plain REST for a fast
// first paint; the websocket's own "hello" message repeats the same data
// moments later, which is harmless. Guards against a session switch landing
// mid-flight by bailing out if the user has since picked a different one.
async function loadSessionData(sessionId) {
  let conversations = [];
  try { conversations = await sApi("GET", "/conversations", undefined, sessionId); }
  catch (err) { toast("Could not load conversations: " + err.message); }
  if (sessionId !== state.sessionId) return;
  state.conversations = conversations;

  if (can("view.config")) {
    try {
      const cfg = await sApi("GET", "/config", undefined, sessionId);
      if (sessionId !== state.sessionId) return;
      applyConfig(cfg);
    } catch (err) { toast("Could not load config: " + err.message); }
  }

  try {
    const mediaList = await sApi("GET", "/media", undefined, sessionId);
    if (sessionId !== state.sessionId) return;
    state.media = mediaList;
  } catch (_) { state.media = []; }

  renderSidebar();
}

// Tears down the previous session's socket, resets every piece of UI state
// that belongs to "whichever session is open", and reconnects fresh.
async function selectSession(sessionId) {
  if (!sessionId || sessionId === state.sessionId) return;
  teardownSocket();
  state.sessionId = sessionId;
  try { localStorage.setItem("panelSessionId", sessionId); } catch (_) {}

  state.conversations = [];
  state.messages = [];
  state.activeChatId = null;
  document.body.classList.remove("chat-open");
  state.config = null;
  state.tenantConfig = null;
  state.status = null;
  state.drafting = new Set();
  state.links = [];
  state.linkOptions = null;
  state.media = [];
  outreach.contacts = [];
  outreach.selected.clear();
  outreach.items = [];
  draftEdits.clear();

  renderSessionSwitcher();
  renderSidebar();
  renderThreadHeader();
  renderComposer();
  renderThread();
  $("conn-dot").classList.remove("on");
  $("conn-text").textContent = "connecting…";
  $("brand-instance").textContent = "";

  await loadSessionData(sessionId);
  if (sessionId !== state.sessionId) return; // switched again while loading
  connectSocket(sessionId);
}

/* --------------------------------------------------------------- gate */

// Whether the admin's own sign-in asks for an authenticator code.
let gateAdminTotp = false;

function showGate(message) {
  const notice = $("ag-notice");
  if (message) { notice.hidden = false; notice.textContent = message; }
  else { notice.hidden = true; notice.textContent = ""; }
  $("admin-gate").classList.add("open");
  setTimeout(() => ($("ag-username").value.trim() ? $("ag-password") : $("ag-username")).focus(), 0);
  gateCodeField();
  // The admin is asked for a code only when the server has 2FA turned on.
  api("GET", "/api/login-options").then((opts) => {
    gateAdminTotp = !!opts.totp;
    gateCodeField();
  }).catch(() => {});
}

// Staff (a username given) always sign in with their authenticator code.
function gateCodeField() {
  const needed = gateAdminTotp || !!$("ag-username").value.trim();
  $("ag-code-field").hidden = !needed;
  $("ag-code").required = needed;
}

$("ag-username").addEventListener("input", gateCodeField);

function hideGate() {
  $("admin-gate").classList.remove("open");
}

async function afterAuth(sessions) {
  let preferred = null;
  try { preferred = localStorage.getItem("panelSessionId"); } catch (_) {}
  const wanted = sessions.find((s) => s.session_id === preferred) || sessions[0];
  if (wanted) {
    await selectSession(wanted.session_id);
  } else {
    renderSessionSwitcher();
    if (can("accounts.connect")) await openAddAccount();
  }
  // Keeps the picker's dots current (running/connected can change from
  // outside this tab) without needing its own websocket.
  setInterval(async () => {
    if ($("admin-gate").classList.contains("open") || !can("view.conversations")) return;
    try { await fetchSessions(); } catch (_) {}
  }, 20000);
}

// Signed in (or already were): who it is, then the panel their role allows.
async function startPanel() {
  state.me = await api("GET", "/api/me");
  const sessions = can("view.conversations") ? await fetchSessions() : null;
  hideGate();
  applyRole();
  sfPoll();
  uaPoll();
  vfPoll();
  stPoll();
  if (sessions) await afterAuth(sessions);
  else noAccountsView();
}

// The header buttons and the views each one needs. The session-level ones
// need an account picked, so the conversations view as well.
// null = the admin only.
const ROLE_BUTTONS = [
  ["global-pause", ["view.conversations", "tenant.pause"]],
  ["open-outreach", ["view.conversations"]],
  ["open-bookings", ["view.conversations", "view.bookings"]],
  ["open-media", ["view.conversations"]],
  ["open-style", ["view.conversations"]],
  ["open-settings", ["view.conversations", "view.config"]],
  ["open-safety", ["view.safety"]],
  ["open-unanswered", ["view.unanswered"]],
  ["open-platform", ["view.config"]],
  ["open-onboarding", ["view.config"]],
  ["open-review", ["view.training"]],
  ["open-owners", ["view.clients"]],
  ["open-verify", ["view.verification"]],
  ["open-terms", ["view.terms"]],
  ["open-managers", null],
  ["open-staff", null],
];

// Hides what the role can't see (the server refuses it anyway) and names
// the staff member in the header. The admin sees everything.
function applyRole() {
  for (const [id, needs] of ROLE_BUTTONS) {
    const allowed = needs === null ? isAdmin() : needs.every(can);
    $(id).classList.toggle("role-off", !allowed);
  }
  const accounts = can("view.conversations");
  $("session-switcher").classList.toggle("role-off", !accounts);
  $("conn-dot").parentNode.classList.toggle("role-off", !accounts);
  if (!can("view.safety")) {
    $("safety-banner").hidden = true;
    $("safety-count").textContent = "";
  }
  if (!can("view.unanswered")) $("unanswered-count").textContent = "";
  if (!can("view.verification")) $("verify-count").textContent = "";
  if (!isAdmin()) $("staff-count").textContent = "";
  const chip = $("me-chip");
  const staff = !!(state.me && !state.me.admin);
  chip.hidden = !staff;
  chip.textContent = staff ? `${state.me.username} · ${state.me.role || "no role"}` : "";
  chip.title = staff ? `Signed in as ${state.me.display_name || state.me.username} (role: ${state.me.role || "none"}). ` +
    "Some changes you make may wait for the admin's approval." : "";
}

// A role without the conversations view: no account picker, no chats.
function noAccountsView() {
  teardownSocket();
  state.sessionId = null;
  state.sessions = [];
  $("sidebar").textContent = "";
  $("thread-header").textContent = "";
  const thread = $("thread");
  thread.textContent = "";
  thread.appendChild(el("div", "empty", "Your role does not include accounts and conversations. " +
    "Use the buttons at the top for what it does include."));
}

$("ag-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const button = $("ag-form").querySelector('button[type="submit"]');
  const username = $("ag-username").value.trim();
  const password = $("ag-password").value;
  const code = $("ag-code").value.trim();
  button.disabled = true;
  try {
    await api("POST", "/api/login", { username, password, code });
    $("ag-password").value = "";
    $("ag-code").value = "";
    await startPanel();
  } catch (err) {
    const notice = $("ag-notice");
    notice.hidden = false;
    notice.textContent = err.message || "Wrong password";
  } finally {
    button.disabled = false;
  }
});

