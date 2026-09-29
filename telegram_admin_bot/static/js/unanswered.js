"use strict";

/* ----------------------------------------------------------- unanswered */
// The unanswered queue across all clients (unanswered_api.py): customer
// messages the bot did not answer, or answered with a fallback phrase.
// The admin marks them reviewed, reopens them, or writes an answer that is
// added to the client's industry FAQ. The top-bar badge counts open items.

const ua = { tab: "open", tenantId: "", data: null, promoting: null };

const UA_TABS = [["open", "Open"], ["reviewed", "Reviewed"], ["added_to_template", "Added to FAQ"], ["all", "All"]];
const UA_REASONS = {
  skipped: "not answered (limit / no-reply rule)", ai_error: "AI error", soft_off: "client switched off",
  paused: "chat paused / taken over", escalated: "escalated", policy_hold: "held by policy",
  fallback: "fallback reply", staging: "staging (not a test chat)",
};
const UA_STATUS = { open: "open", reviewed: "reviewed", added_to_template: "added to FAQ" };

function uaTime(iso) {
  return iso ? new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "—";
}

async function openUnanswered(tab) {
  if (tab) ua.tab = tab;
  $("unanswered").classList.add("open");
  await uaRender();
}

function closeUnanswered() { $("unanswered").classList.remove("open"); }

function uaApplyCount(open) {
  $("unanswered-count").textContent = open ? String(open) : "";
}

async function uaRender() {
  const tabs = $("ua-tabs");
  tabs.textContent = "";
  for (const [key, label] of UA_TABS) {
    const b = el("button", "pf-tab" + (ua.tab === key ? " on" : ""), label);
    b.addEventListener("click", () => { ua.tab = key; ua.promoting = null; uaRender(); });
    tabs.appendChild(b);
  }
  const box = $("ua-body");
  box.textContent = "";
  const params = new URLSearchParams({ status: ua.tab });
  if (ua.tenantId) params.set("tenant_id", ua.tenantId);
  try {
    ua.data = await api("GET", "/api/unanswered?" + params.toString());
  } catch (err) { box.appendChild(el("div", "pf-errors", err.message)); return; }
  uaApplyCount(ua.data.open);

  const bar = el("div", "bk-nav");
  const pick = el("select");
  pick.appendChild(new Option("All clients", ""));
  for (const t of ua.data.tenants) pick.appendChild(new Option(t.name, String(t.id)));
  pick.value = ua.tenantId;
  pick.addEventListener("change", () => { ua.tenantId = pick.value; uaRender(); });
  bar.append(el("label", "muted", "Client"), pick);
  box.appendChild(bar);

  if (!ua.data.items.length) {
    box.appendChild(el("div", "empty", ua.tab === "open" ? "Nothing waiting: every message got an answer." :
      "Nothing here."));
    return;
  }
  for (const item of ua.data.items) box.appendChild(uaRow(item));
}

function uaRow(item) {
  const row = el("div", "bk-row ua-item ua-" + item.status);
  const head = el("div", "bk-head");
  head.append(
    el("span", "bk-state ua-reason ua-r-" + item.reason, UA_REASONS[item.reason] || item.reason),
    el("span", "bk-time", uaTime(item.created_at)),
    el("span", null, item.tenant_name),
    el("span", "muted", item.customer || `Chat ${item.chat_id}`),
  );
  if (item.status !== "open") head.appendChild(el("span", "muted", UA_STATUS[item.status] || item.status));
  row.appendChild(head);

  const detail = el("div", "bk-detail");
  detail.appendChild(el("div", "ua-text", item.text || "(the message is no longer stored)"));
  if (item.detail) detail.appendChild(el("div", "muted ua-why", item.detail));
  if (item.reviewed_by) {
    detail.appendChild(el("div", "muted ua-why", `${UA_STATUS[item.status]} by ${item.reviewed_by}, ${uaTime(item.reviewed_at)}`));
  }

  const actions = el("div", "bk-actions");
  if (item.status === "open") {
    const done = el("button", "btn small", "Reviewed");
    done.addEventListener("click", () => uaPost(`/api/unanswered/${item.id}/reviewed`));
    actions.appendChild(done);
  } else {
    const again = el("button", "btn small", "Reopen");
    again.addEventListener("click", () => uaPost(`/api/unanswered/${item.id}/reopen`));
    actions.appendChild(again);
  }
  if (item.status !== "added_to_template") {
    const promote = el("button", "btn small", "Promote to FAQ…");
    promote.addEventListener("click", () => { ua.promoting = ua.promoting === item.id ? null : item.id; uaRender(); });
    actions.appendChild(promote);
  }
  detail.appendChild(actions);
  if (ua.promoting === item.id) detail.appendChild(uaPromoteForm(item));
  row.appendChild(detail);
  return row;
}

function uaPromoteForm(item) {
  const form = el("div", "pf-section ua-promote");
  form.appendChild(el("p", "pf-note", "Adds a Q&A entry to the FAQ of this client's industry template, as a new " +
    "template version: every client in that industry gets it. Write the answer yourself; nothing is generated."));
  const q = el("input");
  q.type = "text";
  q.value = (item.text || "").replace(/\s+/g, " ").trim();
  const a = el("textarea");
  a.rows = 4;
  a.placeholder = "The answer the bot should give";
  const f1 = el("div", "field");
  f1.append(el("label", null, "Question"), q);
  const f2 = el("div", "field");
  f2.append(el("label", null, "Answer"), a);
  const save = el("button", "btn primary", "Add to the FAQ");
  save.addEventListener("click", async () => {
    if (!a.value.trim()) { toast("Write the answer first."); return; }
    save.disabled = true;
    try {
      const r = await api("POST", `/api/unanswered/${item.id}/promote`, { question: q.value, answer: a.value });
      toast(`Added to the FAQ of ${r.industry.name} (template v${r.industry.template_version}).` +
        (r.pinned_clients ? ` ${r.pinned_clients} pinned client(s) won't see it until unpinned.` : ""), "info");
      ua.promoting = null;
    } catch (err) { toast(err.message); save.disabled = false; return; }
    await uaRender();
  });
  form.append(f1, f2, save);
  return form;
}

async function uaPost(path) {
  try { await api("POST", path); } catch (err) { toast(err.message); }
  await uaRender();
}

async function uaPoll() {
  if ($("admin-gate").classList.contains("open")) return;
  try { uaApplyCount((await api("GET", "/api/unanswered/count")).open); } catch (_) {}
}

// A running account says when it queued something ({"type": "unanswered"}
// on the session's socket): refresh the badge, and the list if it is open.
// socket.js dispatches through the global handleEvent, so wrap it.
const uaHandleEvent = handleEvent;
handleEvent = function (data) {
  if (data && data.type === "unanswered") {
    if ($("unanswered").classList.contains("open")) uaRender(); else uaPoll();
  }
  return uaHandleEvent(data);
};

$("open-unanswered").addEventListener("click", () => openUnanswered());
$("ua-close").addEventListener("click", closeUnanswered);
$("unanswered").addEventListener("click", (ev) => { if (ev.target === $("unanswered")) closeUnanswered(); });
setInterval(uaPoll, 60000);
uaPoll();
