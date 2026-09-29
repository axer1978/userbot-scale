"use strict";

/* ----------------------------------------------------------- onboarding */
// "New client": a step-by-step setup over the routes the panel already
// has: the account sign-in (accounts.js), PATCH /api/tenants/{id} for the
// name and industry, PUT /api/tenants/{id}/config for the settings (every
// save validated and audited there). The only route of its own is the
// read-only status summary (/api/onboarding, review_api.py), which is what
// makes the wizard resumable: every step's "done" is derived from what is
// stored, so closing the wizard, or changing a setting under Clients, loses
// nothing.
//
// The wizard writes no prompt text: the business sections are the platform
// owner's to fill in under Clients → Prompt.

const ob = {
  tenantId: null,
  status: null,       // /api/onboarding/{id}
  view: null,         // /api/tenants/{id}: config (effective, overrides, revision) and prompt
  step: "account",
  industries: [],
  watching: null,     // session ids that existed when the sign-in dialog was opened
  observer: null,
  promptSeen: new Set(),
};

const OB_STEPS = [["account", "Telegram account"], ["business", "Business"], ["settings", "Key settings"],
                  ["staging", "Staging"], ["prompt", "Prompt check"], ["live", "Go live"]];

async function openOnboarding(tenantId) {
  $("onboarding").classList.add("open");
  if (tenantId) await obSelectTenant(tenantId);
  else if (ob.tenantId) await obSelectTenant(ob.tenantId, ob.step);
  else { ob.step = "account"; await obRender(); }
}

function closeOnboarding() { $("onboarding").classList.remove("open"); }

async function obSelectTenant(tenantId, step) {
  ob.tenantId = tenantId;
  try { await obReload(); } catch (err) { toast(err.message); ob.tenantId = null; ob.step = "account"; }
  if (ob.tenantId) {
    // Resume at the first step not done yet (the prompt check sits before
    // go-live, so a tenant in staging lands on it once).
    let next = step || ob.status.next_step || "live";
    if (!step && next === "live" && !ob.promptSeen.has(tenantId)) next = "prompt";
    ob.step = next;
  }
  await obRender();
}

async function obReload() {
  const [status, view] = await Promise.all([
    api("GET", `/api/onboarding/${ob.tenantId}`),
    api("GET", `/api/tenants/${ob.tenantId}`),
  ]);
  ob.status = status;
  ob.view = view;
}

function obStepDone(key) {
  if (!ob.status) return false;
  if (key === "prompt") return ob.promptSeen.has(ob.tenantId) || ob.status.steps.live;
  return !!ob.status.steps[key];
}

function obRenderProgress() {
  const bar = $("ob-progress");
  bar.textContent = "";
  OB_STEPS.forEach(([key, label], i) => {
    const b = el("button", "ob-step" + (obStepDone(key) ? " done" : "") + (ob.step === key ? " on" : ""));
    b.type = "button";
    b.appendChild(el("span", "ob-num", obStepDone(key) ? "✓" : String(i + 1)));
    b.appendChild(el("span", "ob-label", label));
    // Every step but the first needs a client picked.
    b.disabled = key !== "account" && !ob.tenantId;
    b.addEventListener("click", () => { ob.step = key; obRender(); });
    bar.appendChild(b);
  });
}

async function obRender() {
  obRenderProgress();
  $("ob-crumb").textContent = ob.status ? ob.status.name : "";
  const box = $("ob-body");
  box.textContent = "";
  const render = { account: obAccount, business: obBusiness, settings: obSettings, staging: obStaging,
                   prompt: obPrompt, live: obLive, done: obDone }[ob.step];
  await render(box);
}

function obNext(from) {
  const i = OB_STEPS.findIndex(([key]) => key === from);
  ob.step = i >= 0 && i < OB_STEPS.length - 1 ? OB_STEPS[i + 1][0] : "done";
  obRender();
}

function obField(label, input, path, hint) {
  const f = el("div", "field");
  f.dataset.path = path || "";
  f.append(el("label", null, label), input);
  if (hint) f.appendChild(el("div", "ob-hint", hint));
  f.appendChild(el("div", "ob-err"));
  return f;
}

function obCheck(label, checked, path) {
  const input = el("input");
  input.type = "checkbox";
  input.checked = !!checked;
  const f = el("div", "field check");
  f.dataset.path = path || "";
  const id = "ob-" + path.replace(/\W/g, "-");
  input.id = id;
  const lab = el("label", null, label);
  lab.htmlFor = id;
  f.append(input, lab, el("div", "ob-err"));
  return [f, input];
}

