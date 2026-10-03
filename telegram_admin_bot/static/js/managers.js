"use strict";

/* ------------------------------------------------------------- managers */
// Logins for moderators who can step in when the platform admin can't
// (manager_admin_api.py). They sign in at /manager/ with a temporary
// password set here, choose their own and must set up an authenticator app
// before anything opens. Nothing here ever shows a stored password or
// authenticator secret; the server never returns them. Temporary passwords
// come from owTempPassword (owners.js). What each manager may do is their
// role's (Staff → Roles, staffroles.js); a role with the admin panel signs
// in at / as well, the rest only at /manager/.

const mg = { tab: "list", managers: [], roles: [] };

const MG_TABS = [["list", "Managers"], ["new", "New manager"]];

function mgUrl() {
  return location.origin + "/manager/";
}

function mgRole(id) {
  return mg.roles.find((r) => r.id === id) || null;
}

// Where a manager with this role signs in.
function mgSignInUrl(roleId) {
  const role = mgRole(roleId);
  return role && role.admin_panel ? location.origin + "/" : mgUrl();
}

// A role picker; "" stands for the default (the server's "Moderator").
function mgRoleSelect(current, withDefault) {
  const select = el("select", "mg-role");
  if (withDefault) {
    const option = el("option", null, "Moderator (default)");
    option.value = "";
    select.appendChild(option);
  }
  for (const r of mg.roles) {
    const option = el("option", null, r.name + (r.admin_panel ? " · admin panel" : ""));
    option.value = String(r.id);
    select.appendChild(option);
  }
  if (current !== null && current !== undefined) select.value = String(current);
  return select;
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
    const [managers, roles] = await Promise.all([
      api("GET", "/api/managers"),
      api("GET", "/api/staff/roles").catch(() => null),
    ]);
    mg.managers = managers;
    mg.roles = roles ? roles.roles : [];
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

// What a manager can do is their role's; the roles there are, and the rules
// every manager login has.
function mgRules(box) {
  const note = el("div", "pf-note mg-rules");
  note.appendChild(el("div", "mg-rules-head", "What a manager can do comes from their role"));
  if (mg.roles.length) {
    const ul = el("ul");
    for (const r of mg.roles) {
      const li = el("li");
      li.appendChild(el("b", null, r.name));
      li.appendChild(document.createTextNode(
        ` (${r.admin_panel ? "admin panel and " : ""}/manager/)` + (r.description ? ": " + r.description : "")));
      ul.appendChild(li);
    }
    note.appendChild(ul);
  }
  const edit = el("button", "btn small", "Edit roles");
  edit.type = "button";
  edit.addEventListener("click", () => { closeManagers(); openStaff("roles"); });
  note.appendChild(edit);
  note.appendChild(el("p", null, "An authenticator app is required: they set one up at their first sign-in " +
    "at " + mgUrl() + ", before anything opens (also for the admin panel). Every change they make is in Staff → " +
    "Activity and the audit log as \"manager:<username>\"."));
  box.appendChild(note);
}

/* ------------------------------------------------------------- the list */

function mgList(box) {
  box.appendChild(el("p", "pf-note", "Managers sign in at " + mgUrl() + ". Those whose role includes the " +
    "admin panel can also sign in at " + location.origin + "/ with their username."));
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
  badges.appendChild(el("span", "badge takeover", m.role_name || "no role"));
  title.appendChild(badges);
  card.appendChild(title);
  card.appendChild(el("div", "pf-note ow-meta",
    `${m.display_name || "No name"} · last sign-in ${owTime(m.last_login_at)} · ` +
    `${m.sessions} active session${m.sessions === 1 ? "" : "s"} · created ${owTime(m.created_at)}` +
    (m.created_by ? ` by ${m.created_by}` : "") + ` · signs in at ${mgSignInUrl(m.role_id)}`));

  // Role
  if (mg.roles.length) {
    const roleRow = el("div", "pf-actions");
    const role = mgRoleSelect(m.role_id, m.role_id === null);
    const saveRole = el("button", "btn small", "Change role");
    saveRole.addEventListener("click", () => {
      if (!role.value || Number(role.value) === m.role_id) return;
      mgCall("PATCH", `/api/managers/${m.id}`, { role_id: Number(role.value) }, "Role changed.");
    });
    roleRow.append(el("span", "muted", "Role"), role, saveRole);
    card.appendChild(roleRow);
  }

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
  const role = mgRoleSelect(null, true);
  const fallback = mg.roles.find((r) => r.name === "Moderator");
  if (fallback) role.value = String(fallback.id);
  field("Role (what they may do; Staff → Roles)", role);
  const where = el("p", "pf-note");
  const showWhere = () => {
    const url = mgSignInUrl(role.value ? Number(role.value) : (fallback ? fallback.id : null));
    where.textContent = url === mgUrl()
      ? "They sign in at " + mgUrl() + "."
      : "This role includes the admin panel: they set up their login at " + mgUrl() + " first, then sign in at " +
        url + " with their username.";
  };
  role.addEventListener("change", showWhere);
  showWhere();
  form.appendChild(where);

  const actions = el("div", "pf-actions");
  const create = el("button", "btn primary", "Create manager");
  create.addEventListener("click", async () => {
    create.disabled = true;
    try {
      const body = { username: username.value.trim(), display_name: name.value.trim(), password: password.value };
      if (role.value) body.role_id = Number(role.value);
      const manager = await api("POST", "/api/managers", body);
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
