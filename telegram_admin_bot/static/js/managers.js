"use strict";

/* ------------------------------------------------------------- managers */
// Logins for moderators who can step in when the platform admin can't
// (manager_admin_api.py). They sign in at /manager/ with a temporary
// password set here, choose their own and must set up an authenticator app
// before anything opens. Nothing here ever shows a stored password or
// authenticator secret; the server never returns them. Temporary passwords
// come from owTempPassword (owners.js).

const mg = { tab: "list", managers: [] };

const MG_TABS = [["list", "Managers"], ["new", "New manager"]];

const MG_CAN = [
  "see every client's status, health and alerts",
  "pause a client's bot",
  "lift manual or anomaly holds",
  "read conversations (read-only)",
  "pause and resume single chats",
  "acknowledge alerts",
  "approve or reject client sign-ups",
  "disable and enable client logins",
];

const MG_CANNOT = [
  "change configs or prompts",
  "touch billing",
  "lift any other hold",
  "send messages",
  "use the global stop or hard-off",
  "link businesses to client logins",
  "set or reset passwords",
  "manage managers",
  "change the terms or sign-up",
];

function mgUrl() {
  return location.origin + "/manager/";
}

async function openManagers(tab) {
  if (tab) mg.tab = tab;
  $("managers").classList.add("open");
  await mgRender();
}

function closeManagers() { $("managers").classList.remove("open"); }

async function mgRender() {
  const tabs = $("mg-tabs");
  tabs.textContent = "";
  for (const [key, label] of MG_TABS) {
    const b = el("button", "pf-tab" + (mg.tab === key ? " on" : ""), label);
    b.addEventListener("click", () => { mg.tab = key; mgRender(); });
    tabs.appendChild(b);
  }
  const box = $("mg-body");
  box.textContent = "";
  try {
    mg.managers = await api("GET", "/api/managers");
  } catch (err) { box.appendChild(el("div", "pf-errors", err.message)); return; }
  (mg.tab === "new" ? mgNew : mgList)(box);
}

async function mgCall(method, path, body, done) {
  try {
    const result = await api(method, path, body);
    if (done) toast(done, "info");
    await mgRender();
    return result;
  } catch (err) {
    toast(err.message);
    return null;
  }
}

// What a manager can and can't do, and the rules every manager login has.
function mgRules(box) {
  const note = el("div", "pf-note mg-rules");
  const col = (heading, items) => {
    const wrap = el("div");
    wrap.appendChild(el("div", "mg-rules-head", heading));
    const ul = el("ul");
    for (const item of items) ul.appendChild(el("li", null, item));
    wrap.appendChild(ul);
    return wrap;
  };
  const cols = el("div", "mg-rules-cols");
  cols.append(col("A manager can", MG_CAN), col("A manager cannot", MG_CANNOT));
  note.appendChild(cols);
  note.appendChild(el("p", null, "An authenticator app is required: they set one up at their first sign-in, " +
    "before anything opens. Every action they take is in the audit log as \"manager:<username>\"."));
  box.appendChild(note);
}

/* ------------------------------------------------------------- the list */

function mgList(box) {
  box.appendChild(el("p", "pf-note", "Managers sign in at " + mgUrl() + "."));
  mgRules(box);
  if (!mg.managers.length) {
    box.appendChild(el("div", "pf-note", "No managers yet."));
    const add = el("button", "btn primary", "Create the first one");
    add.addEventListener("click", () => { mg.tab = "new"; mgRender(); });
    box.appendChild(add);
    return;
  }
  for (const m of mg.managers) box.appendChild(mgCard(m));
}

