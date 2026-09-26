"use strict";

/* ------------------------------------------------------------- clients */
// Industry folders with their clients, plus the platform rules. How a
// client's bot behaves (config), what it knows (prompt), and the version
// history of both are edited here, against platform_api.py. The account's
// pause switch and per-contact styles stay in the top bar and Style sheet.

const platform = { tree: null, node: null, tab: null, view: null, audit: null, proposal: null };

const TABS = {
  tenant: [["config", "Config"], ["prompt", "Prompt"], ["versions", "Versions"],
           ["preview", "Rendered prompt"], ["assist", "Ask AI"], ["audit", "Audit log"]],
  industry: [["template", "Template"], ["config", "Default config"], ["versions", "Versions"], ["clients", "Clients"]],
  base: [["rules", "Rules"], ["versions", "Versions"]],
};

async function openPlatform(node) {
  $("platform").classList.add("open");
  try { await loadTree(); } catch (err) { toast(err.message); return; }
  if (node) await selectNode(node);
  else if (!platform.node) renderDetail();
}

function closePlatform() { $("platform").classList.remove("open"); }

async function loadTree() {
  platform.tree = await api("GET", "/api/platform/tree");
  renderTree();
}

function sameNode(a, b) { return a && b && a.kind === b.kind && a.id === b.id; }

function treeNode(label, node, cls, extra) {
  const row = el("div", "pf-node " + cls + (sameNode(node, platform.node) ? " active" : ""), label);
  if (extra) row.appendChild(extra);
  row.addEventListener("click", () => selectNode(node));
  return row;
}

function renderTree() {
  const nav = $("pf-tree");
  nav.textContent = "";
  const t = platform.tree;
  nav.appendChild(treeNode("Platform rules", { kind: "base", id: 0 }, "folder",
    el("span", "count", `v${t.base_version}`)));
  for (const industry of t.industries) {
    const clients = t.tenants.filter((c) => c.industry_id === industry.id);
    nav.appendChild(treeNode("📁 " + industry.name, { kind: "industry", id: industry.id }, "folder",
      el("span", "count", String(clients.length))));
    for (const client of clients) {
      const row = treeNode(client.name, { kind: "tenant", id: client.id }, "client");
      if (client.status !== "active") row.appendChild(el("span", "count", client.status));
      nav.appendChild(row);
    }
  }
  const actions = el("div", "pf-tree-actions");
  const add = el("button", "btn small", "+ New industry");
  add.addEventListener("click", async () => {
    const name = (prompt("Name of the new industry:") || "").trim();
    if (!name) return;
    try {
      const created = await api("POST", "/api/industries", { name });
      await loadTree();
      await selectNode({ kind: "industry", id: created.id });
    } catch (err) { toast(err.message); }
  });
  actions.appendChild(add);
  nav.appendChild(actions);
}

async function selectNode(node, tab) {
  platform.node = node;
  platform.tab = tab || TABS[node.kind][0][0];
  platform.proposal = null;
  platform.audit = null;
  renderTree();
  await loadDetail();
}

async function loadDetail() {
  const n = platform.node;
  const path = n.kind === "tenant" ? `/api/tenants/${n.id}`
    : n.kind === "industry" ? `/api/industries/${n.id}` : "/api/platform/base";
  try { platform.view = await api("GET", path); } catch (err) { toast(err.message); return; }
  if (n.kind === "tenant" && platform.tab === "audit") await loadAudit();
  renderDetail();
}

async function loadAudit() {
  platform.audit = await api("GET", `/api/audit?tenant_id=${platform.node.id}`);
}