function obActions(...buttons) {
  const row = el("div", "sheet-actions ob-actions");
  row.append(...buttons);
  return row;
}

/* ---------------------------------------------------- 1. the account */

async function obAccount(box) {
  box.appendChild(el("p", "pf-note", "Every client runs on its own Telegram account. Sign a new one in, or pick an " +
    "account that is already signed in and not set up yet."));
  const add = el("button", "btn primary ob-big", "Sign in a new Telegram account");
  add.addEventListener("click", obStartSignIn);
  box.appendChild(add);
  if (ob.watching) {
    box.appendChild(el("p", "pf-note", "Finish the sign-in in the dialog; this step continues by itself when it " +
      "is done."));
  }

  box.appendChild(el("h3", "sf-sub", "Accounts already signed in"));
  let all;
  try { all = await api("GET", "/api/onboarding"); }
  catch (err) { box.appendChild(el("div", "pf-errors", err.message)); return; }
  const open = all.filter((s) => s.session_id && !s.steps.live);
  const liveOnes = all.filter((s) => s.session_id && s.steps.live);
  if (!open.length) box.appendChild(el("p", "pf-note", "None waiting to be set up."));
  const list = (items, target) => {
    for (const s of items) {
      const row = el("div", "bk-row ob-tenant" + (s.tenant_id === ob.tenantId ? " open" : ""));
      const head = el("div", "bk-head");
      head.appendChild(el("b", null, s.name));
      head.appendChild(el("span", "bk-num", s.session_label || s.session_id));
      const done = OB_STEPS.filter(([k]) => k !== "prompt" && s.steps[k]).length;
      const label = s.steps.live ? "live" : s.staging.enabled ? "staging" : s.configured ? "set up, not tested"
        : "not set up";
      head.appendChild(el("span", "bk-state", `${label} · ${done}/5`));
      if (s.session_state && s.session_state !== "active") head.appendChild(el("span", "warn-note", s.session_state));
      head.addEventListener("click", () => obSelectTenant(s.tenant_id));
      row.appendChild(head);
      target.appendChild(row);
    }
  };
  list(open, box);
  if (liveOnes.length) {
    const more = el("details", "ob-live-list");
    more.appendChild(el("summary", "muted", `Live clients (${liveOnes.length}), to change a step`));
    box.appendChild(more);
    list(liveOnes, more);
  }
}

// Opens the panel's own sign-in dialog (accounts.js) above the wizard and
// waits for it to close. A session id that was not there before is the new
// account; its tenant was created with it (SessionRegistry.create).
async function obStartSignIn() {
  try { ob.watching = new Set((await api("GET", "/api/sessions")).map((s) => s.session_id)); }
  catch (err) { toast(err.message); return; }
  if (ob.observer) ob.observer.disconnect();
  ob.observer = new MutationObserver(async () => {
    if ($("login").classList.contains("open")) return;
    ob.observer.disconnect();
    ob.observer = null;
    const before = ob.watching;
    ob.watching = null;
    let added = [];
    try { added = (await api("GET", "/api/sessions")).filter((s) => !before.has(s.session_id)); }
    catch (err) { toast(err.message); }
    if (!added.length) { obRender(); return; }   // cancelled
    try {
      const tenant = await api("GET", `/api/tenants/by-session/${encodeURIComponent(added[0].session_id)}`);
      await obSelectTenant(tenant.id, "business");
    } catch (err) { toast(err.message); obRender(); }
  });
  ob.observer.observe($("login"), { attributes: true, attributeFilter: ["class"] });
  await openAddAccount();
  if (!$("login").classList.contains("open")) {
    // The dialog did not open (openAddAccount showed why).
    ob.observer.disconnect();
    ob.observer = null;
    ob.watching = null;
  }
  obRender();
}

/* --------------------------------------------------- 2. the business */

