"use strict";

/* --------------------------------------------------------------- safety */
// Kill switches, billing, alerts and health (safety_api.py). Everything
// here is platform-admin only. The top bar polls a small summary so an
// alert, the global stop or a dead scheduler shows without opening this.

const sf = { tab: "clients", overview: null, tenantId: null, openOnly: true };

const SF_TABS = [["clients", "All clients"], ["alerts", "Alerts"], ["client", "This client"],
                 ["billing", "Billing notice"]];
const SF_HEALTH = { ok: "connected", not_running: "not running", disconnected: "not connected",
                    logged_out: "logged out", rate_limited: "rate-limited", revoked: "revoked (hard-off)",
                    stopped: "stopped", unknown: "no report yet" };
const SF_BILLING = { active: "active", grace: "grace", suspended: "suspended" };

function sfTime(iso) {
  return iso ? new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "—";
}

async function openSafety(tab, tenantId) {
  if (tab) sf.tab = tab;
  if (tenantId) sf.tenantId = tenantId;
  $("safety").classList.add("open");
  await sfRender();
}

function closeSafety() { $("safety").classList.remove("open"); }

async function sfRender() {
  const tabs = $("sf-tabs");
  tabs.textContent = "";
  for (const [key, label] of SF_TABS) {
    const b = el("button", "pf-tab" + (sf.tab === key ? " on" : ""), label);
    b.addEventListener("click", () => { sf.tab = key; sfRender(); });
    tabs.appendChild(b);
  }
  const box = $("sf-body");
  box.textContent = "";
  try {
    sf.overview = await api("GET", "/api/safety");
    sfApplySummary(sf.overview);
  } catch (err) { box.appendChild(el("div", "pf-errors", err.message)); return; }
  await ({ clients: sfClients, alerts: sfAlerts, client: sfClient, billing: sfBillingSettings })[sf.tab](box);
}

function sfTenantName(id) {
  const t = sf.overview && sf.overview.tenants.find((x) => x.id === id);
  return t ? t.name : (id ? `Client ${id}` : "Platform");
}

/* ------------------------------------------------------ top-bar summary */

function sfApplySummary(s) {
  const count = $("safety-count");
  const total = s.alerts ? s.alerts.total : 0;
  count.textContent = total ? String(total) : "";
  count.classList.toggle("critical", !!(s.alerts && s.alerts.critical));
  const banner = $("safety-banner");
  const lines = [];
  if (s.global_stop && s.global_stop.on) {
    lines.push(`GLOBAL STOP: every client is soft-off (${s.global_stop.reason}). Safety → All clients to resume.`);
  }
  if (s.scheduler && s.scheduler.stale) {
    lines.push("The scheduler is not running" + (s.scheduler.at ? ` (last seen ${sfTime(s.scheduler.at)})` : "") +
      ": no reminders, no health alerts, no billing changes until it is.");
  }
  banner.textContent = lines.join("  ·  ");
  banner.hidden = !lines.length;
}

async function sfPoll() {
  if ($("admin-gate").classList.contains("open") || !can("view.safety")) return;
  try { sfApplySummary(await api("GET", "/api/safety/summary")); } catch (_) {}
}

/* ------------------------------------------------------------ clients */