function renderDetail() {
  const box = $("pf-detail");
  box.textContent = "";
  const n = platform.node;
  if (!n || !platform.view) {
    box.appendChild(el("div", "empty", "Pick a client, an industry, or the platform rules on the left."));
    $("pf-crumb").textContent = "";
    return;
  }
  box.appendChild(n.kind === "tenant" ? tenantHead() : n.kind === "industry" ? industryHead() : baseHead());
  const tabs = el("div", "pf-tabs");
  for (const [key, label] of TABS[n.kind]) {
    const b = el("button", "pf-tab" + (platform.tab === key ? " on" : ""), label);
    b.addEventListener("click", async () => {
      platform.tab = key;
      if (key === "audit") { try { await loadAudit(); } catch (err) { toast(err.message); } }
      renderDetail();
    });
    tabs.appendChild(b);
  }
  box.appendChild(tabs);
  const body = el("div");
  box.appendChild(body);
  const render = {
    tenant: { config: tenantConfigTab, prompt: tenantPromptTab, versions: tenantVersionsTab,
              preview: tenantPreviewTab, assist: tenantAssistTab, audit: tenantAuditTab },
    industry: { template: industryTemplateTab, config: industryConfigTab, versions: industryVersionsTab,
                clients: industryClientsTab },
    base: { rules: baseRulesTab, versions: baseVersionsTab },
  }[n.kind][platform.tab];
  render(body);
}

/* ------------------------------------------------------------- heads */

function tenantHead() {
  const v = platform.view;
  $("pf-crumb").textContent = `${v.industry.name} › ${v.tenant.name}`;
  const head = el("div", "pf-head");
  head.appendChild(el("h3", null, v.tenant.name));
  head.appendChild(el("span", "tag", v.tenant.status));
  if (v.tenant.session_id) head.appendChild(el("span", "muted", v.tenant.session_id));
  head.appendChild(el("span", "spacer"));
  const rename = el("button", "btn small", "Rename");
  rename.addEventListener("click", async () => {
    const name = (prompt("New name for this client:", v.tenant.name) || "").trim();
    if (!name || name === v.tenant.name) return;
    await patchTenant({ name, reason: "renamed" });
  });
  const industry = el("select");
  for (const i of platform.tree.industries) {
    const o = el("option", null, "Industry: " + i.name);
    o.value = String(i.id);
    o.selected = i.id === v.tenant.industry_id;
    industry.appendChild(o);
  }
  industry.addEventListener("change", async () => {
    const target = platform.tree.industries.find((i) => String(i.id) === industry.value);
    if (!confirm(`Move ${v.tenant.name} to ${target.name}? It will inherit that industry's template and ` +
                 "defaults, and any pinned template version is released.")) { renderDetail(); return; }
    await patchTenant({ industry_id: target.id, reason: "moved to " + target.name });
  });
  head.appendChild(rename);
  head.appendChild(industry);
  return head;
}

async function patchTenant(body) {
  try {
    await api("PATCH", `/api/tenants/${platform.node.id}`, body);
    await loadTree();
    await loadDetail();
    toast("Saved.", "info");
  } catch (err) { toast(err.message); }
}

function industryHead() {
  const v = platform.view;
  $("pf-crumb").textContent = v.industry.name;
  const head = el("div", "pf-head");
  head.appendChild(el("h3", null, v.industry.name));
  head.appendChild(el("span", "muted", `live template v${v.industry.template_version} · ${v.tenants.length} client(s)`));
  return head;
}

function baseHead() {
  $("pf-crumb").textContent = "Platform rules";
  const head = el("div", "pf-head");
  head.appendChild(el("h3", null, "Platform rules"));
  head.appendChild(el("span", "muted", `v${platform.view.current.version} · first in every client's prompt; nothing below can override them`));
  return head;
}

/* ------------------------------------------------------ config editor */
// One table for both layers that have config: an industry's defaults (over
// the platform's) and a client's overrides (over its industry's). Rows the
// layer does not set are greyed and read-only until "Override" is pressed;
// rows it sets are highlighted and can be reset to inherit.

function getPath(obj, path) {
  let node = obj;
  for (const part of path.split(".")) {
    if (node === null || typeof node !== "object" || !(part in node)) return undefined;
    node = node[part];
  }
  return node;
}

function setPath(obj, path, value) {
  const parts = path.split(".");
  let node = obj;
  for (const part of parts.slice(0, -1)) node = node[part] = node[part] || {};
  node[parts[parts.length - 1]] = value;
}

function valueText(kind, value) {
  if (kind === "list") return (value || []).join("\n");
  if (kind === "map") return Object.entries(value || {}).map(([k, v]) => `${k} = ${v}`).join("\n");
  return value === null || value === undefined ? "" : String(value);
}

