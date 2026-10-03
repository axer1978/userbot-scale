"use strict";

/* ---------------------------------------------------------------- staff */
// Staff roles and the approval queue (staff_api.py, staff.py). A role sets
// every action of the catalogue to Off, Allowed or Needs my approval. A
// change that needs approval answers the moderator as if it was done, but
// waits here: approving runs it now, exactly as sent; rejecting drops it
// and the moderator is not told. Protective changes (pausing a bot or a
// chat, disabling a login…) run at once whatever the level. The server
// refuses all of /api/staff/ to staff, so this is the admin's alone.

const sr = {
  tab: "waiting",
  pending: 0,
  roles: [],
  catalogue: [],
  draft: null,     // the role being edited: a copy, saved only with Save
  dirty: false,
  status: "all",   // Activity filters
  managerId: "",
};

const SR_TABS = [["waiting", "Waiting for you"], ["activity", "Activity"], ["roles", "Roles"]];
const SR_LEVELS = [["off", "Off"], ["allow", "Allowed"], ["approve", "Needs my approval"]];
const SR_STATUS = {
  applied: ["done", "badge link"],
  pending: ["pending", "badge paused"],
  approved: ["approved", "badge link"],
  rejected: ["rejected", "badge"],
  failed: ["failed", "badge escalated"],
};
const SR_FILTERS = [["all", "Every status"], ["applied", "Done at once"], ["pending", "Waiting"],
                    ["approved", "Approved"], ["rejected", "Rejected"], ["failed", "Failed"]];

function srTime(iso) {
  return iso ? new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "—";
}

async function openStaff(tab) {
  if (!isAdmin()) return;
  if (tab) sr.tab = tab;
  $("staff").classList.add("open");
  await srRender();
}

function closeStaff() {
  if (sr.dirty && !confirm("Drop the unsaved changes to this role?")) return;
  if (sr.dirty) { sr.draft = null; sr.dirty = false; }
  $("staff").classList.remove("open");
}

/* ------------------------------------------------------- top-bar badge */

function srApplyCount(n) {
  sr.pending = n || 0;
  $("staff-count").textContent = sr.pending ? String(sr.pending) : "";
}

async function stPoll() {
  if ($("admin-gate").classList.contains("open") || !isAdmin()) return;
  try {
    srApplyCount((await api("GET", "/api/staff/requests?status=pending&limit=1")).pending);
    if ($("staff").classList.contains("open")) srTabs();
  } catch (_) {}
}

/* -------------------------------------------------------------- frame */

function srTabs() {
  const tabs = $("sr-tabs");
  tabs.textContent = "";
  for (const [key, label] of SR_TABS) {
    const b = el("button", "pf-tab" + (sr.tab === key ? " on" : ""), label);
    if (key === "waiting") b.appendChild(el("span", "count-badge", sr.pending ? String(sr.pending) : ""));
    b.addEventListener("click", () => {
      if (sr.tab === "roles" && key !== "roles" && sr.dirty) {
        if (!confirm("Drop the unsaved changes to this role?")) return;
        sr.draft = null;
        sr.dirty = false;
      }
      sr.tab = key;
      srRender();
    });
    tabs.appendChild(b);
  }
}

async function srRender() {
  srTabs();
  const box = $("sr-body");
  box.textContent = "";
  box.appendChild(el("div", "pf-note", "Loading…"));
  const tab = sr.tab;
  const draw = tab === "activity" ? srActivity : tab === "roles" ? srRoles : srWaiting;
  try {
    const data = await srLoad(tab);
    if (sr.tab !== tab) return; // switched tab while loading
    srTabs();
    box.textContent = "";
    draw(box, data);
  } catch (err) {
    if (sr.tab !== tab) return;
    box.textContent = "";
    box.appendChild(el("div", "pf-errors", err.message));
  }
}

async function srLoad(tab) {
  if (tab === "roles") {
    const data = await api("GET", "/api/staff/roles");
    sr.roles = data.roles;
    sr.catalogue = data.catalogue;
    return data;
  }
  if (tab === "activity") {
    const query = new URLSearchParams({ status: sr.status });
    if (sr.managerId) query.set("manager_id", sr.managerId);
    const [data, managers] = await Promise.all([
      api("GET", "/api/staff/requests?" + query.toString()),
      api("GET", "/api/managers").catch(() => []),
    ]);
    srApplyCount(data.pending);
    return { requests: data.requests, managers };
  }
  const data = await api("GET", "/api/staff/requests?status=pending");
  srApplyCount(data.pending);
  return data;
}