async function obBusiness(box) {
  try { ob.industries = (await api("GET", "/api/platform/tree")).industries; }
  catch (err) { box.appendChild(el("div", "pf-errors", err.message)); return; }
  const s = ob.status;
  const name = el("input");
  name.type = "text";
  name.maxLength = 200;
  name.placeholder = "The business name customers know";
  name.value = s.steps.business ? s.name : "";
  const industry = el("select");
  for (const i of ob.industries) {
    const o = el("option", null, i.name);
    o.value = i.id;
    if (i.id === s.industry_id) o.selected = true;
    industry.appendChild(o);
  }
  box.append(
    obField("Business name", name, "name"),
    obField("Industry", industry, "industry_id", "Decides the default settings and the prompt template it starts from."),
  );
  const errors = el("div", "pf-errors");
  const save = el("button", "btn primary ob-big", "Save and continue");
  save.addEventListener("click", async () => {
    errors.textContent = "";
    if (!name.value.trim()) { errors.textContent = "Enter the business name."; return; }
    save.disabled = true;
    try {
      await api("PATCH", `/api/tenants/${ob.tenantId}`,
        { name: name.value.trim(), industry_id: Number(industry.value), reason: "onboarding" });
      await obReload();
      obNext("business");
    } catch (err) { errors.textContent = err.message; }
    finally { save.disabled = false; }
  });
  box.append(errors, obActions(save));
}

/* ------------------------------------------------- 3. key settings */

function obIsObject(v) { return v && typeof v === "object" && !Array.isArray(v); }

// Nested objects merge; anything else (lists included) replaces.
function obDeepMerge(base, patch) {
  const out = obIsObject(base) ? JSON.parse(JSON.stringify(base)) : {};
  for (const [key, value] of Object.entries(patch)) {
    out[key] = obIsObject(value) && obIsObject(out[key]) ? obDeepMerge(out[key], value) : value;
  }
  return out;
}

// Merges `patch` into the client's current overrides and saves them, with
// the revision just read, so a change made elsewhere in between is refused
// (409) rather than overwritten.
async function obSaveConfig(patch, reason) {
  const view = await api("GET", `/api/tenants/${ob.tenantId}`);
  ob.view = await api("PUT", `/api/tenants/${ob.tenantId}/config`, {
    overrides: obDeepMerge(view.config.overrides || {}, patch),
    reason,
    expected_revision: view.config.revision,
  });
  ob.status = await api("GET", `/api/onboarding/${ob.tenantId}`);
}

// Validation errors ({path, message}) next to the field they are about;
// what matches no field goes to the box under the form.
function obShowErrors(form, errors, err) {
  for (const node of form.querySelectorAll(".ob-err")) node.textContent = "";
  errors.textContent = err.message;
  if (!err.errors) return;
  const fields = [...form.querySelectorAll("[data-path]")].filter((f) => f.dataset.path);
  const loose = [];
  for (const e of err.errors) {
    const hit = fields.find((f) => f.dataset.path === e.path) ||
      fields.find((f) => f.dataset.path.startsWith(e.path + ".")) ||
      fields.find((f) => e.path.startsWith(f.dataset.path + "."));
    if (hit) hit.querySelector(".ob-err").textContent = e.message;
    else loose.push(`${e.path}: ${e.message}`);
  }
  errors.textContent = loose.length ? loose.join(" · ") : "Please fix the fields marked below.";
}

function obZones() {
  try { return Intl.supportedValuesOf("timeZone"); } catch (_) { return []; }
}