function parseValue(kind, input) {
  if (kind === "bool") return input.checked;
  if (kind === "int" || kind === "float") return input.value.trim() === "" ? null : Number(input.value);
  if (kind === "list") return input.value.split("\n").map((s) => s.trim()).filter(Boolean);
  if (kind === "map") {
    const out = {};
    for (const line of input.value.split("\n")) {
      if (!line.trim()) continue;
      const i = line.lastIndexOf("=");
      out[(i < 0 ? line : line.slice(0, i)).trim()] = i < 0 ? NaN : Number(line.slice(i + 1).trim());
    }
    return out;
  }
  return input.value;
}

function fieldInput(field, value) {
  let input;
  if (field.kind === "bool") {
    input = el("input");
    input.type = "checkbox";
    input.checked = !!value;
  } else if (field.kind === "choice") {
    input = el("select");
    for (const c of field.choices) {
      const o = el("option", null, String(c));
      o.value = c;
      o.selected = c === value;
      input.appendChild(o);
    }
  } else if (field.kind === "list" || field.kind === "map") {
    input = el("textarea");
    input.rows = Math.min(6, Math.max(2, valueText(field.kind, value).split("\n").length));
    input.placeholder = field.kind === "map" ? "one per line: service = price" : "one per line";
    input.value = valueText(field.kind, value);
  } else {
    input = el("input");
    input.type = field.kind === "int" || field.kind === "float" ? "number" : "text";
    if (field.kind === "float") input.step = "any";
    input.value = valueText(field.kind, value);
  }
  return input;
}

function configEditor(box, { fields, overrides, layer, inheritedLabel, revision, save }) {
  box.appendChild(el("p", "pf-note",
    `Greyed rows are inherited from ${inheritedLabel}. Highlighted rows are set here. ` +
    "Values are checked against the schema when you save; nothing is clamped."));
  const table = el("table", "cfg-table");
  const rows = [];
  let group = null;
  for (const field of fields) {
    const top = field.path.includes(".") ? field.path.split(".")[0] : null;
    if (top !== group) {
      group = top;
      if (top) {
        const g = el("tr", "cfg-group");
        const td = el("td", null, top.replace(/_/g, " "));
        td.colSpan = 4;
        g.appendChild(td);
        table.appendChild(g);
      }
    }
    const raw = getPath(overrides, field.path);
    const appending = field.kind === "list" && raw && typeof raw === "object" && !Array.isArray(raw);
    const row = { field, set: field.source === layer, mode: appending ? "append" : "replace", dirty: false };
    const tr = el("tr", "cfg-row");
    const pathCell = el("td", "path", field.path);
    const srcCell = el("td");
    const valCell = el("td", "val");
    const actCell = el("td", "act");
    tr.append(pathCell, srcCell, valCell, actCell);
    row.tr = tr;

    const draw = () => {
      tr.className = "cfg-row " + (row.set ? "overridden" : "inherited") + (row.dirty ? " dirty" : "");
      srcCell.textContent = "";
      srcCell.appendChild(el("span", "src " + (row.set ? layer : field.source === layer ? "platform" : field.source),
        row.set ? (row.dirty ? "changed" : layer) : (field.source === layer ? "inherit" : field.source)));
      valCell.textContent = "";
      const shown = row.set
        ? (row.input ? parseValue(field.kind, row.input) : (appending ? raw.append : (raw === undefined ? field.value : raw)))
        : field.inherited_value !== undefined && field.source === layer ? field.inherited_value : field.value;
      row.input = fieldInput(field, shown);
      row.input.disabled = !row.set;
      row.input.addEventListener("input", () => { row.dirty = true; tr.classList.add("dirty"); });
      row.input.addEventListener("change", () => { row.dirty = true; tr.classList.add("dirty"); });
      valCell.appendChild(row.input);
      if (row.set && field.kind === "list") {
        const mode = el("select");
        for (const [v, label] of [["replace", "replaces the inherited list"], ["append", "adds to the inherited list"]]) {
          const o = el("option", null, label);
          o.value = v;
          o.selected = row.mode === v;
          mode.appendChild(o);
        }
        mode.style.marginTop = "4px";
        mode.addEventListener("change", () => { row.mode = mode.value; row.dirty = true; tr.classList.add("dirty"); });
        valCell.appendChild(mode);
      }
      if (row.error) valCell.appendChild(el("div", "err", row.error));
      actCell.textContent = "";
      const toggle = el("button", "btn small", row.set ? "Inherit" : "Override");
      toggle.title = row.set ? `Remove this value and use the one from ${inheritedLabel}` : "Set a value here";
      toggle.addEventListener("click", () => {
        row.set = !row.set;
        row.dirty = true;
        row.input = null;
        row.error = null;
        draw();
      });
      actCell.appendChild(toggle);
    };
    row.draw = draw;
    draw();
    rows.push(row);
    table.appendChild(tr);
  }
  box.appendChild(table);

  const errors = el("div", "pf-errors");
  box.appendChild(errors);
  const actions = el("div", "pf-actions");
  const reason = el("input");
  reason.placeholder = "Reason for the change (goes into the audit log)";
  const button = el("button", "btn primary", "Save config");
  actions.append(reason, button);
  box.appendChild(actions);

  button.addEventListener("click", async () => {
    const out = {};
    for (const row of rows) {
      if (!row.set) continue;
      let value = parseValue(row.field.kind, row.input);
      if (row.field.kind === "list" && row.mode === "append") value = { append: value };
      setPath(out, row.field.path, value);
    }
    errors.textContent = "";
    button.disabled = true;
    try {
      await save({ overrides: out, reason: reason.value.trim(), expected_revision: revision });
      toast("Config saved.", "info");
    } catch (err) {
      errors.textContent = err.message;
      for (const row of rows) {
        const hit = (err.errors || []).find((e) => e.path === row.field.path || e.path.startsWith(row.field.path + "."));
        row.error = hit ? hit.message : null;
        if (hit) row.draw();
      }
    } finally { button.disabled = false; }
  });
}