/* ------------------------------------------------------ shared pieces */

function srWho(r) {
  return r.username + (r.role_name ? ` (${r.role_name})` : "");
}

function srBusiness(r) {
  if (r.tenant_name) return r.tenant_name;
  return r.tenant_id ? `Client ${r.tenant_id}` : "";
}

function srStatusBadge(status) {
  const [label, cls] = SR_STATUS[status] || [status, "badge"];
  return el("span", cls, label);
}

// The JSON that was sent, pretty-printed when it is JSON.
function srPretty(body) {
  if (!body) return "";
  try { return JSON.stringify(JSON.parse(body), null, 2); } catch (_) { return body; }
}

function srChange(r) {
  const wrap = el("div", "sr-change");
  const text = srPretty(r.body);
  wrap.appendChild(el("pre", "pf-rendered sr-sent", text || "(nothing was sent with it)"));
  wrap.appendChild(el("div", "sr-route", `${r.method} ${r.path}${r.query ? "?" + r.query : ""}`));
  return wrap;
}

// What the app answered when an approved change was run: its "detail"
// when it is one of our JSON errors, else the text as it came.
function srResultText(r) {
  const body = r.result_body || "";
  try {
    const data = JSON.parse(body);
    if (data && typeof data.detail === "string") return data.detail;
    if (data && data.detail) return JSON.stringify(data.detail);
  } catch (_) {}
  return body || `error ${r.result_status}`;
}

/* ------------------------------------------------- waiting for you */

function srWaiting(box, data) {
  box.appendChild(el("p", "pf-note", "Moderators see these as done. Approving runs them now, exactly as sent; " +
    "rejecting drops them and the moderator is not told."));
  if (!data.requests.length) {
    box.appendChild(el("div", "pf-note", "Nothing waits for you."));
    return;
  }
  for (const r of data.requests) box.appendChild(srRequestCard(r));
}

function srRequestCard(r) {
  const card = el("div", "pf-section sr-request");
  const title = el("div", "title");
  title.appendChild(el("span", null, r.action_label));
  const badge = srStatusBadge(r.status);
  title.appendChild(badge);
  card.appendChild(title);
  const meta = [srWho(r), srTime(r.created_at)];
  const business = srBusiness(r);
  if (business) meta.push("business: " + business);
  card.appendChild(el("div", "pf-note sr-meta", meta.join(" · ")));
  card.appendChild(srChange(r));

  const actions = el("div", "pf-actions");
  const note = el("input");
  note.type = "text";
  note.maxLength = 500;
  note.placeholder = "Note (optional)";
  const approve = el("button", "btn small primary", "Approve");
  const reject = el("button", "btn small warn", "Reject");
  const result = el("div", "sr-result");
  const decide = async (verb) => {
    if (verb === "reject" && !confirm(`Reject this change by ${r.username}? It is never done, and they are not told.`)) {
      return;
    }
    approve.disabled = true;
    reject.disabled = true;
    try {
      const row = await api("POST", `/api/staff/requests/${r.id}/${verb}`, { note: note.value.trim() });
      actions.remove();
      badge.replaceWith(srStatusBadge(row.status));
      if (verb === "reject") {
        result.appendChild(el("div", "pf-note", "Rejected: it was not done."));
      } else if (row.result_status >= 400) {
        result.appendChild(el("div", "pf-errors", "It could not be done: " + srResultText(row)));
      } else {
        result.appendChild(el("div", "sr-ok", "Approved and done."));
      }
      card.classList.add("sr-decided");
      stPoll();
    } catch (err) {
      toast(err.message);
      approve.disabled = false;
      reject.disabled = false;
      // Decided elsewhere (another tab) or gone: show the queue as it is.
      if (err.status === 409 || err.status === 404) srRender();
    }
  };
  approve.addEventListener("click", () => decide("approve"));
  reject.addEventListener("click", () => decide("reject"));
  actions.append(note, approve, reject);
  card.append(actions, result);
  return card;
}

