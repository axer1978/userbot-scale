"use strict";

/* --------------------------------------------------------- verification */
// Identity videos and client photos that wait for the platform admin
// (review_admin_api.py, review.py). Not the "Review" overlay (review.js),
// which is about the bot's replies for training.
//
// - Videos: a client films themselves holding a random code on paper and
//   doing a random gesture; approve or reject (the client sees the reason).
// - Photos: every photo a client of a reviewed business adds, and every file
//   pulled back for a re-check, waits here before the bot may use it.
// - Businesses: which ones are under review, which are paused until a video
//   is approved, and the industries whose businesses all need review.

const vf = { tab: "videos", allVideos: false, allPhotos: false, summary: null };

const VF_TABS = [["videos", "Videos"], ["photos", "Photos"], ["businesses", "Businesses"]];
const VF_STATUS = {
  requested: ["asked, no video yet", "paused"],
  submitted: ["video waiting", "paused"],
  approved: ["verified", "link"],
  rejected: ["rejected", "escalated"],
};
const VF_PHOTO_STATUS = {
  pending: ["waiting", "paused"],
  approved: ["approved", "link"],
  rejected: ["rejected", "escalated"],
  withdrawn: ["withdrawn by the client", ""],
};

function vfTime(iso) {
  return iso ? new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "—";
}