async function obSettings(box) {
  const c = ob.view.config.effective;
  box.appendChild(el("p", "pf-note", "The settings a new client most often needs. Everything else is under " +
    "Clients → Config. Only what you change here is saved as this client's own value; the rest keeps " +
    "following the industry."));
  const form = el("div", "ob-form");

  const tz = el("input");
  tz.type = "text";
  tz.value = c.timezone;
  tz.setAttribute("list", "ob-zones");
  const zones = el("datalist");
  zones.id = "ob-zones";
  for (const z of obZones()) { const o = el("option"); o.value = z; zones.appendChild(o); }
  const provider = el("input");
  provider.type = "text";
  provider.maxLength = 64;
  provider.value = c.booking.provider;
  provider.placeholder = "@username or phone number";
  const [bookingsField, bookings] = obCheck("Bookings on (the bot takes booking requests for the owner to confirm)",
    c.booking.enabled, "booking.enabled");
  const [autoField, autoSend] = obCheck("Auto-send (off: every reply waits in the panel for approval)",
    c.auto_send, "auto_send");
  const [quietField, quiet] = obCheck("Quiet hours (nothing is sent in this window)", c.quiet_hours.enabled,
    "quiet_hours.enabled");
  const qStart = el("input");
  qStart.type = "time";
  qStart.value = c.quiet_hours.start;
  const qEnd = el("input");
  qEnd.type = "time";
  qEnd.value = c.quiet_hours.end;
  const cap = el("input");
  cap.type = "number";
  cap.min = 1;
  cap.step = 1;
  cap.value = c.daily_message_cap;

  const quietRow = el("div", "row");
  quietRow.append(obField("Quiet from", qStart, "quiet_hours.start"), obField("until", qEnd, "quiet_hours.end"));
  form.append(
    obField("Timezone", tz, "timezone", "Opening hours, quiet hours, reminders and billing follow it."),
    zones,
    obField("Owner's Telegram", provider, "booking.provider",
      "Booking requests, escalations and the weekly summary go here."),
    bookingsField, autoField, quietField, quietRow,
    obField("Messages per day, at most", cap, "daily_message_cap", "Every message the account sends, replies included."),
  );
  box.appendChild(form);

  const errors = el("div", "pf-errors");
  const save = el("button", "btn primary ob-big", "Save and continue");
  save.addEventListener("click", async () => {
    // Only what differs from the current effective value becomes a client
    // override; the rest keeps following the industry.
    const patch = {};
    const put = (path, value, current) => {
      if (value === current) return;
      const keys = path.split(".");
      let node = patch;
      for (const k of keys.slice(0, -1)) node = node[k] = node[k] || {};
      node[keys[keys.length - 1]] = value;
    };
    put("timezone", tz.value.trim(), c.timezone);
    put("booking.provider", provider.value.trim(), c.booking.provider);
    put("booking.enabled", bookings.checked, c.booking.enabled);
    put("auto_send", autoSend.checked, c.auto_send);
    put("quiet_hours.enabled", quiet.checked, c.quiet_hours.enabled);
    put("quiet_hours.start", qStart.value, c.quiet_hours.start);
    put("quiet_hours.end", qEnd.value, c.quiet_hours.end);
    put("daily_message_cap", cap.value === "" ? null : Number(cap.value), c.daily_message_cap);
    if (!provider.value.trim()) {
      errors.textContent = "Enter the owner's Telegram: without it, nothing can reach the owner.";
      return;
    }
    save.disabled = true;
    try {
      if (Object.keys(patch).length) await obSaveConfig(patch, "onboarding: key settings");
      obNext("settings");
    } catch (err) {
      if (err.status === 409) {
        try { await obReload(); } catch (_) {}
        errors.textContent = err.message;
      } else obShowErrors(form, errors, err);
    } finally { save.disabled = false; }
  });
  box.append(errors, obActions(save));
}

/* ------------------------------------------------------ 4. staging */

async function obStaging(box) {
  const st = ob.view.config.effective.staging;
  box.appendChild(el("p", "pf-note", "In staging the bot answers only the test chats listed here. Everyone else's " +
    "messages are stored and listed as unanswered, not replied to. Use it to try the bot before real customers " +
    "reach it."));
  box.appendChild(el("div", "ob-state " + (st.enabled ? "on" : "off"),
    st.enabled ? `Staging is ON: answering only ${st.test_chats.join(", ") || "(no test chats)"}.`
      : "Staging is off: the bot answers everyone."));

  const chats = el("textarea");
  chats.rows = 4;
  chats.value = st.test_chats.join("\n");
  chats.placeholder = "@username or numeric chat id, one per line";
  const form = el("div", "ob-form");
  form.appendChild(obField("Test chats", chats, "staging.test_chats",
    "Telegram usernames (with or without @) or chat ids of people who will test, e.g. your own account."));
  box.appendChild(form);

  const errors = el("div", "pf-errors");
  const on = el("button", "btn primary ob-big", st.enabled ? "Save test chats" : "Turn staging on");
  on.addEventListener("click", async () => {
    errors.textContent = "";
    const list = chats.value.split(/[\n,]+/).map((x) => x.trim()).filter(Boolean);
    if (!list.length) { errors.textContent = "Add at least one test chat."; return; }
    on.disabled = true;
    try {
      await obSaveConfig({ staging: { enabled: true, test_chats: list } }, "onboarding: staging on");
      await obRender();
    } catch (err) { obShowErrors(form, errors, err); }
    finally { on.disabled = false; }
  });
  const skip = el("button", "btn ob-big", st.enabled ? "Continue" : "Skip staging");
  skip.addEventListener("click", () => obNext("staging"));
  box.append(errors, obActions(skip, on));

  if (st.enabled) {
    const test = el("div", "pf-section ob-test");
    test.appendChild(el("div", "title", "Now test it"));
    test.appendChild(el("p", "pf-note", "Send a message to this account from one of the test chats, e.g. a " +
      "question a customer would ask. The reply appears in the account's conversations (or waits there for " +
      "approval while auto-send is off)."));
    const sessionId = ob.status.session_id;
    if (sessionId && typeof selectSession === "function") {
      const go = el("button", "btn ob-big", "Open this account's conversations");
      go.addEventListener("click", async () => {
        closeOnboarding();
        await selectSession(sessionId);
        toast("The wizard keeps its place: open New client again to continue.", "info");
      });
      test.appendChild(go);
    } else {
      test.appendChild(el("p", "pf-note", "Pick the account in the account switcher at the top to see its " +
        "conversations."));
    }
    box.appendChild(test);
  }
}