/* ------------------------------------------------------------ activity */

function srActivity(box, data) {
  const bar = el("div", "bk-nav");
  const status = el("select");
  for (const [value, label] of SR_FILTERS) {
    const option = el("option", null, label);
    option.value = value;
    status.appendChild(option);
  }
  status.value = sr.status;
  status.addEventListener("change", () => { sr.status = status.value; srRender(); });
  const who = el("select");
  const anyone = el("option", null, "Every manager");
  anyone.value = "";
  who.appendChild(anyone);
  for (const m of data.managers) {
    const option = el("option", null, m.username + (m.role_name ? ` (${m.role_name})` : ""));
    option.value = String(m.id);
    who.appendChild(option);
  }
  who.value = sr.managerId;
  who.addEventListener("change", () => { sr.managerId = who.value; srRender(); });
  bar.append(status, who);
  box.appendChild(bar);
  box.appendChild(el("p", "pf-note", "Every change a moderator made, newest first (the last 200). " +
    "\"done\" ran at once: the role allows it, or it only protects (a pause, say)."));
  if (!data.requests.length) {
    box.appendChild(el("div", "pf-note", "Nothing here."));
    return;
  }
  const table = el("table", "cfg-table sr-table");
  const head = el("tr");
  for (const label of ["Time", "Who", "Change", "Business", "Status", "Note"]) head.appendChild(el("th", null, label));
  table.appendChild(head);
  for (const r of data.requests) {
    const tr = el("tr");
    tr.appendChild(el("td", "sr-when", srTime(r.created_at)));
    tr.appendChild(el("td", null, srWho(r)));
    const change = el("td");
    change.appendChild(el("div", null, r.action_label));
    const details = el("details", "sr-details");
    details.appendChild(el("summary", null, "what was sent"));
    details.appendChild(srChange(r));
    change.appendChild(details);
    tr.appendChild(change);
    tr.appendChild(el("td", null, srBusiness(r) || "—"));
    const cell = el("td");
    cell.appendChild(srStatusBadge(r.status));
    tr.appendChild(cell);
    const note = el("td", "sr-note");
    if (r.note) note.appendChild(el("div", null, r.note));
    if (r.decided_at) {
      note.appendChild(el("div", "muted", `${r.status === "rejected" ? "Rejected" : "Approved"}` +
        `${r.decided_by ? " by " + r.decided_by : ""}, ${srTime(r.decided_at)}`));
    }
    if (r.decision_note) note.appendChild(el("div", null, "“" + r.decision_note + "”"));
    if (r.status === "failed") note.appendChild(el("div", "warn-note", "It could not be done: " + srResultText(r)));
    tr.appendChild(note);
    table.appendChild(tr);
  }
  box.appendChild(table);
}

/* --------------------------------------------------------------- roles */

function srDraftOf(role) {
  return {
    id: role.id, name: role.name, description: role.description || "", admin_panel: !!role.admin_panel,
    permissions: Object.assign({}, role.permissions || {}), members: role.members || 0,
  };
}

function srBlankRole() {
  return { id: null, name: "", description: "", admin_panel: false, permissions: {}, members: 0 };
}

// Switching to another role (or a new one) drops unsaved edits, after asking.
function srPick(draft) {
  if (sr.dirty && !confirm("Drop the unsaved changes to this role?")) return;
  sr.draft = draft;
  sr.dirty = false;
  srRolesDraw();
}

function srRolesDraw() {
  const box = $("sr-body");
  box.textContent = "";
  srRoles(box, { roles: sr.roles, catalogue: sr.catalogue });
}