async function sfClients(box) {
  const o = sf.overview;
  const stop = el("div", "pf-section" + (o.global_stop.on ? " danger" : ""));
  stop.appendChild(el("div", "title", "Global stop"));
  if (o.global_stop.on) {
    stop.appendChild(el("p", "warn-note", `On since ${sfTime(o.global_stop.at)}: ${o.global_stop.reason}. ` +
      "Every client is soft-off: messages are received, nothing is sent on its own."));
    const resume = el("button", "btn primary", "Resume everything");
    resume.addEventListener("click", async () => {
      if (!confirm("Lift the global stop? Clients with their own holds stay off.")) return;
      try { await api("POST", "/api/safety/global-stop", { on: false, reason: "" }); await sfRender(); }
      catch (err) { toast(err.message); }
    });
    stop.appendChild(resume);
  } else {
    stop.appendChild(el("p", "pf-note", "Soft-off for every client at once: messages keep arriving and are stored, " +
      "nothing is sent on its own until you lift it. Resuming replays nothing. Also available on the server: " +
      "python controls.py stop \"reason\"."));
    const button = el("button", "btn warn", "Stop everything…");
    button.addEventListener("click", async () => {
      const reason = prompt("Why is everything being stopped? (goes into the audit log)", "");
      if (!reason || !reason.trim()) return;
      try { await api("POST", "/api/safety/global-stop", { on: true, reason }); await sfRender(); }
      catch (err) { toast(err.message); }
    });
    stop.appendChild(button);
  }
  box.appendChild(stop);
  box.appendChild(el("p", "pf-note", "Scheduler: " + (o.scheduler.at ? `last tick ${sfTime(o.scheduler.at)}` : "never ran") +
    (o.scheduler.stale ? " — NOT RUNNING" : "")));

  const table = el("table", "cfg-table sf-table");
  const head = el("tr");
  for (const h of ["Client", "Account", "Telegram", "Sending", "Billing", "Alerts"]) head.appendChild(el("td", "muted", h));
  table.appendChild(head);
  for (const t of o.tenants) {
    const tr = el("tr", "sf-click");
    const health = t.health.status;
    const sending = o.global_stop.on ? "global stop" :
      t.holds.length ? t.holds.map((h) => h.label).join(", ") : "on";
    const billing = SF_BILLING[t.billing.status] + (t.billing.next_due ? ` · due ${t.billing.next_due}` : "");
    const cells = [
      [t.name, null], [t.label || t.session_id || "—", "muted"],
      [SF_HEALTH[health] || health, "sf-h-" + health],
      [sending, sending === "on" ? "sf-ok" : "sf-bad"],
      [billing, "sf-b-" + t.billing.status],
      [t.open_alerts ? String(t.open_alerts) : "", t.open_alerts ? "sf-bad" : null],
    ];
    for (const [text, cls] of cells) tr.appendChild(el("td", cls, text));
    tr.title = t.health.last_error ? "Last error: " + t.health.last_error : "";
    tr.addEventListener("click", () => { sf.tenantId = t.id; sf.tab = "client"; sfRender(); });
    table.appendChild(tr);
  }
  box.appendChild(table);
}

/* ------------------------------------------------------------- alerts */

async function sfAlerts(box) {
  const bar = el("div", "bk-nav");
  const toggle = el("button", "btn small", sf.openOnly ? "Show acknowledged too" : "Only open ones");
  toggle.addEventListener("click", () => { sf.openOnly = !sf.openOnly; sfRender(); });
  const all = el("button", "btn small", "Acknowledge all");
  all.addEventListener("click", async () => {
    try { await api("POST", "/api/alerts/ack-all", {}); await sfRender(); } catch (err) { toast(err.message); }
  });
  bar.append(toggle, all, el("span", "muted", "Also delivered by e-mail / webhook when ALERT_EMAIL / " +
    "ALERT_WEBHOOK_URL are set in .env."));
  box.appendChild(bar);
  let list;
  try { list = await api("GET", "/api/alerts" + (sf.openOnly ? "?open=true" : "")); }
  catch (err) { box.appendChild(el("div", "pf-errors", err.message)); return; }
  if (!list.length) box.appendChild(el("div", "empty", sf.openOnly ? "No open alerts." : "No alerts yet."));
  for (const a of list) box.appendChild(sfAlertRow(a));
}