function mgCard(m) {
  const card = el("div", "pf-section ow-card" + (m.disabled ? " ow-disabled" : ""));
  const title = el("div", "title");
  title.appendChild(el("span", null, m.username));
  const badges = el("span", "ow-badges");
  if (m.disabled) badges.appendChild(el("span", "badge paused", "disabled"));
  if (m.must_change_password) badges.appendChild(el("span", "badge", "temporary password"));
  badges.appendChild(m.totp ? el("span", "badge link", "2FA on") : el("span", "badge paused", "no authenticator yet"));
  title.appendChild(badges);
  card.appendChild(title);
  card.appendChild(el("div", "pf-note ow-meta",
    `${m.display_name || "No name"} · last sign-in ${owTime(m.last_login_at)} · ` +
    `${m.sessions} active session${m.sessions === 1 ? "" : "s"} · created ${owTime(m.created_at)}` +
    (m.created_by ? ` by ${m.created_by}` : "")));

  // Name
  const nameRow = el("div", "pf-actions");
  const name = el("input");
  name.type = "text";
  name.value = m.display_name;
  name.placeholder = "Name";
  name.maxLength = 200;
  const saveName = el("button", "btn small", "Save name");
  saveName.addEventListener("click", () =>
    mgCall("PATCH", `/api/managers/${m.id}`, { display_name: name.value }, "Name saved."));
  nameRow.append(name, saveName);
  card.appendChild(nameRow);

  // Actions
  const actions = el("div", "pf-actions");
  const toggle = el("button", "btn small" + (m.disabled ? "" : " warn"), m.disabled ? "Enable" : "Disable");
  toggle.title = m.disabled ? "Allow this manager to sign in again" : "Block this manager and end their sessions now";
  toggle.addEventListener("click", () => {
    if (!m.disabled && !confirm(`Disable ${m.username}? Their open sessions end at once.`)) return;
    mgCall("PATCH", `/api/managers/${m.id}`, { disabled: !m.disabled }, m.disabled ? "Enabled." : "Disabled.");
  });
  actions.appendChild(toggle);

  const reset = el("button", "btn small", "Reset password…");
  reset.addEventListener("click", async () => {
    const suggestion = owTempPassword();
    const password = prompt(`New temporary password for ${m.username} (at least 10 characters). ` +
      "Their sessions end and they choose their own at the next sign-in.", suggestion);
    if (password === null) return;
    const done = await mgCall("POST", `/api/managers/${m.id}/reset-password`, { password }, null);
    if (done) mgShowSecret(`Temporary password for ${m.username}`, password);
  });
  actions.appendChild(reset);

  if (m.totp) {
    const totp = el("button", "btn small", "Remove 2FA");
    totp.title = "For a lost phone: their sessions end and they must set up a new app at the next sign-in";
    totp.addEventListener("click", () => {
      if (!confirm(`Remove the authenticator of ${m.username}? Their sessions end now and they must set up ` +
        "a new app at the next sign-in. Only do this when you are sure it is them asking.")) return;
      mgCall("DELETE", `/api/managers/${m.id}/totp`, undefined, "Authenticator removed.");
    });
    actions.appendChild(totp);
  }

  const del = el("button", "btn small warn", "Delete");
  del.addEventListener("click", () => {
    if (!confirm(`Delete the manager ${m.username}? What they did stays in the audit log.`)) return;
    mgCall("DELETE", `/api/managers/${m.id}`, undefined, "Manager deleted.");
  });
  actions.appendChild(del);
  card.appendChild(actions);
  return card;
}

// A password is shown once, right after it was set, to pass on to the manager.
function mgShowSecret(label, password) {
  const box = $("mg-body");
  const note = el("div", "pf-section ow-secret");
  note.appendChild(el("div", "title", label));
  note.appendChild(el("p", "pf-note", "Shown only now. Send it to them together with " + mgUrl() +
    "; they choose their own password and set up an authenticator app when they first sign in."));
  note.appendChild(el("code", "ow-password", password));
  box.insertBefore(note, box.firstChild);
}

/* ---------------------------------------------------------- new manager */

function mgNew(box) {
  const form = el("div", "pf-section");
  const field = (label, input) => {
    const wrap = el("div", "field");
    wrap.append(el("label", null, label), input);
    form.appendChild(wrap);
    return input;
  };
  const username = el("input");
  username.type = "text";
  username.placeholder = "e.g. maris@example.com or maris";
  username.autocomplete = "off";
  username.maxLength = 64;
  field("Username (3–64 letters, digits or . _ @ + -)", username);
  const name = el("input");
  name.type = "text";
  name.placeholder = "e.g. Māris Ozols";
  name.maxLength = 200;
  field("Name", name);
  const password = el("input");
  password.type = "text";
  password.autocomplete = "off";
  password.value = owTempPassword();
  const pwRow = el("div", "pf-actions ow-pw-row");
  const regen = el("button", "btn small", "New suggestion");
  regen.type = "button";
  regen.addEventListener("click", () => { password.value = owTempPassword(); });
  pwRow.append(password, regen);
  const pwWrap = el("div", "field");
  pwWrap.append(el("label", null, "Temporary password (at least 10 characters; they change it at the first sign-in)"),
    pwRow);
  form.appendChild(pwWrap);

  const actions = el("div", "pf-actions");
  const create = el("button", "btn primary", "Create manager");
  create.addEventListener("click", async () => {
    create.disabled = true;
    try {
      const manager = await api("POST", "/api/managers", {
        username: username.value.trim(), display_name: name.value.trim(), password: password.value,
      });
      const temp = password.value;
      toast(`Manager ${manager.username} created.`, "info");
      mg.tab = "list";
      await mgRender();
      mgShowSecret(`Temporary password for ${manager.username}`, temp);
    } catch (err) {
      toast(err.message);
    } finally {
      create.disabled = false;
    }
  });
  actions.appendChild(create);
  form.appendChild(actions);
  box.appendChild(form);
  mgRules(box);
  username.focus();
}

$("open-managers").addEventListener("click", () => openManagers());
$("mg-close").addEventListener("click", closeManagers);
$("managers").addEventListener("click", (ev) => { if (ev.target === $("managers")) closeManagers(); });
