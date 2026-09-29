"use strict";

/* --------------------------------------------------------------- review */
// Review batches for the trainer (review_api.py). A batch is every reply the
// bot wrote and sent to one client's customers in a date range, each with
// the conversation before it. The trainer approves, rejects or corrects
// each one; approved and corrected ones export as JSONL for training.
// Big buttons and no keyboard shortcuts: this is meant to work on a tablet.

const rv = {
  tenants: [],
  tenantId: null,
  batches: [],
  batch: null,       // the open batch (counts included), or null = the list
  index: 0,          // position of the item on screen
  items: new Map(),  // offset -> item, filled a page at a time
  editing: false,
  showAll: false,
};
const RV_PAGE = 50;
const RV_CONTEXT_SHORT = 6;
const RV_DECISION = { approve: "Approved", reject: "Rejected", edit: "Edited" };

function rvIsoDate(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function rvTenantName(id) {
  const t = rv.tenants.find((x) => x.id === id);
  return t ? t.name : `Client ${id}`;
}

async function openReview() {
  $("review").classList.add("open");
  try {
    rv.tenants = (await api("GET", "/api/platform/tree")).tenants;
  } catch (err) { toast(err.message); return; }
  if (rv.tenantId === null && rv.tenants.length) {
    // Start on the client of the account that is open, if there is one.
    const current = rv.tenants.find((t) => t.session_id === state.sessionId);
    rv.tenantId = (current || rv.tenants[0]).id;
  }
  if (rv.batch) await rvRenderItem();
  else await rvRenderList();
}

function closeReview() { $("review").classList.remove("open"); }

function rvProgress(counts) {
  const bar = el("div", "rv-bar");
  const total = counts.total || 1;
  for (const [key, cls] of [["approved", "ok"], ["edited", "edit"], ["rejected", "bad"]]) {
    const part = el("span", "rv-bar-" + cls);
    part.style.width = `${(100 * counts[key]) / total}%`;
    bar.appendChild(part);
  }
  return bar;
}

function rvCountsText(c) {
  return `${c.total - c.undecided} of ${c.total} decided · ${c.approved} approved · ${c.edited} edited · ` +
    `${c.rejected} rejected`;
}

/* ---------------------------------------------------------- batch list */

async function rvRenderList() {
  rv.batch = null;
  $("rv-crumb").textContent = "the bot's replies, for training";
  const box = $("rv-body");
  box.textContent = "";

  // New batch.
  const form = el("div", "pf-section rv-new");
  form.appendChild(el("div", "title", "New review batch"));
  const client = el("select");
  for (const t of rv.tenants) {
    const o = el("option", null, t.name);
    o.value = t.id;
    if (t.id === rv.tenantId) o.selected = true;
    client.appendChild(o);
  }
  const to = new Date();
  const from = new Date(to.getTime() - 6 * 86400000);
  const dateFrom = el("input");
  dateFrom.type = "date";
  dateFrom.value = rvIsoDate(from);
  const dateTo = el("input");
  dateTo.type = "date";
  dateTo.value = rvIsoDate(to);
  const name = el("input");
  name.type = "text";
  name.maxLength = 200;
  name.placeholder = "e.g. September, week 1";
  const row = el("div", "row");
  for (const [label, input] of [["Client", client], ["From", dateFrom], ["To (including)", dateTo], ["Name", name]]) {
    const f = el("div", "field");
    f.append(el("label", null, label), input);
    row.appendChild(f);
  }
  form.appendChild(row);
  form.appendChild(el("p", "pf-note", "Takes every reply the bot wrote and sent in those days (the client's own " +
    "timezone), with up to 20 messages of the conversation before each. At most 2000 replies per batch."));
  const errors = el("div", "pf-errors");
  const create = el("button", "btn primary", "Create batch");
  create.addEventListener("click", async () => {
    errors.textContent = "";
    const tenantId = Number(client.value);
    const label = name.value.trim() || `${rvTenantName(tenantId)} ${dateFrom.value} – ${dateTo.value}`;
    create.disabled = true;
    try {
      const batch = await api("POST", "/api/review/batches",
        { tenant_id: tenantId, name: label, date_from: dateFrom.value, date_to: dateTo.value });
      rv.tenantId = tenantId;
      await rvOpenBatch(batch.id);
    } catch (err) { errors.textContent = err.message; }
    finally { create.disabled = false; }
  });
  client.addEventListener("change", () => { rv.tenantId = Number(client.value); rvRenderList(); });
  const actions = el("div", "pf-actions");
  actions.appendChild(create);
  form.append(errors, actions);
  box.appendChild(form);

  // Existing batches of this client.
  box.appendChild(el("h3", "sf-sub", `Batches of ${rvTenantName(rv.tenantId)}`));
  try {
    rv.batches = rv.tenantId === null ? [] : await api("GET", `/api/review/batches?tenant_id=${rv.tenantId}`);
  } catch (err) { box.appendChild(el("div", "pf-errors", err.message)); return; }
  if (!rv.batches.length) {
    box.appendChild(el("p", "pf-note", "No batches yet for this client."));
    return;
  }
  for (const b of rv.batches) {
    const card = el("div", "bk-row rv-batch" + (b.status === "done" ? " done" : ""));
    const head = el("div", "bk-head");
    head.appendChild(el("b", null, b.name));
    head.appendChild(el("span", "bk-num", `${b.date_from} – ${b.date_to}`));
    head.appendChild(el("span", "bk-state", b.status === "done" ? "done" : "open"));
    card.appendChild(head);
    const detail = el("div", "rv-batch-detail");
    detail.appendChild(rvProgress(b.counts));
    detail.appendChild(el("div", "muted", rvCountsText(b.counts)));
    const acts = el("div", "bk-actions");
    const open = el("button", "btn primary", b.counts.undecided ? "Continue" : "Open");
    open.addEventListener("click", () => rvOpenBatch(b.id));
    const exp = el("a", "btn", "Export JSONL");
    exp.href = `/api/review/batches/${b.id}/export.jsonl`;
    const del = el("button", "btn warn", "Delete");
    del.addEventListener("click", async () => {
      if (!confirm(`Delete the batch "${b.name}" and its decisions? The messages themselves stay.`)) return;
      try { await api("DELETE", `/api/review/batches/${b.id}`); await rvRenderList(); }
      catch (err) { toast(err.message); }
    });
    acts.append(open, exp, del);
    detail.appendChild(acts);
    card.appendChild(detail);
    box.appendChild(card);
  }
}

/* -------------------------------------------------------- one by one */

async function rvLoadPage(offset) {
  const start = Math.floor(offset / RV_PAGE) * RV_PAGE;
  const page = await api("GET", `/api/review/batches/${rv.batch.id}?offset=${start}&limit=${RV_PAGE}`);
  rv.batch = Object.assign(rv.batch, { counts: page.counts, status: page.status,
                                       first_undecided: page.first_undecided });
  page.items.forEach((item, i) => rv.items.set(start + i, item));
}

async function rvOpenBatch(batchId) {
  rv.items = new Map();
  rv.editing = false;
  rv.showAll = false;
  try {
    rv.batch = await api("GET", `/api/review/batches/${batchId}?offset=0&limit=1`);
  } catch (err) { toast(err.message); return; }
  rv.index = rv.batch.first_undecided === null ? 0 : rv.batch.first_undecided;
  await rvRenderItem();
}

async function rvGo(index) {
  rv.index = Math.max(0, Math.min(index, rv.batch.counts.total - 1));
  rv.editing = false;
  rv.showAll = false;
  await rvRenderItem();
}

async function rvRenderItem() {
  const b = rv.batch;
  const box = $("rv-body");
  if (!rv.items.has(rv.index)) {
    try { await rvLoadPage(rv.index); } catch (err) { toast(err.message); return; }
  }
  const item = rv.items.get(rv.index);
  box.textContent = "";
  $("rv-crumb").textContent = `${rvTenantName(b.tenant_id)} · ${b.name}`;

  // Top: where we are, progress, batch actions.
  const top = el("div", "rv-top");
  const back = el("button", "btn", "← Batches");
  back.addEventListener("click", rvRenderList);
  top.appendChild(back);
  top.appendChild(el("b", null, `Reply ${rv.index + 1} of ${b.counts.total}`));
  top.appendChild(el("span", "muted", rvCountsText(b.counts)));
  top.appendChild(el("span", "spacer"));
  const exp = el("a", "btn", "Export JSONL");
  exp.href = `/api/review/batches/${b.id}/export.jsonl`;
  const done = el("button", "btn" + (b.status === "done" ? "" : " primary"), b.status === "done" ? "Done ✓" : "Mark done");
  done.disabled = b.status === "done";
  done.addEventListener("click", async () => {
    if (b.counts.undecided && !confirm(`${b.counts.undecided} replies have no decision yet; they are left out ` +
                                       "of the export. Mark the batch done anyway?")) return;
    try {
      const saved = await api("POST", `/api/review/batches/${b.id}/done`);
      rv.batch.status = saved.status;
      rv.batch.counts = saved.counts;
      await rvRenderItem();
    } catch (err) { toast(err.message); }
  });
  top.append(exp, done);
  box.appendChild(top);
  box.appendChild(rvProgress(b.counts));

  if (!item) { box.appendChild(el("p", "pf-note", "This batch has no replies.")); return; }

  // The conversation before the reply, compact: the last few, all on request.
  const ctx = el("div", "rv-context");
  const who = item.chat_name || `chat ${item.chat_id}`;
  ctx.appendChild(el("div", "rv-label", `Conversation with ${who}` + (item.sent_at ? ` · ${fmtTime(item.sent_at)}` : "")));
  const messages = item.context || [];
  const shown = rv.showAll ? messages : messages.slice(-RV_CONTEXT_SHORT);
  if (messages.length > shown.length) {
    const more = el("button", "btn small", `Show all ${messages.length} earlier messages`);
    more.addEventListener("click", () => { rv.showAll = true; rvRenderItem(); });
    ctx.appendChild(more);
  }
  if (!messages.length) ctx.appendChild(el("div", "muted", "(no earlier messages)"));
  for (const m of shown) {
    ctx.appendChild(el("div", "rv-msg " + (m.role === "user" ? "user" : "assistant"), m.content));
  }
  box.appendChild(ctx);

  // The reply under review.
  const reply = el("div", "rv-reply" + (item.decision ? " " + item.decision : ""));
  reply.appendChild(el("div", "rv-label", "The bot's reply" +
    (item.decision ? ` — ${RV_DECISION[item.decision]}` : "")));
  reply.appendChild(el("div", "rv-text" + (item.decision === "edit" ? " struck" : ""), item.reply));
  if (item.decision === "edit" && item.edited_text) {
    reply.appendChild(el("div", "rv-label", "Corrected"));
    reply.appendChild(el("div", "rv-text", item.edited_text));
  }
  box.appendChild(reply);

  const errors = el("div", "pf-errors");
  const decide = async (decision, editedText) => {
    errors.textContent = "";
    try {
      const saved = await api("POST", `/api/review/items/${item.id}`, { decision, edited_text: editedText });
      const was = item.decision;
      rv.items.set(rv.index, saved);
      const c = rv.batch.counts;
      const key = { approve: "approved", reject: "rejected", edit: "edited" };
      if (was) c[key[was]] -= 1; else c.undecided -= 1;
      c[key[decision]] += 1;
      rv.editing = false;
      if (rv.index < c.total - 1) await rvGo(rv.index + 1);
      else await rvRenderItem();
    } catch (err) { errors.textContent = err.message; }
  };

  if (rv.editing) {
    const area = el("textarea", "rv-edit");
    area.rows = 5;
    area.value = item.edited_text || item.reply;
    const save = el("button", "btn primary rv-big", "Save correction");
    save.addEventListener("click", () => {
      if (!area.value.trim()) { errors.textContent = "The corrected reply cannot be empty."; return; }
      return decide("edit", area.value);
    });
    const cancel = el("button", "btn rv-big", "Cancel");
    cancel.addEventListener("click", () => { rv.editing = false; rvRenderItem(); });
    const row = el("div", "rv-buttons");
    row.append(save, cancel);
    box.append(area, errors, row);
    area.focus();
  } else {
    const row = el("div", "rv-buttons");
    const approve = el("button", "btn rv-big rv-approve" + (item.decision === "approve" ? " on" : ""), "Approve");
    approve.addEventListener("click", () => decide("approve"));
    const reject = el("button", "btn rv-big rv-reject" + (item.decision === "reject" ? " on" : ""), "Reject");
    reject.addEventListener("click", () => decide("reject"));
    const edit = el("button", "btn rv-big rv-editbtn" + (item.decision === "edit" ? " on" : ""), "Edit");
    edit.addEventListener("click", () => { rv.editing = true; rvRenderItem(); });
    row.append(approve, reject, edit);
    box.append(row, errors);
  }

  const nav = el("div", "rv-buttons rv-nav");
  const prev = el("button", "btn rv-big", "← Previous");
  prev.disabled = rv.index === 0;
  prev.addEventListener("click", () => rvGo(rv.index - 1));
  const next = el("button", "btn rv-big", "Next →");
  next.disabled = rv.index >= b.counts.total - 1;
  next.addEventListener("click", () => rvGo(rv.index + 1));
  nav.append(prev, next);
  if (b.first_undecided !== null && b.counts.undecided) {
    const jump = el("button", "btn rv-big", "First undecided");
    jump.addEventListener("click", async () => {
      try { await rvLoadPage(0); } catch (_) {}
      if (rv.batch.first_undecided !== null) await rvGo(rv.batch.first_undecided);
    });
    nav.appendChild(jump);
  }
  box.appendChild(nav);
}

$("open-review").addEventListener("click", () => openReview());
$("rv-close").addEventListener("click", closeReview);
$("review").addEventListener("click", (ev) => { if (ev.target === $("review")) closeReview(); });