/* ------------------------------------------------------------ tenant */

function tenantConfigTab(box) {
  const v = platform.view;
  configEditor(box, {
    fields: v.config.fields, overrides: v.config.overrides, layer: "client",
    inheritedLabel: `the ${v.industry.name} industry or the platform defaults`, revision: v.config.revision,
    save: async (body) => {
      platform.view = await api("PUT", `/api/tenants/${platform.node.id}/config`, body);
      renderDetail();
    },
  });
}

function tenantPromptTab(box) {
  const v = platform.view;
  box.appendChild(el("p", "pf-note",
    "Each section comes from the industry template. Override replaces it for this client, append adds " +
    "to it. The platform rules always come first and cannot be changed from here; see the Rendered prompt tab."));
  const editors = [];
  for (const section of v.prompt.sections) {
    const card = el("div", "pf-section" + (section.override ? " overridden" : ""));
    const title = el("div", "title", section.heading);
    const mode = el("select");
    for (const [value, label] of [["inherit", "Inherit from industry"], ["override", "Override"], ["append", "Append"]]) {
      const o = el("option", null, label);
      o.value = value;
      o.selected = (section.override ? section.override.mode : "inherit") === value;
      mode.appendChild(o);
    }
    title.appendChild(mode);
    card.appendChild(title);
    card.appendChild(el("div", "inherited-text", section.inherited || "(the industry template leaves this empty)"));
    const text = el("textarea");
    text.rows = 3;
    text.value = section.override ? section.override.text : "";
    text.placeholder = "This client's text";
    const sync = () => {
      text.style.display = mode.value === "inherit" ? "none" : "";
      card.classList.toggle("overridden", mode.value !== "inherit");
    };
    mode.addEventListener("change", sync);
    sync();
    card.appendChild(text);
    box.appendChild(card);
    editors.push({ key: section.key, mode, text });
  }
  const addCard = el("div", "pf-section");
  addCard.appendChild(el("div", "title", "Additional notes from the business"));
  const addendum = el("textarea");
  addendum.rows = 3;
  addendum.maxLength = v.prompt.addendum_limit;
  addendum.value = v.prompt.addendum;
  const count = el("div", "pf-note");
  const counter = () => { count.textContent = `${addendum.value.length} / ${v.prompt.addendum_limit} characters`; };
  addendum.addEventListener("input", counter);
  counter();
  addCard.append(addendum, count);
  box.appendChild(addCard);

  const actions = el("div", "pf-actions");
  const note = el("input");
  note.placeholder = "What changed (saved with the new version)";
  const button = el("button", "btn primary", "Save as new version");
  actions.append(note, button);
  box.appendChild(actions);
  button.addEventListener("click", async () => {
    const overrides = {};
    for (const e of editors) {
      if (e.mode.value !== "inherit" && e.text.value.trim()) overrides[e.key] = { mode: e.mode.value, text: e.text.value };
    }
    button.disabled = true;
    try {
      platform.view = await api("PUT", `/api/tenants/${platform.node.id}/prompt`,
        { overrides, addendum: addendum.value, note: note.value.trim() });
      toast(`Saved as client version ${platform.view.prompt.client_version}.`, "info");
      renderDetail();
    } catch (err) { toast(err.message); } finally { button.disabled = false; }
  });
}