function sfAlertRow(a) {
  const row = el("div", "bk-row sf-alert sev-" + a.severity + (a.acknowledged_at ? " done" : ""));
  const head = el("div", "bk-head");
  head.append(el("span", "bk-state", a.severity), el("span", "bk-time", sfTime(a.last_at)),
    el("span", "bk-who", sfTenantName(a.tenant_id)), el("span", "muted", a.kind));
  if (a.count > 1) head.appendChild(el("span", "muted", `×${a.count}`));
  head.appendChild(el("span", "spacer"));
  if (!a.acknowledged_at) {
    const ack = el("button", "btn small", "Acknowledge");
    ack.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      try { await api("POST", `/api/alerts/${a.id}/ack`); await sfRender(); } catch (err) { toast(err.message); }
    });
    head.appendChild(ack);
  } else {
    head.appendChild(el("span", "muted", `closed by ${a.acknowledged_by}`));
  }
  if (a.tenant_id) head.addEventListener("click", () => { sf.tenantId = a.tenant_id; sf.tab = "client"; sfRender(); });
  row.append(head, el("div", "bk-detail", a.message));
  return row;
}

/* ------------------------------------------------------- one client */

async function sfClient(box) {
  const id = sf.tenantId || (state.status && state.status.tenant_id);
  if (!id) { box.appendChild(el("div", "empty", "Pick a client under All clients.")); return; }
  let c;
  try { c = await api("GET", `/api/tenants/${id}/controls`); }
  catch (err) { box.appendChild(el("div", "pf-errors", err.message)); return; }
  box.appendChild(el("h3", "sf-name", `${c.tenant.name} · ${c.tenant.label || c.tenant.session_id || "no account"}`));

  // Sending
  const send = el("div", "pf-section" + (c.off_reason ? " danger" : ""));
  send.appendChild(el("div", "title", "Sending"));
  send.appendChild(el("p", c.off_reason ? "warn-note" : "pf-note", c.off_reason
    ? "Soft-off: " + c.off_reason + ". Messages are received and stored; nothing is sent on its own. " +
      "Resuming replays nothing."
    : "On: replies, reminders and owner messages go out as configured."));
  for (const h of c.holds) {
    const line = el("div", "sf-hold");
    line.append(el("strong", null, h.label), el("span", null, h.reason),
      el("span", "muted", `${sfTime(h.created_at)} by ${h.created_by}`), el("span", "spacer"));
    if (h.kind === "billing") {
      line.appendChild(el("span", "muted", "lifted by recording a payment (below)"));
    } else {
      const resume = el("button", "btn small primary", "Resume");
      resume.addEventListener("click", async () => {
        const reason = prompt(`Lift "${h.label}"? Say why (goes into the audit log).`, "");
        if (reason === null) return;
        await sfPost(`/api/tenants/${id}/resume`, { kind: h.kind, reason });
      });
      line.appendChild(resume);
    }
    send.appendChild(line);
  }
  if (!c.holds.some((h) => h.kind === "manual")) {
    const off = el("button", "btn small warn", "Soft-off (pause)…");
    off.addEventListener("click", async () => {
      const reason = prompt("Why? (e.g. payment, owner on holiday)", "");
      if (reason === null) return;
      await sfPost(`/api/tenants/${id}/soft-off`, { reason });
    });
    send.appendChild(off);
  }
  box.appendChild(send);

  // Billing
  const b = c.billing;
  const bill = el("div", "pf-section");
  bill.appendChild(el("div", "title", "Billing"));
  bill.appendChild(el("p", "pf-note", `Status: ${SF_BILLING[b.status]}` +
    (b.status === "grace" ? ` until ${sfTime(b.grace_until)} (owner told: ${b.notice_sent_at ? sfTime(b.notice_sent_at) : "not yet"})` : "") +
    ". The day after the due date without a payment it goes into grace (the owner gets a message from this " +
    "account), then it is suspended."));
  const dueRow = el("div", "pf-actions");
  const due = el("input");
  due.type = "date";
  due.value = b.next_due || "";
  const saveDue = el("button", "btn small", "Save due date");
  saveDue.addEventListener("click", () => sfPut(`/api/tenants/${id}/billing/due`, { next_due: due.value || null }));
  const paid = el("button", "btn small primary", "Record payment…");
  paid.addEventListener("click", async () => {
    const next = prompt("Payment recorded. Next due date (YYYY-MM-DD, empty = stop tracking):", "");
    if (next === null) return;
    if (next && !/^\d{4}-\d{2}-\d{2}$/.test(next.trim())) { toast("Use YYYY-MM-DD"); return; }
    await sfPost(`/api/tenants/${id}/billing/paid`, { next_due: next.trim() || null });
  });
  const statusSel = el("select");
  for (const s of Object.keys(SF_BILLING)) {
    const o = el("option", null, "Set " + SF_BILLING[s]);
    o.value = s;
    o.selected = s === b.status;
    statusSel.appendChild(o);
  }
  const setStatus = el("button", "btn small", "Override");
  setStatus.addEventListener("click", async () => {
    const reason = prompt(`Set the billing status to "${statusSel.value}" by hand. Why?`, "");
    if (!reason || !reason.trim()) return;
    await sfPost(`/api/tenants/${id}/billing/status`, { status: statusSel.value, reason });
  });
  dueRow.append(el("span", "muted", "Next due"), due, saveDue, paid, statusSel, setStatus);
  bill.appendChild(dueRow);
  box.appendChild(bill);

  // Health
  const h = c.health;
  const hl = el("div", "pf-section");
  hl.appendChild(el("div", "title", "Telegram"));
  const facts = [["Status", (SF_HEALTH[h.status] || h.status) + (h.status_since ? ` since ${sfTime(h.status_since)}` : "")],
                 ["Last connected", sfTime(h.last_seen_at)],
                 ["Last error", h.last_error ? `${h.last_error} (${sfTime(h.last_error_at)})` : ""],
                 ["Rate-limited until", h.rate_limited_until ? sfTime(h.rate_limited_until) : ""]];
  for (const [label, value] of facts) {
    if (!value) continue;
    const line = el("div", "bk-fact");
    line.append(el("span", "muted", label + ": "), el("span", null, value));
    hl.appendChild(line);
  }
  if (h.logins) {
    hl.appendChild(el("div", "muted sf-sub", `Logins on this Telegram account (checked ${sfTime(h.logins_checked_at)}). ` +
      "A new one switches the client off (anomaly.new_login_suspend)."));
    for (const l of h.logins) {
      hl.appendChild(el("div", "bk-fact", `${l.current ? "▶ this server · " : ""}${l.device || "?"} · ${l.platform || ""} · ` +
        `${l.app || ""} · ${l.country || ""}${l.created ? " · since " + sfTime(l.created) : ""}`));
    }
  }
  box.appendChild(hl);

  // Telegram proxy
  const px = el("div", "pf-section");
  px.appendChild(el("div", "title", "Telegram proxy"));
  px.appendChild(el("p", "pf-note", c.proxy
    ? `Connects through ${c.proxy.type}://${c.proxy.host}:${c.proxy.port}` +
      (c.proxy.username ? ` as ${c.proxy.username}` : "") + ". The password is stored encrypted and never shown."
    : "Connects directly from this server. A residential or mobile proxy in the account's country makes it " +
      "connect from there instead."));
  if (c.tenant.session_id) {
    const pxRow = el("div", "pf-actions");
    const pxInput = el("input");
    pxInput.type = "password";
    pxInput.autocomplete = "off";
    pxInput.placeholder = "socks5://user:password@host:port";
    const pxSave = el("button", "btn small primary", c.proxy ? "Replace" : "Use this proxy");
    pxSave.addEventListener("click", () => sfProxy(id, pxInput.value.trim()));
    pxRow.append(pxInput, pxSave);
    if (c.proxy) {
      const pxClear = el("button", "btn small warn", "Connect directly");
      pxClear.addEventListener("click", () => {
        if (confirm("Stop using the proxy and connect from this server's own address?")) sfProxy(id, "");
      });
      pxRow.appendChild(pxClear);
    }
    px.appendChild(pxRow);
  }
  box.appendChild(px);

  // Hard-off
  const hard = el("div", "pf-section danger");
  hard.appendChild(el("div", "title", "Hard-off"));
  hard.appendChild(el("p", "pf-note", "For a hijacked or leaked session: logs this server's Telegram session out, " +
    "deletes its key and deactivates the account. Signing it in again is a new login from the panel. " +
    "It does not touch the owner's phone or other logins."));
  if (c.tenant.session_id && c.tenant.state !== "revoked") {
    const button = el("button", "btn warn", "Revoke the session…");
    button.addEventListener("click", async () => {
      const reason = prompt("Why is the session being revoked? (audit log)", "");
      if (!reason || !reason.trim()) return;
      const confirmId = prompt(`Type the account id (${c.tenant.session_id}) to confirm.`, "");
      if (confirmId === null) return;
      try {
        const r = await api("POST", `/api/tenants/${id}/hard-off`, { reason, confirm: confirmId });
        toast(r.logged_out ? "Session logged out and deleted." :
          "Key deleted, but Telegram could not be told: end the session under Settings → Devices on the phone.",
          r.logged_out ? "info" : undefined);
      } catch (err) { toast(err.message); }
      await sfRender();
    });
    hard.appendChild(button);
  } else {
    hard.appendChild(el("p", "muted", c.tenant.state === "revoked" ? "Already revoked." : "No account."));
  }
  box.appendChild(hard);

  if (c.alerts.length) {
    box.appendChild(el("h4", "bk-day", "Alerts for this client"));
    for (const a of c.alerts) box.appendChild(sfAlertRow(a));
  }
}