function srRoles(box, data) {
  box.appendChild(el("p", "pf-note", "Each action is Off (refused, and that part of the panel is hidden), " +
    "Allowed (done at once) or Needs my approval: the change looks done to the moderator, but it waits under " +
    "\"Waiting for you\" until you approve it. Reads can only be Off or Allowed. Staff, roles and this queue " +
    "are always yours alone. A role's changes apply to its members from their next click."));
  const roles = data.roles;
  if (sr.draft && sr.draft.id !== null && !roles.some((r) => r.id === sr.draft.id)) {
    sr.draft = null;
    sr.dirty = false;
  }
  if (!sr.draft && roles.length) sr.draft = srDraftOf(roles[0]);

  const wrap = el("div", "sr-roles");
  const list = el("div", "sr-role-list");
  for (const r of roles) {
    const b = el("button", "sr-role" + (sr.draft && sr.draft.id === r.id ? " on" : ""));
    b.type = "button";
    b.appendChild(el("span", "sr-role-name", r.name));
    b.appendChild(el("span", "muted", `${r.members} member${r.members === 1 ? "" : "s"}` +
      (r.admin_panel ? " · admin panel" : "")));
    b.addEventListener("click", () => { if (!sr.draft || sr.draft.id !== r.id) srPick(srDraftOf(r)); });
    list.appendChild(b);
  }
  const add = el("button", "btn small" + (sr.draft && sr.draft.id === null ? " primary" : ""), "New role");
  add.type = "button";
  add.addEventListener("click", () => { if (!sr.draft || sr.draft.id !== null) srPick(srBlankRole()); });
  list.appendChild(add);

  const editor = el("div", "sr-editor");
  wrap.append(list, editor);
  box.appendChild(wrap);
  srEditor(editor, data.catalogue);
}

function srEditor(box, catalogue) {
  const d = sr.draft;
  if (!d) {
    box.appendChild(el("div", "pf-note", "No roles yet. Make one with New role."));
    return;
  }
  const head = el("div", "pf-section");
  const title = el("div", "title");
  title.appendChild(el("span", null, d.id === null ? "New role" : d.name));
  if (d.id !== null) {
    title.appendChild(el("span", "muted", `${d.members} manager${d.members === 1 ? "" : "s"} have this role`));
  }
  head.appendChild(title);

  const nameField = el("div", "field sr-field");
  const name = el("input");
  name.type = "text";
  name.maxLength = 80;
  name.value = d.name;
  name.placeholder = "e.g. Night moderator";
  name.addEventListener("input", () => { d.name = name.value; sr.dirty = true; });
  nameField.append(el("label", null, "Name"), name);
  head.appendChild(nameField);

  const descField = el("div", "field");
  const desc = el("textarea");
  desc.rows = 2;
  desc.maxLength = 500;
  desc.value = d.description;
  desc.placeholder = "What this role is for (only you see it)";
  desc.addEventListener("input", () => { d.description = desc.value; sr.dirty = true; });
  descField.append(el("label", null, "Description"), desc);
  head.appendChild(descField);

  const check = el("div", "field check");
  const panel = el("input");
  panel.type = "checkbox";
  panel.id = "sr-admin-panel";
  panel.checked = d.admin_panel;
  panel.addEventListener("change", () => { d.admin_panel = panel.checked; sr.dirty = true; });
  const panelLabel = el("label", null, "Can sign in to the admin panel");
  panelLabel.htmlFor = "sr-admin-panel";
  check.append(panel, panelLabel);
  head.appendChild(check);
  head.appendChild(el("p", "pf-note sr-hint", "With it, members sign in at " + location.origin + "/ with their " +
    "moderator username, password and authenticator code, and see only what the role allows. " +
    "Without it, only the moderator panel at " + location.origin + "/manager/."));
  box.appendChild(head);

  // The matrix, one section per group, in the catalogue's order.
  const groups = [];
  for (const action of catalogue) {
    let group = groups.find((g) => g.name === action.group);
    if (!group) { group = { name: action.group, actions: [] }; groups.push(group); }
    group.actions.push(action);
  }
  for (const group of groups) box.appendChild(srGroup(group));

  const actions = el("div", "pf-actions sr-save");
  const save = el("button", "btn primary", d.id === null ? "Create role" : "Save role");
  save.addEventListener("click", async () => {
    const body = {
      name: d.name.trim(), description: d.description.trim(), admin_panel: d.admin_panel,
      permissions: srCleanPermissions(d.permissions),
    };
    if (!body.name) { toast("Give the role a name."); name.focus(); return; }
    save.disabled = true;
    try {
      const role = d.id === null
        ? await api("POST", "/api/staff/roles", body)
        : await api("PUT", `/api/staff/roles/${d.id}`, body);
      toast(`Role ${role.name} saved.`, "info");
      sr.draft = srDraftOf(role);
      sr.dirty = false;
      await srRender();
    } catch (err) {
      toast(err.message);
    } finally {
      save.disabled = false;
    }
  });
  actions.appendChild(save);
  const discard = el("button", "btn", "Discard changes");
  discard.addEventListener("click", () => {
    const saved = d.id === null ? null : sr.roles.find((r) => r.id === d.id);
    sr.dirty = false;
    sr.draft = saved ? srDraftOf(saved) : null;
    srRolesDraw();
  });
  actions.appendChild(discard);
  if (d.id !== null) {
    const del = el("button", "btn warn", "Delete role");
    del.addEventListener("click", async () => {
      if (d.members) {
        toast(`${d.members} manager${d.members === 1 ? " has" : "s have"} this role. Give them another role first ` +
          "(Managers).");
        return;
      }
      if (!confirm(`Delete the role ${d.name}?`)) return;
      try {
        await api("DELETE", `/api/staff/roles/${d.id}`);
        toast("Role deleted.", "info");
        sr.draft = null;
        sr.dirty = false;
        await srRender();
      } catch (err) { toast(err.message); }
    });
    actions.appendChild(del);
  }
  box.appendChild(actions);
}

