"use strict";

/* --------------------------------------------------------- client logins */
// Logins for business owners to their own dashboard (/owner/), managed here
// by the platform admin (owner_admin_api.py). One login can be linked to
// several businesses. The password set here is temporary: the owner has to
// choose their own at the first sign-in. Nothing here ever shows a stored
// password or authenticator secret; the server never returns them.

const ow = { tab: "logins", owners: [], tenants: [] };

const OW_TABS = [["logins", "Logins"], ["new", "New login"]];
// No 0/O/1/l/I: temporary passwords get read out or typed from a message.
const OW_ALPHABET = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789";

function owTime(iso) {
  return iso ? new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "never";
}

function owTempPassword(length = 14) {
  const bytes = new Uint32Array(length);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => OW_ALPHABET[b % OW_ALPHABET.length]).join("");
}

function owDashboardUrl() {
  return location.origin + "/owner/";
}

async function openOwners(tab) {
  if (tab) ow.tab = tab;
  $("owners").classList.add("open");
  await owRender();
}

function closeOwners() { $("owners").classList.remove("open"); }

async function owRender() {
  const tabs = $("ow-tabs");
  tabs.textContent = "";
  for (const [key, label] of OW_TABS) {
    const b = el("button", "pf-tab" + (ow.tab === key ? " on" : ""), label);
    b.addEventListener("click", () => { ow.tab = key; owRender(); });
    tabs.appendChild(b);
  }
  const box = $("ow-body");
  box.textContent = "";
  try {
    const [owners, tree] = await Promise.all([api("GET", "/api/owners"), api("GET", "/api/platform/tree")]);
    ow.owners = owners;
    ow.tenants = tree.tenants.map((t) => ({ id: t.id, name: t.name }))
      .sort((a, b) => a.name.localeCompare(b.name) || a.id - b.id);
  } catch (err) { box.appendChild(el("div", "pf-errors", err.message)); return; }
  (ow.tab === "new" ? owNew : owList)(box);
}

// Checkboxes for every business; returns a function giving the ticked ids.
function owTenantPicker(box, selected) {
  const picker = el("div", "picker ow-picker");
  const boxes = [];
  if (!ow.tenants.length) picker.appendChild(el("div", "row-item muted", "No clients yet."));
  for (const t of ow.tenants) {
    const row = el("label", "row-item");
    const input = el("input");
    input.type = "checkbox";
    input.value = String(t.id);
    input.checked = selected.includes(t.id);
    boxes.push(input);
    row.append(input, el("span", null, t.name), el("span", "handle", `#${t.id}`));
    picker.appendChild(row);
  }
  box.appendChild(picker);
  return () => boxes.filter((b) => b.checked).map((b) => Number(b.value));
}

async function owCall(method, path, body, done) {
  try {
    const result = await api(method, path, body);
    if (done) toast(done, "info");
    await owRender();
    return result;
  } catch (err) {
    toast(err.message);
    return null;
  }
}

/* ------------------------------------------------------------- the list */

function owList(box) {
  box.appendChild(el("p", "pf-note", "Each login opens the dashboard at " + owDashboardUrl() +
    " for the businesses ticked below: bookings, weekly numbers and unanswered messages, read-only apart from " +
    "marking a message reviewed. It cannot change any bot setting or pause anything."));
  if (!ow.owners.length) {
    box.appendChild(el("div", "pf-note", "No client logins yet."));
    const add = el("button", "btn primary", "Create the first one");
    add.addEventListener("click", () => { ow.tab = "new"; owRender(); });
    box.appendChild(add);
    return;
  }
  for (const o of ow.owners) box.appendChild(owCard(o));
}