/* -------------------------------------------------- 5. prompt check */

async function obPrompt(box) {
  const p = ob.view.prompt;
  box.appendChild(el("p", "pf-note", "This is the whole prompt the bot runs on, as it is now. It is read-only " +
    "here: the business sections (services, hours, prices, tone, …) are filled in under Clients → this client → " +
    "Prompt by the platform owner. Check that it describes this business before going live."));
  box.appendChild(el("pre", "pf-rendered", p.rendered));
  box.appendChild(el("div", "muted", `Version ${p.version_tag}`));
  ob.promptSeen.add(ob.tenantId);
  const edit = el("button", "btn ob-big", "Open Clients → Prompt");
  edit.addEventListener("click", async () => {
    closeOnboarding();
    await openPlatform();
    await selectNode({ kind: "tenant", id: ob.tenantId }, "prompt");
  });
  const next = el("button", "btn primary ob-big", "Looks right, continue");
  next.addEventListener("click", () => obNext("prompt"));
  box.appendChild(obActions(edit, next));
}

/* ------------------------------------------------------ 6. go live */

async function obLive(box) {
  const s = ob.status;
  const c = ob.view.config.effective;
  const facts = el("div", "pf-section");
  const fact = (ok, text) => facts.appendChild(el("div", "ob-fact " + (ok ? "ok" : "todo"), (ok ? "✓ " : "• ") + text));
  fact(s.steps.account, `Account: ${s.session_label || s.session_id || "none"}`);
  fact(s.steps.business, `Business: ${s.name} (${s.industry})`);
  fact(s.steps.settings, `Owner's Telegram: ${c.booking.provider || "not set"}`);
  fact(true, `Auto-send: ${c.auto_send ? "on" : "off (every reply waits for approval)"}`);
  fact(s.staging.enabled, s.staging.enabled ? `Staging on, test chats: ${s.staging.test_chats.join(", ")}`
    : "Staging is off");
  box.appendChild(facts);

  if (!s.configured) {
    box.appendChild(el("div", "pf-errors", "Name the business and set the owner's Telegram first."));
    return;
  }
  const errors = el("div", "pf-errors");
  if (!s.staging.enabled) {
    box.appendChild(el("p", "pf-note", "Staging is off, so this client already answers everyone."));
    const done = el("button", "btn primary ob-big", "Finish");
    done.addEventListener("click", () => { ob.step = "done"; obRender(); });
    box.appendChild(obActions(done));
    return;
  }
  box.appendChild(el("p", "pf-note", "Going live turns staging off: from then on the bot answers everyone who " +
    "writes to this account. The test chat list is kept, so staging can be turned back on later."));
  const live = el("button", "btn primary ob-big", "Go live");
  live.addEventListener("click", async () => {
    if (!confirm(`Go live for ${s.name}? The bot will answer every customer from now on.`)) return;
    live.disabled = true;
    try {
      await obSaveConfig({ staging: { enabled: false } }, "onboarding: go live");
      ob.step = "done";
      await obRender();
    } catch (err) { errors.textContent = err.message; live.disabled = false; }
  });
  box.append(errors, obActions(live));
}

async function obDone(box) {
  box.appendChild(el("div", "ob-state live", `${ob.status ? ob.status.name : "The client"} is live.`));
  box.appendChild(el("p", "pf-note", "Next: give the owner a login to their dashboard under Client logins " +
    "(one login can hold several of their accounts)."));
  const another = el("button", "btn ob-big", "Set up another client");
  another.addEventListener("click", () => {
    ob.tenantId = null; ob.status = null; ob.view = null; ob.step = "account"; obRender();
  });
  const close = el("button", "btn primary ob-big", "Close");
  close.addEventListener("click", closeOnboarding);
  box.appendChild(obActions(another, close));
}

$("open-onboarding").addEventListener("click", () => openOnboarding());
$("ob-close").addEventListener("click", closeOnboarding);
$("onboarding").addEventListener("click", (ev) => { if (ev.target === $("onboarding")) closeOnboarding(); });