async function sfPost(path, body) {
  try { await api("POST", path, body); } catch (err) { toast(err.message); }
  await sfRender();
  if (state.sessionId) {
    try { applyStatus(await sApi("GET", "/status")); } catch (_) {}
  }
}

async function sfProxy(id, url) {
  try {
    const r = await api("PUT", `/api/tenants/${id}/proxy`, { proxy_url: url });
    toast((url ? "Proxy saved. " : "Proxy removed. ") +
      (r.reconnected ? "The account reconnected." : "The account uses it when it next starts."), "info");
  } catch (err) { toast(err.message); }
  await sfRender();
}

async function sfPut(path, body) {
  try { await api("PUT", path, body); toast("Saved.", "info"); } catch (err) { toast(err.message); }
  await sfRender();
}

/* ---------------------------------------------------- billing notice */

async function sfBillingSettings(box) {
  let s;
  try { s = await api("GET", "/api/platform/billing"); }
  catch (err) { box.appendChild(el("div", "pf-errors", err.message)); return; }
  box.appendChild(el("p", "pf-note", "Sent to the owner (booking.provider) from the client's own account when a " +
    "payment is missed, once, at the start of grace. You can use {business}, {due} and {until}."));
  const hours = el("input");
  hours.type = "number";
  hours.min = 1;
  hours.value = s.grace_hours;
  const text = el("textarea");
  text.rows = 4;
  text.value = s.notice;
  const f1 = el("div", "field");
  f1.append(el("label", null, "Grace, in hours"), hours);
  const f2 = el("div", "field");
  f2.append(el("label", null, "Message to the owner"), text);
  const save = el("button", "btn primary", "Save");
  save.addEventListener("click", () => sfPut("/api/platform/billing",
    { grace_hours: Number(hours.value), notice: text.value }));
  box.append(f1, f2, save);
}

$("open-safety").addEventListener("click", () => openSafety());
$("sf-close").addEventListener("click", closeSafety);
$("safety").addEventListener("click", (ev) => { if (ev.target === $("safety")) closeSafety(); });
$("safety-banner").addEventListener("click", () => openSafety("clients"));
setInterval(sfPoll, 30000);