function owCard(o) {
  const card = el("div", "pf-section ow-card" + (o.disabled ? " ow-disabled" : ""));
  const title = el("div", "title");
  title.appendChild(el("span", null, o.username));
  const badges = el("span", "ow-badges");
  if (o.disabled) badges.appendChild(el("span", "badge paused", "disabled"));
  if (o.must_change_password) badges.appendChild(el("span", "badge", "temporary password"));
  if (o.totp) badges.appendChild(el("span", "badge link", "2FA on"));
  title.appendChild(badges);
  card.appendChild(title);
  card.appendChild(el("div", "pf-note ow-meta",
    `${o.display_name || "No name"} · last sign-in ${owTime(o.last_login_at)} · ` +
    `${o.sessions} active session${o.sessions === 1 ? "" : "s"} · created ${owTime(o.created_at)}`));

  // Name
  const nameRow = el("div", "pf-actions");
  const name = el("input");
  name.type = "text";
  name.value = o.display_name;
  name.placeholder = "Name shown on their dashboard";
  name.maxLength = 200;
  const saveName = el("button", "btn small", "Save name");
  saveName.addEventListener("click", () =>
    owCall("PATCH", `/api/owners/${o.id}`, { display_name: name.value }, "Name saved."));
  nameRow.append(name, saveName);
  card.appendChild(nameRow);

  // Businesses
  card.appendChild(el("div", "sf-sub muted", "Businesses this login can see"));
  const picked = owTenantPicker(card, o.tenant_ids);
  const saveLinks = el("button", "btn small", "Save businesses");
  saveLinks.addEventListener("click", () =>
    owCall("PATCH", `/api/owners/${o.id}`, { tenant_ids: picked() }, "Businesses saved."));

  // Actions
  const actions = el("div", "pf-actions");
  actions.appendChild(saveLinks);
  const toggle = el("button", "btn small" + (o.disabled ? "" : " warn"), o.disabled ? "Enable" : "Disable");
  toggle.title = o.disabled ? "Allow this login again" : "Block this login and end its sessions now";
  toggle.addEventListener("click", () => {
    if (!o.disabled && !confirm(`Disable ${o.username}? Their open sessions end at once.`)) return;
    owCall("PATCH", `/api/owners/${o.id}`, { disabled: !o.disabled }, o.disabled ? "Enabled." : "Disabled.");
  });
  actions.appendChild(toggle);

  const reset = el("button", "btn small", "Reset password…");
  reset.addEventListener("click", async () => {
    const suggestion = owTempPassword();
    const password = prompt(`New temporary password for ${o.username} (at least 10 characters). ` +
      "Their sessions end and they choose their own at the next sign-in.", suggestion);
    if (password === null) return;
    const done = await owCall("POST", `/api/owners/${o.id}/reset-password`, { password }, null);
    if (done) owShowSecret(`Temporary password for ${o.username}`, password);
  });
  actions.appendChild(reset);

  if (o.totp) {
    const totp = el("button", "btn small", "Remove 2FA");
    totp.title = "For a lost phone: they sign in with the password alone and can set up a new app";
    totp.addEventListener("click", () => {
      if (!confirm(`Remove the authenticator of ${o.username}? Only do this when you are sure it is them asking.`)) return;
      owCall("DELETE", `/api/owners/${o.id}/totp`, undefined, "Authenticator removed.");
    });
    actions.appendChild(totp);
  }

  const del = el("button", "btn small warn", "Delete");
  del.addEventListener("click", () => {
    if (!confirm(`Delete the login ${o.username}? The businesses and their data stay; only the login goes.`)) return;
    owCall("DELETE", `/api/owners/${o.id}`, undefined, "Login deleted.");
  });
  actions.appendChild(del);
  card.appendChild(actions);
  return card;
}

// A password is shown once, right after it was set, to pass on to the owner.
function owShowSecret(label, password) {
  const box = $("ow-body");
  const note = el("div", "pf-section ow-secret");
  note.appendChild(el("div", "title", label));
  note.appendChild(el("p", "pf-note", "Shown only now. Send it to them together with " + owDashboardUrl() +
    "; they choose their own password when they first sign in."));
  const code = el("code", "ow-password", password);
  note.appendChild(code);
  box.insertBefore(note, box.firstChild);
}

/* ------------------------------------------------------------ new login */

function owNew(box) {
  const form = el("div", "pf-section");
  const field = (label, input) => {
    const wrap = el("div", "field");
    const l = el("label", null, label);
    wrap.append(l, input);
    form.appendChild(wrap);
    return input;
  };
  const username = el("input");
  username.type = "text";
  username.placeholder = "e.g. anna@salon.lv or anna.salon";
  username.autocomplete = "off";
  username.maxLength = 64;
  field("Username (3–64 letters, digits or . _ @ -)", username);
  const name = el("input");
  name.type = "text";
  name.placeholder = "e.g. Anna Kalniņa";
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

  form.appendChild(el("div", "sf-sub muted", "Businesses this login can see"));
  const picked = owTenantPicker(form, []);

  const actions = el("div", "pf-actions");
  const create = el("button", "btn primary", "Create login");
  create.addEventListener("click", async () => {
    create.disabled = true;
    try {
      const owner = await api("POST", "/api/owners", {
        username: username.value.trim(), display_name: name.value.trim(), password: password.value,
        tenant_ids: picked(),
      });
      const temp = password.value;
      toast(`Login ${owner.username} created.`, "info");
      ow.tab = "logins";
      await owRender();
      owShowSecret(`Temporary password for ${owner.username}`, temp);
    } catch (err) {
      toast(err.message);
    } finally {
      create.disabled = false;
    }
  });
  actions.appendChild(create);
  form.appendChild(actions);
  box.appendChild(form);
  username.focus();
}

$("open-owners").addEventListener("click", () => openOwners());
$("ow-close").addEventListener("click", closeOwners);
$("owners").addEventListener("click", (ev) => { if (ev.target === $("owners")) closeOwners(); });