function vfBytes(n) {
  if (!n && n !== 0) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function vfBadge(map, status) {
  const [label, kind] = map[status] || [status || "not asked", ""];
  return el("span", "badge" + (kind ? " " + kind : ""), label);
}

async function openVerify(tab) {
  if (tab) vf.tab = tab;
  $("verify").classList.add("open");
  await vfRender();
}

function closeVerify() { $("verify").classList.remove("open"); }

function vfApplyCount(summary) {
  const p = (summary && summary.pending) || {};
  const n = (p.verifications || 0) + (p.photos || 0);
  $("verify-count").textContent = n ? String(n) : "";
}

async function vfPoll() {
  if ($("admin-gate").classList.contains("open") || !can("view.verification")) return;
  try {
    vf.summary = await api("GET", "/api/review/summary");
    vfApplyCount(vf.summary);
    if ($("verify").classList.contains("open")) vfTabs();
  } catch (_) {}
}

function vfTabs() {
  const tabs = $("vf-tabs");
  tabs.textContent = "";
  const p = (vf.summary && vf.summary.pending) || {};
  for (const [key, label] of VF_TABS) {
    const b = el("button", "pf-tab" + (vf.tab === key ? " on" : ""), label);
    const n = key === "videos" ? p.verifications : key === "photos" ? p.photos : 0;
    b.appendChild(el("span", "count-badge", n ? String(n) : ""));
    b.addEventListener("click", () => { vf.tab = key; vfRender(); });
    tabs.appendChild(b);
  }
}

async function vfRender() {
  vfTabs();
  const box = $("vf-body");
  box.textContent = "";
  box.appendChild(el("div", "pf-note", "Loading…"));
  const tab = vf.tab;
  const draw = tab === "photos" ? vfPhotos : tab === "businesses" ? vfBusinesses : vfVideos;
  try {
    const [summary, data] = await Promise.all([api("GET", "/api/review/summary"), vfLoad(tab)]);
    vf.summary = summary;
    vfApplyCount(summary);
    if (vf.tab !== tab) return; // switched tab while loading
    vfTabs();
    box.textContent = "";
    draw(box, data);
  } catch (err) {
    if (vf.tab !== tab) return;
    box.textContent = "";
    box.appendChild(el("div", "pf-errors", err.message));
  }
}

function vfLoad(tab) {
  if (tab === "videos") {
    return api("GET", "/api/review/verifications" + (vf.allVideos ? "" : "?status=submitted"));
  }
  if (tab === "photos") return api("GET", "/api/review/photos?status=" + (vf.allPhotos ? "all" : "pending"));
  return api("GET", "/api/review/industries");
}

// A decision: tell the result, then refresh the list and the badge.
async function vfCall(method, path, body, done, button) {
  if (button) button.disabled = true;
  try {
    const result = await api(method, path, body);
    const text = typeof done === "function" ? done(result) : done;
    if (text) toast(text, "info");
    await vfRender();
    return result;
  } catch (err) {
    toast(err.message);
    if (button) button.disabled = false;
    return null;
  }
}

// "Show all" switch above a list.
function vfShowAll(box, checked, label, onChange) {
  const bar = el("div", "field check vf-showall");
  const input = el("input");
  input.type = "checkbox";
  input.id = "vf-showall";
  input.checked = checked;
  input.addEventListener("change", () => onChange(input.checked));
  const l = el("label", null, label);
  l.htmlFor = "vf-showall";
  bar.append(input, l);
  box.appendChild(bar);
}

// A hidden inline form: a reason input and a button that needs it.
function vfReasonForm(placeholder, label, onSubmit) {
  const form = el("div", "pf-actions vf-reason");
  form.hidden = true;
  const input = el("input");
  input.type = "text";
  input.maxLength = 500;
  input.placeholder = placeholder;
  const go = el("button", "btn small warn", label);
  const cancel = el("button", "btn small", "Cancel");
  const submit = () => {
    const text = input.value.trim();
    if (!text) { toast("Write a reason first."); input.focus(); return; }
    onSubmit(text, go);
  };
  go.addEventListener("click", submit);
  input.addEventListener("keydown", (ev) => { if (ev.key === "Enter") submit(); });
  cancel.addEventListener("click", () => { form.hidden = true; });
  form.append(input, go, cancel);
  form.show = () => { form.hidden = !form.hidden; if (!form.hidden) input.focus(); };
  return form;
}

/* --------------------------------------------------------------- videos */

function vfVideos(box, items) {
  box.appendChild(el("p", "pf-note", "Each client got a random code to write on paper and a random gesture, " +
    "and had 30 minutes to film themselves showing both: a video made earlier can't know the code. " +
    "Only you see these videos; they are deleted 30 days after your decision. Approving lifts the pause on " +
    "their businesses; rejecting asks for a new video, and the client sees your reason."));
  vfShowAll(box, vf.allVideos, "Show all (also decided and still-open requests)", (on) => {
    vf.allVideos = on;
    vfRender();
  });
  if (!items.length) {
    box.appendChild(el("div", "pf-note", vf.allVideos ? "No verification yet." : "No video is waiting."));
    return;
  }
  for (const v of items) box.appendChild(vfVideoCard(v));
}

function vfVideoCard(v) {
  const card = el("div", "pf-section ow-card vf-card" + (v.status === "submitted" ? "" : " vf-done"));
  const title = el("div", "title");
  title.appendChild(el("span", null, v.username));
  const badges = el("span", "ow-badges");
  badges.appendChild(vfBadge(VF_STATUS, v.status));
  title.appendChild(badges);
  card.appendChild(title);

  const facts = el("div", "ow-facts");
  const fact = (label, value) => {
    const row = el("div");
    row.append(el("span", "muted", label), el("span", null, value || "—"));
    facts.appendChild(row);
  };
  fact("Name", v.display_name);
  fact("Company", v.company);
  fact("E-mail", v.email);
  fact("Phone", v.phone);
  fact("Businesses", (v.tenants || []).map((t) => t.name).join(", ") || "none linked");
  fact("Asked because", v.reason + (v.requested_by ? ` (${v.requested_by})` : ""));
  fact("Submitted", v.submitted_at ? vfTime(v.submitted_at) + (v.video_bytes ? ` · ${vfBytes(v.video_bytes)}` : "") : "");
  if (v.reviewed_at) fact("Decided", vfTime(v.reviewed_at));
  card.appendChild(facts);
  if (v.review_reason) {
    card.appendChild(el("div", "pf-note " + (v.status === "rejected" ? "ow-reason" : "ow-meta"),
      (v.status === "rejected" ? "Reason given: " : "Note: ") + v.review_reason));
  }

  const row = el("div", "vf-video-row");
  const media = el("div", "vf-video");
  if (v.has_video) {
    const video = el("video");
    video.controls = true;
    video.preload = "metadata";
    video.playsInline = true;
    video.src = `/api/review/verifications/${v.id}/video`;
    video.addEventListener("error", () => {
      media.textContent = "";
      media.appendChild(el("div", "vf-missing", "The video could not be loaded."));
    });
    media.appendChild(video);
  } else if (v.video_deleted_at) {
    media.appendChild(el("div", "vf-missing", `Video deleted ${vfTime(v.video_deleted_at)} (kept 30 days).`));
  } else {
    media.appendChild(el("div", "vf-missing", "No video yet."));
  }
  row.appendChild(media);

  const must = el("div", "vf-challenge");
  must.appendChild(el("div", "vf-must-label", "The video must show"));
  if (v.challenge) {
    must.appendChild(el("div", "vf-code", v.challenge));
    must.appendChild(el("div", "vf-gesture", v.gesture || "—"));
    if (v.challenge_at) must.appendChild(el("div", "muted vf-small", `Code given ${vfTime(v.challenge_at)}`));
  } else {
    must.appendChild(el("div", "muted", "No code was given yet."));
  }
  must.appendChild(el("div", "vf-checklist",
    "Code readable on paper? Gesture done? Same person throughout? Looks 18+? Not a recording of a screen?"));
  row.appendChild(must);
  card.appendChild(row);

  if (v.status === "submitted") {
    const reasonRow = el("div", "pf-actions");
    const reason = el("input");
    reason.type = "text";
    reason.maxLength = 500;
    reason.placeholder = "Note: optional to approve, required to reject (the client sees it)";
    reasonRow.appendChild(reason);
    card.appendChild(reasonRow);

    const actions = el("div", "pf-actions");
    const approve = el("button", "btn small primary", "Approve");
    approve.addEventListener("click", () => {
      vfCall("POST", `/api/review/verifications/${v.id}/approve`, { reason: reason.value.trim() },
        `${v.username} verified. Their businesses run again unless something else holds them.`, approve);
    });
    const reject = el("button", "btn small warn", "Reject");
    reject.addEventListener("click", () => {
      const text = reason.value.trim();
      if (!text) {
        toast("Write a reason first: the client sees it.");
        reason.focus();
        return;
      }
      if (!confirm(`Reject the video of ${v.username}? They will see this reason:\n\n${text}`)) return;
      vfCall("POST", `/api/review/verifications/${v.id}/reject`, { reason: text },
        `Video of ${v.username} rejected. They have to send a new one; their businesses stay paused.`, reject);
    });
    actions.append(approve, reject);
    card.appendChild(actions);
  }
  return card;
}

/* --------------------------------------------------------------- photos */

function vfPhotos(box, items) {
  box.appendChild(el("p", "pf-note", "Photos and videos the bot may send, waiting for you before it can use " +
    "them. The description is what the bot sees to pick the right file: correct it before approving if needed. " +
    "Approving a replacement removes the photo it replaces. Rejecting needs a reason: the client sees it."));
  vfShowAll(box, vf.allPhotos, "Show all (also decided ones)", (on) => {
    vf.allPhotos = on;
    vfRender();
  });
  if (!items.length) {
    box.appendChild(el("div", "pf-note", vf.allPhotos ? "Nothing submitted yet." : "No photo is waiting."));
    return;
  }
  const grid = el("div", "vf-grid");
  for (const p of items) grid.appendChild(vfPhotoCard(p));
  box.appendChild(grid);
}

// <img>/<video> that turns into a note when the file is gone.
function vfMedia(kind, src, missing) {
  const wrap = el("div", "vf-media");
  const node = el(kind === "video" ? "video" : "img");
  if (kind === "video") {
    node.controls = true;
    node.preload = "metadata";
    node.playsInline = true;
  } else {
    node.alt = "";
    node.loading = "lazy";
  }
  node.addEventListener("error", () => {
    wrap.textContent = "";
    wrap.appendChild(el("div", "vf-missing", missing));
  });
  node.src = src;
  wrap.appendChild(node);
  return wrap;
}

function vfPhotoCard(p) {
  const pending = p.status === "pending";
  const card = el("div", "pf-section vf-photo" + (pending ? "" : " vf-done"));
  card.appendChild(vfMedia(p.kind, `/api/review/photos/${p.id}/file`, "File no longer kept."));

  const head = el("div", "title vf-photo-title");
  head.appendChild(el("span", null, p.tenant_name));
  const badges = el("span", "ow-badges");
  badges.appendChild(vfBadge(VF_PHOTO_STATUS, p.status));
  if (p.kind === "video") badges.appendChild(el("span", "badge", "video"));
  head.appendChild(badges);
  card.appendChild(head);

  const who = p.source === "recheck" ? "pulled back for re-check" : `from ${p.username || "a deleted login"}`;
  card.appendChild(el("div", "pf-note ow-meta vf-small",
    [who, vfTime(p.created_at), p.original_name, vfBytes(p.bytes)].filter(Boolean).join(" · ")));

  if (p.replaces_item !== null && p.replaces_item !== undefined) {
    const rep = el("div", "vf-replaces");
    rep.appendChild(el("div", "muted vf-small", "Replaces"));
    if (p.session_id) {
      rep.appendChild(vfMedia("photo",
        `/api/sessions/${encodeURIComponent(p.session_id)}/media/${p.replaces_item}/file`,
        "That photo is already gone."));
    } else {
      rep.appendChild(el("div", "vf-missing", `Photo #${p.replaces_item} (no account to show it from).`));
    }
    card.appendChild(rep);
  }

  const descWrap = el("div", "field vf-desc");
  const desc = el("input");
  desc.type = "text";
  desc.maxLength = 300;
  desc.value = p.description || "";
  desc.placeholder = "What the bot sees, e.g. the room with the red sofa";
  desc.disabled = !pending;
  descWrap.append(el("label", null, "Description (what the bot sees)"), desc);
  card.appendChild(descWrap);

  if (p.review_reason) {
    card.appendChild(el("div", "pf-note " + (p.status === "rejected" ? "ow-reason" : "ow-meta"),
      (p.status === "rejected" ? "Reason given: " : "Note: ") + p.review_reason));
  }
  if (p.reviewed_by && !pending) {
    card.appendChild(el("div", "pf-note ow-meta vf-small", `${p.status} by ${p.reviewed_by} ${vfTime(p.reviewed_at)}`));
  }

  if (pending) {
    const reason = el("input");
    reason.type = "text";
    reason.maxLength = 500;
    reason.placeholder = "Reason to reject (the client sees it)";
    const reasonRow = el("div", "pf-actions vf-photo-reason");
    reasonRow.appendChild(reason);
    card.appendChild(reasonRow);

    const actions = el("div", "pf-actions");
    const approve = el("button", "btn small primary", "Approve");
    approve.addEventListener("click", () => {
      vfCall("POST", `/api/review/photos/${p.id}/approve`, { reason: "", description: desc.value.trim() },
        `Approved for ${p.tenant_name}: the bot can use it from its next reply.`, approve);
    });
    const reject = el("button", "btn small warn", "Reject");
    reject.addEventListener("click", () => {
      const text = reason.value.trim();
      if (!text) {
        toast("Write a reason first: the client sees it.");
        reason.focus();
        return;
      }
      vfCall("POST", `/api/review/photos/${p.id}/reject`, { reason: text }, `Rejected for ${p.tenant_name}.`, reject);
    });
    actions.append(approve, reject);
    card.appendChild(actions);
  }
  return card;
}

/* ----------------------------------------------------------- businesses */

function vfBusinesses(box, industries) {
  const tenants = (vf.summary && vf.summary.tenants) || [];
  box.appendChild(el("p", "pf-note", "Businesses in an industry marked below, and any whose client you asked to " +
    "verify again. A paused bot receives messages but sends nothing until a client login linked to the " +
    "business has an approved video."));
  if (!tenants.length) {
    box.appendChild(el("div", "pf-note", "No business is under review: no industry is marked below and " +
      "nobody was asked to verify again."));
  }
  for (const t of tenants) box.appendChild(vfTenantCard(t));
  vfIndustries(box, industries);
}

function vfTenantCard(t) {
  const card = el("div", "pf-section ow-card vf-tenant");
  const title = el("div", "title");
  title.append(el("span", null, t.name), el("span", "muted", t.industry));
  const badges = el("span", "ow-badges");
  if (t.held) badges.appendChild(el("span", "badge escalated", "Bot paused until verified"));
  if (t.pending_photos) {
    badges.appendChild(el("span", "badge paused", `${t.pending_photos} photo${t.pending_photos === 1 ? "" : "s"} waiting`));
  }
  title.appendChild(badges);
  card.appendChild(title);

  card.appendChild(el("div", "sf-sub muted", "Client logins"));
  const owners = t.owners || [];
  if (!owners.length) {
    card.appendChild(el("div", "pf-note ow-meta", "No client login linked yet (link one under Client logins)."));
  }
  for (const o of owners) {
    const row = el("div", "vf-owner");
    const line = el("div", "vf-owner-line");
    line.append(el("span", null, o.username), vfBadge(VF_STATUS, o.status));
    const ask = el("button", "btn small", "Ask to verify again…");
    line.append(el("span", "spacer"), ask);
    row.appendChild(line);
    const form = vfReasonForm("Why (the client sees it)", "Ask to verify", (reason, button) => {
      if (!confirm(`Ask ${o.username} to send a new verification video? Every business of this login ` +
        "pauses until you approve it.")) return;
      vfCall("POST", `/api/owners/${o.id}/request-verification`, { reason },
        `${o.username} has to verify again; their businesses are paused.`, button);
    });
    ask.addEventListener("click", () => form.show());
    row.appendChild(form);
    card.appendChild(row);
  }

  const actions = el("div", "pf-actions");
  const recheck = el("button", "btn small warn", "Re-check all photos…");
  actions.appendChild(recheck);
  card.appendChild(actions);
  const form = vfReasonForm("Why (kept in the audit log)", "Re-check all photos", (reason, button) => {
    if (!confirm(`Every photo and video of ${t.name} goes back into review and the bot stops using them now.`)) return;
    vfCall("POST", `/api/tenants/${t.id}/recheck-media`, { reason },
      (r) => r.moved ? `${r.moved} file${r.moved === 1 ? "" : "s"} of ${t.name} moved back into review.` :
        `${t.name} had no photo or video to re-check.`, button);
  });
  recheck.addEventListener("click", () => form.show());
  card.appendChild(form);
  return card;
}

function vfIndustries(box, industries) {
  const sec = el("div", "pf-section vf-industries");
  sec.appendChild(el("div", "title", "Industries"));
  sec.appendChild(el("p", "pf-note", "Mark the escort market here. Clients of a marked industry must verify " +
    "their identity and age by video before their bot runs, and every photo they add waits for you here before " +
    "the bot may send it."));
  if (!industries.length) sec.appendChild(el("div", "pf-note", "No industry yet."));
  for (const i of industries) {
    const row = el("div", "field check vf-industry");
    const input = el("input");
    input.type = "checkbox";
    input.id = `vf-ind-${i.id}`;
    input.checked = !!i.requires_review;
    const label = el("label", null, i.name);
    label.htmlFor = input.id;
    row.append(input, label, el("span", "muted vf-small",
      `${i.tenants} business${i.tenants === 1 ? "" : "es"}` + (i.requires_review ? " · review required" : "")));
    input.addEventListener("change", async () => {
      const on = input.checked;
      if (on && !confirm(`Every business in ${i.name} is paused until its client sends an approved verification video.`)) {
        input.checked = false;
        return;
      }
      input.disabled = true;
      const done = await vfCall("PUT", `/api/review/industries/${i.id}`, { requires_review: on },
        on ? `${i.name}: review required.` : `${i.name}: review no longer required.`);
      if (!done) { input.checked = !on; input.disabled = false; }
    });
    sec.appendChild(row);
  }
  box.appendChild(sec);
}

/* ----------------------------------------------------------------- wire */

$("open-verify").addEventListener("click", () => openVerify());
$("vf-close").addEventListener("click", closeVerify);
$("verify").addEventListener("click", (ev) => { if (ev.target === $("verify")) closeVerify(); });

// The first poll below runs before sign-in finishes; poll again once the
// admin gate closes so the badge doesn't wait a minute.
const vfHideGate = hideGate;
hideGate = function () {
  const result = vfHideGate.apply(this, arguments);
  setTimeout(vfPoll, 0);
  return result;
};
setInterval(vfPoll, 60000);
vfPoll();