function versionList(versions, currentVersion, actionLabel, onPick) {
  const list = el("div", "pf-list");
  if (!versions.length) list.appendChild(el("p", "pf-note", "No versions yet."));
  for (const ver of versions) {
    const row = el("div", "v" + (ver.version === currentVersion ? " current" : ""));
    row.appendChild(el("strong", null, "v" + ver.version));
    const info = el("div", "grow");
    info.appendChild(el("div", null, ver.note || "(no note)"));
    info.appendChild(el("div", "muted", `${ver.created_by} · ${fmtTime(ver.created_at)}`));
    if (ver.content) {
      const d = el("details");
      d.appendChild(el("summary", null, "content"));
      d.appendChild(el("pre", null, JSON.stringify(ver.content, null, 2)));
      info.appendChild(d);
    }
    row.appendChild(info);
    if (ver.version === currentVersion) row.appendChild(el("span", "tag sent", "in use"));
    else if (onPick) {
      const b = el("button", "btn small", actionLabel);
      b.addEventListener("click", () => onPick(ver.version));
      row.appendChild(b);
    }
    list.appendChild(row);
  }
  return list;
}

function askReason(what) {
  const reason = prompt(`Reason for ${what} (goes into the audit log):`, "");
  return reason === null ? null : reason.trim();
}

function tenantVersionsTab(box) {
  const v = platform.view;
  box.appendChild(el("div", "sub", "Industry template"));
  box.appendChild(el("p", "pf-note", v.prompt.pinned
    ? `Pinned to ${v.industry.name} template v${v.prompt.pinned}: industry edits do not reach this client until unpinned.`
    : `Follows the ${v.industry.name} template's live version (now v${v.industry.template_version}).`));
  const pinRow = el("div", "pf-actions");
  const pin = el("select");
  const follow = el("option", null, `Follow the live version (v${v.industry.template_version})`);
  follow.value = "";
  pin.appendChild(follow);
  for (const iv of v.prompt.industry_versions) {
    const o = el("option", null, `Pin to v${iv.version}${iv.note ? " — " + iv.note : ""}`);
    o.value = String(iv.version);
    o.selected = iv.version === v.prompt.pinned;
    pin.appendChild(o);
  }
  const apply = el("button", "btn", "Apply");
  apply.addEventListener("click", async () => {
    const reason = askReason("changing the pin");
    if (reason === null) return;
    try {
      platform.view = await api("POST", `/api/tenants/${platform.node.id}/pin`,
        { version: pin.value ? Number(pin.value) : null, reason });
      await loadDetail();
    } catch (err) { toast(err.message); }
  });
  pinRow.append(pin, apply);
  box.appendChild(pinRow);

  box.appendChild(el("div", "sub", "This client's prompt versions"));
  box.appendChild(versionList(v.prompt.client_versions, v.prompt.client_version, "Roll back to this", async (version) => {
    const reason = askReason(`rolling back to v${version}`);
    if (reason === null) return;
    try {
      await api("POST", `/api/tenants/${platform.node.id}/prompt/rollback`, { version, reason });
      await loadDetail();
      toast(`Now using client version ${version}.`, "info");
    } catch (err) { toast(err.message); }
  }));
}

function tenantPreviewTab(box) {
  const v = platform.view;
  box.appendChild(el("p", "pf-note",
    `Exactly what the model is given before the conversation, as ${v.prompt.version_tag} ` +
    "(platform rules / industry template / client version). Per-reply notes (appointments, media, style) are added at runtime."));
  box.appendChild(el("pre", "pf-rendered", v.prompt.rendered));
}