// Only what is on; reads are never "approve" (the server drops that too).
function srCleanPermissions(perms) {
  const out = {};
  for (const action of sr.catalogue) {
    const level = perms[action.key];
    if (level === "allow" || (level === "approve" && !action.view)) out[action.key] = level;
    else if (level === "approve" && action.view) out[action.key] = "allow";
  }
  return out;
}

function srSet(key, level) {
  if (level === "off") delete sr.draft.permissions[key];
  else sr.draft.permissions[key] = level;
  sr.dirty = true;
}

function srGroup(group) {
  const sec = el("div", "pf-section sr-group");
  const title = el("div", "title");
  title.appendChild(el("span", null, group.name));
  const quick = el("span", "sr-quick");
  const segs = [];
  const allViews = group.actions.every((a) => a.view);
  const quickLevels = [["off", "All off"], ["allow", "All allowed"]];
  if (!allViews) quickLevels.push(["approve", "All need approval"]);
  for (const [level, label] of quickLevels) {
    const b = el("button", "btn small", label);
    b.type = "button";
    b.addEventListener("click", () => {
      for (const action of group.actions) {
        srSet(action.key, level === "approve" && action.view ? "allow" : level);
      }
      for (const redraw of segs) redraw();
    });
    quick.appendChild(b);
  }
  title.appendChild(quick);
  sec.appendChild(title);

  for (const action of group.actions) {
    const row = el("div", "sr-action");
    const text = el("div", "sr-action-text");
    text.appendChild(el("div", null, action.label));
    text.appendChild(el("div", "sr-action-key", action.key));
    if (action.protective) {
      text.appendChild(el("div", "sr-protective", "protective: runs at once even when it needs approval"));
    }
    const [seg, redraw] = srSeg(action);
    segs.push(redraw);
    row.append(text, seg);
    sec.appendChild(row);
  }
  return sec;
}

// Off / Allowed / Needs my approval (reads: Off / Allowed).
function srSeg(action) {
  const seg = el("div", "sr-seg");
  seg.setAttribute("role", "group");
  seg.setAttribute("aria-label", action.label);
  const redraw = () => {
    seg.textContent = "";
    const current = sr.draft.permissions[action.key] || "off";
    for (const [level, label] of SR_LEVELS) {
      if (action.view && level === "approve") continue;
      const on = current === level || (action.view && level === "allow" && current === "approve");
      const b = el("button", `sr-level sr-${level}` + (on ? " on" : ""), label);
      b.type = "button";
      b.setAttribute("aria-pressed", String(on));
      b.addEventListener("click", () => { srSet(action.key, level); redraw(); });
      seg.appendChild(b);
    }
  };
  redraw();
  return [seg, redraw];
}

/* ----------------------------------------------------------------- wire */

$("open-staff").addEventListener("click", () => openStaff());
$("sr-close").addEventListener("click", closeStaff);
$("staff").addEventListener("click", (ev) => { if (ev.target === $("staff")) closeStaff(); });
setInterval(stPoll, 60000);