function tenantAssistTab(box) {
  box.appendChild(el("p", "pf-note",
    "Describe a change in plain words. The AI proposes it as config, the schema checks it, and nothing " +
    "changes until you press Apply. Uses DEEPSEEK_PLATFORM_KEY."));
  const intent = el("textarea");
  intent.rows = 3;
  intent.placeholder = "e.g. Don't answer between 10 pm and 8 am, and never talk about prices of colouring";
  box.appendChild(intent);
  const actions = el("div", "pf-actions");
  const ask = el("button", "btn primary", "Propose");
  actions.appendChild(ask);
  box.appendChild(actions);
  const out = el("div");
  box.appendChild(out);

  const show = () => {
    out.textContent = "";
    const p = platform.proposal;
    if (!p) return;
    out.appendChild(el("div", "sub", p.valid ? "Proposed change" : "The proposal does not validate"));
    for (const e of p.errors) out.appendChild(el("div", "pf-errors", `${e.path}: ${e.message}`));
    if (p.valid && !p.changes.length) out.appendChild(el("p", "pf-note", "It would change nothing."));
    if (p.changes.length) {
      const t = el("table", "cfg-table");
      for (const c of p.changes) {
        const tr = el("tr");
        tr.append(el("td", "path", c.path), el("td", null, JSON.stringify(c.from)), el("td", null, "→"),
          el("td", null, JSON.stringify(c.to)));
        t.appendChild(tr);
      }
      out.appendChild(t);
    }
    const raw = el("details");
    raw.appendChild(el("summary", "muted", "raw proposal"));
    raw.appendChild(el("pre", "pf-rendered", JSON.stringify(p.proposal, null, 2)));
    out.appendChild(raw);
    if (p.valid && p.changes.length) {
      const apply = el("button", "btn primary", "Apply this change");
      apply.addEventListener("click", async () => {
        apply.disabled = true;
        try {
          platform.view = await api("PUT", `/api/tenants/${platform.node.id}/config`, {
            overrides: p.overrides, reason: "AI proposal: " + p.intent, expected_revision: p.revision,
          });
          platform.proposal = null;
          platform.tab = "config";
          toast("Applied.", "info");
          renderDetail();
        } catch (err) { toast(err.message); apply.disabled = false; }
      });
      const bar = el("div", "pf-actions");
      bar.appendChild(apply);
      out.appendChild(bar);
    }
  };

  ask.addEventListener("click", async () => {
    if (!intent.value.trim()) return;
    ask.disabled = true;
    ask.textContent = "Asking…";
    try {
      platform.proposal = await api("POST", `/api/tenants/${platform.node.id}/config/propose`, { intent: intent.value });
      show();
    } catch (err) { toast(err.message); } finally { ask.disabled = false; ask.textContent = "Propose"; }
  });
  show();
}

function auditList(events) {
  const list = el("div", "pf-list");
  if (!events.length) list.appendChild(el("p", "pf-note", "Nothing recorded yet."));
  for (const e of events) {
    const row = el("div", "v");
    row.appendChild(el("span", "muted", fmtTime(e.created_at)));
    const info = el("div", "grow");
    info.appendChild(el("div", null, `${e.event} · ${e.actor}${e.reason ? " — " + e.reason : ""}`));
    if (e.payload && Object.keys(e.payload).length) {
      const d = el("details");
      d.appendChild(el("summary", null, "details"));
      d.appendChild(el("pre", null, JSON.stringify(e.payload, null, 2)));
      info.appendChild(d);
    }
    row.appendChild(info);
    list.appendChild(row);
  }
  return list;
}

function tenantAuditTab(box) {
  box.appendChild(auditList(platform.audit || []));
}

/* ----------------------------------------------------------- industry */

function industryTemplateTab(box) {
  const v = platform.view;
  box.appendChild(el("p", "pf-note",
    `Saving makes a new version and puts it live for every ${v.industry.name} client that is not pinned.`));
  const editors = [];
  for (const section of v.sections) {
    const card = el("div", "pf-section");
    card.appendChild(el("div", "title", section.heading));
    const text = el("textarea");
    text.rows = 3;
    text.value = section.text;
    card.appendChild(text);
    box.appendChild(card);
    editors.push({ key: section.key, text });
  }
  const actions = el("div", "pf-actions");
  const note = el("input");
  note.placeholder = "What changed (saved with the new version)";
  const button = el("button", "btn primary", "Save as new version");
  actions.append(note, button);
  box.appendChild(actions);
  button.addEventListener("click", async () => {
    const sections = {};
    for (const e of editors) if (e.text.value.trim()) sections[e.key] = e.text.value;
    button.disabled = true;
    try {
      const saved = await api("PUT", `/api/industries/${platform.node.id}/template`, { sections, note: note.value.trim() });
      toast(`Template v${saved.template_version} is live.`, "info");
      await loadDetail();
    } catch (err) { toast(err.message); } finally { button.disabled = false; }
  });
}

function industryConfigTab(box) {
  const v = platform.view;
  configEditor(box, {
    fields: v.config.fields, overrides: v.config.overrides, layer: "industry",
    inheritedLabel: "the platform defaults", revision: v.config.revision,
    save: async (body) => {
      await api("PUT", `/api/industries/${platform.node.id}/config`, body);
      await loadDetail();
    },
  });
}

function industryVersionsTab(box) {
  const v = platform.view;
  box.appendChild(versionList(v.versions, v.industry.template_version, "Make live", async (version) => {
    const reason = askReason(`making v${version} live`);
    if (reason === null) return;
    try {
      await api("POST", `/api/industries/${platform.node.id}/template/rollback`, { version, reason });
      await loadDetail();
    } catch (err) { toast(err.message); }
  }));
}

function industryClientsTab(box) {
  const list = el("div", "pf-list");
  if (!platform.view.tenants.length) list.appendChild(el("p", "pf-note", "No clients in this industry."));
  for (const t of platform.view.tenants) {
    const row = el("div", "v");
    row.appendChild(el("div", "grow", t.name));
    row.appendChild(el("span", "muted", t.prompt_pin_version ? `pinned to v${t.prompt_pin_version}` : "follows live"));
    const open = el("button", "btn small", "Open");
    open.addEventListener("click", () => selectNode({ kind: "tenant", id: t.id }));
    row.appendChild(open);
    list.appendChild(row);
  }
  box.appendChild(list);
}

/* --------------------------------------------------------------- base */

function baseRulesTab(box) {
  box.appendChild(el("p", "pf-note",
    "Rendered first in every client's prompt and restated as taking precedence at the end. Saving " +
    "makes a new version for all clients at once. The policy checks on outgoing replies run in code " +
    "whatever this says."));
  const rules = el("textarea");
  rules.rows = 16;
  rules.value = platform.view.current.content.rules;
  box.appendChild(rules);
  const actions = el("div", "pf-actions");
  const note = el("input");
  note.placeholder = "What changed (saved with the new version)";
  const button = el("button", "btn primary", "Save as new version");
  actions.append(note, button);
  box.appendChild(actions);
  button.addEventListener("click", async () => {
    if (!confirm("This changes the prompt of every client. Save?")) return;
    try {
      await api("PUT", "/api/platform/base", { rules: rules.value, note: note.value.trim() });
      await loadTree();
      await loadDetail();
      toast("Platform rules saved.", "info");
    } catch (err) { toast(err.message); }
  });
}

function baseVersionsTab(box) {
  const v = platform.view;
  box.appendChild(versionList(v.versions, v.current.version, "Use this version", async (version) => {
    const reason = askReason(`switching every client to platform rules v${version}`);
    if (reason === null) return;
    try {
      await api("POST", "/api/platform/base/rollback", { version, reason });
      await loadTree();
      await loadDetail();
    } catch (err) { toast(err.message); }
  }));
}

/* ------------------------------------------------------------- wiring */

$("open-platform").addEventListener("click", () => openPlatform());
$("open-settings").addEventListener("click", () => {
  const tenantId = state.status && state.status.tenant_id;
  openPlatform(tenantId ? { kind: "tenant", id: tenantId } : null);
});
$("pf-close").addEventListener("click", closePlatform);
$("platform").addEventListener("click", (ev) => { if (ev.target === $("platform")) closePlatform(); });
