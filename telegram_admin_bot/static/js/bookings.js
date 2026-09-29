"use strict";

/* ------------------------------------------------------------- bookings */
// The selected account's bookings (booking_api.py): a week at a time, what
// waits for the owner's answer, the waitlist, opening hours, and the
// calendar feed and AI usage. Every action goes to the running account,
// which tells the customer and the owner; nothing here confirms anything
// by itself.

const bk = { tab: "calendar", start: null, data: null, selected: null, events: null };

const BK_TABS = [["calendar", "Calendar"], ["waiting", "Waiting for an answer"], ["waitlist", "Waitlist"],
                 ["hours", "Opening hours"], ["links", "Calendar link & AI usage"]];
const BK_STATE = { requested: "not sent to the owner yet", pending: "waiting for the owner", confirmed: "confirmed",
                   cancelled: "cancelled", no_show: "missed", completed: "done" };
const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

function bkTz() { return (bk.data && bk.data.timezone) || undefined; }

function bkWhen(iso, opts) {
  return new Date(iso).toLocaleString([], { timeZone: bkTz(), ...opts });
}

function bkSpan(b) {
  return bkWhen(b.starts_at, { weekday: "short", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) +
    "–" + bkWhen(b.ends_at, { hour: "2-digit", minute: "2-digit" });
}

async function openBookings() {
  if (!state.sessionId) { toast("Pick an account first."); return; }
  $("bookings").classList.add("open");
  await bkLoad();
}

function closeBookings() { $("bookings").classList.remove("open"); }

async function bkLoad() {
  try {
    const q = bk.start ? `?start=${bk.start}&days=7` : "?days=7";
    bk.data = await sApi("GET", "/bookings" + q);
    bk.start = bk.data.start;
  } catch (err) { toast(err.message); return; }
  bkRender();
}

function bkRender() {
  const box = $("bk-body");
  box.textContent = "";
  const tabs = $("bk-tabs");
  tabs.textContent = "";
  for (const [key, label] of BK_TABS) {
    const count = key === "waiting" && bk.data ? ` (${bk.data.awaiting.length})` : "";
    const b = el("button", "pf-tab" + (bk.tab === key ? " on" : ""), label + count);
    b.addEventListener("click", () => { bk.tab = key; bk.selected = null; bkRender(); });
    tabs.appendChild(b);
  }
  if (bk.data && !bk.data.enabled) {
    box.appendChild(el("p", "pf-note warn-note",
      "Bookings are off for this client (booking.enabled in Settings → Config). Nothing new is detected " +
      "until they are turned on."));
  }
  ({ calendar: bkCalendar, waiting: bkWaiting, waitlist: bkWaitlist, hours: bkHours, links: bkLinks })[bk.tab](box);
}

/* --------------------------------------------------------- calendar */

function bkCalendar(box) {
  const nav = el("div", "bk-nav");
  const prev = el("button", "btn small", "◀ Previous week");
  const today = el("button", "btn small", "This week");
  const next = el("button", "btn small", "Next week ▶");
  const shift = (days) => {
    const d = new Date(bk.start + "T00:00:00Z");
    d.setUTCDate(d.getUTCDate() + days);
    bk.start = d.toISOString().slice(0, 10);
    bkLoad();
  };
  prev.addEventListener("click", () => shift(-7));
  next.addEventListener("click", () => shift(7));
  today.addEventListener("click", () => { bk.start = null; bkLoad(); });
  nav.append(prev, today, next, el("span", "muted", `Week of ${bk.start} · times in ${bk.data.timezone}`));
  box.appendChild(nav);

  const byDay = new Map();
  for (const b of bk.data.bookings) {
    const day = bkWhen(b.starts_at, { weekday: "long", day: "numeric", month: "long" });
    if (!byDay.has(day)) byDay.set(day, []);
    byDay.get(day).push(b);
  }
  if (!byDay.size) box.appendChild(el("div", "empty", "No bookings this week."));
  for (const [day, list] of byDay) {
    box.appendChild(el("h4", "bk-day", day));
    for (const b of list) box.appendChild(bkRow(b));
  }
}

function bkRow(b) {
  const row = el("div", "bk-row st-" + b.state + (bk.selected === b.id ? " open" : ""));
  const head = el("div", "bk-head");
  head.appendChild(el("span", "bk-time", bkWhen(b.starts_at, { hour: "2-digit", minute: "2-digit" })));
  head.appendChild(el("span", "bk-num", "#" + b.number));
  head.appendChild(el("span", "bk-who", b.customer_name || "customer"));
  if (b.service) head.appendChild(el("span", "muted", b.service));
  head.appendChild(el("span", "spacer"));
  head.appendChild(el("span", "bk-state", BK_STATE[b.state] || b.state));
  head.addEventListener("click", async () => {
    bk.selected = bk.selected === b.id ? null : b.id;
    bk.events = null;
    bkRender();
    if (bk.selected) {
      try { bk.events = (await sApi("GET", `/bookings/${b.id}`)).events; bkRender(); }
      catch (err) { toast(err.message); }
    }
  });
  row.appendChild(head);
  if (b.proposed_starts_at) {
    const who = b.proposed_by === "owner" ? "Proposed to the customer" : "The customer asks to move it to";
    row.appendChild(el("div", "bk-proposal", `${who}: ${bkWhen(b.proposed_starts_at,
      { weekday: "short", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" })}`));
  }
  if (bk.selected === b.id) row.appendChild(bkDetail(b));
  return row;
}

function bkDetail(b) {
  const box = el("div", "bk-detail");
  const facts = [
    ["When", bkSpan(b)],
    ["Customer", (b.customer_name || "") + (b.customer_username ? ` (@${b.customer_username})` : "")],
    ["Notes", b.notes], ["Cancelled", b.cancel_reason],
    ["Coming", b.attendance_confirmed_at ? "confirmed by the customer" : ""],
    ["Arrived", b.arrived_at ? bkWhen(b.arrived_at, { hour: "2-digit", minute: "2-digit" }) +
      (b.arrival_photo_match === true ? " (photo matches the entrance)" :
       b.arrival_photo_match === false ? " (photo did not match)" : "") : ""],
  ];
  for (const [label, value] of facts) {
    if (!value) continue;
    const line = el("div", "bk-fact");
    line.append(el("span", "muted", label + ": "), el("span", null, value));
    box.appendChild(line);
  }
  const open = new Date(b.starts_at) > new Date();
  const actions = el("div", "bk-actions");
  const act = (label, action, cls, extra) => {
    const button = el("button", "btn small " + (cls || ""), label);
    button.addEventListener("click", () => bkAct(b, action, extra));
    actions.appendChild(button);
  };
  if (b.state === "requested") act("Send to the owner again", "resend");
  if (b.state === "requested" || b.state === "pending") {
    act("Confirm", "confirm", "primary");
    act("Decline", "decline", "warn");
  }
  if (b.state === "confirmed" && b.proposed_by === "customer") {
    act("Accept the move", "confirm", "primary");
    act("Keep the old time", "decline");
  }
  if (["requested", "pending", "confirmed"].includes(b.state) && open) {
    act("Propose another time", "propose", null, true);
    if (b.state === "confirmed") act("Move it now", "reschedule", null, true);
    act("Cancel", "cancel", "warn");
  }
  if (b.state === "confirmed" && !open) {
    act("Done", "complete", "primary");
    act("Did not come", "no_show", "warn");
  }
  box.appendChild(actions);
  if (bk.events) {
    const list = el("div", "bk-events");
    for (const e of bk.events) {
      list.appendChild(el("div", null, `${fmtTime(e.created_at)} · ${e.action} by ${e.actor}` +
        (e.from_state ? ` (${e.from_state} → ${e.to_state})` : ` (${e.to_state})`) + (e.reason ? ` — ${e.reason}` : "")));
    }
    box.appendChild(list);
  }
  return box;
}

async function bkAct(b, action, needsTime) {
  const body = { action };
  if (needsTime) {
    const local = bkWhen(b.starts_at, { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit",
                                        minute: "2-digit", hour12: false });
    const answer = prompt(`New start time in ${bkTz()} (YYYY-MM-DD HH:MM). Now: ${local}`, "");
    if (!answer) return;
    const m = answer.trim().match(/^(\d{4}-\d{2}-\d{2})[ T](\d{1,2}):(\d{2})$/);
    if (!m) { toast("Use YYYY-MM-DD HH:MM"); return; }
    body.starts_at = `${m[1]}T${m[2].padStart(2, "0")}:${m[3]}`;
  }
  if (action === "cancel") {
    const reason = prompt("Why? (goes into the history; the customer is told it is cancelled)", "");
    if (reason === null) return;
    body.reason = reason;
  }
  try {
    await sApi("POST", `/bookings/${b.id}/action`, body);
    toast("Done. The customer and the owner are told.", "info");
  } catch (err) { toast(err.message); }
  await bkLoad();
}

/* ------------------------------------------------------------ waiting */

function bkWaiting(box) {
  box.appendChild(el("p", "pf-note", "Requests the owner has not answered yet, and moves customers asked for. " +
    "The owner can answer by text (YES 7, NO 7, 7 15:30) or you can answer here."));
  if (!bk.data.awaiting.length) box.appendChild(el("div", "empty", "Nothing is waiting."));
  for (const b of bk.data.awaiting) box.appendChild(bkRow(b));
}

/* ----------------------------------------------------------- waitlist */

async function bkWaitlist(box) {
  box.appendChild(el("p", "pf-note", "People waiting for a time to free up, first in line first. When a booking " +
    "is cancelled or moved, the first person whose wish covers the freed time is offered it."));
  const list = el("div");
  box.appendChild(list);
  let entries;
  try { entries = await sApi("GET", "/waitlist"); } catch (err) { toast(err.message); return; }
  if (!entries.length) list.appendChild(el("div", "empty", "Nobody is waiting."));
  for (const e of entries) {
    const row = el("div", "bk-row");
    const head = el("div", "bk-head");
    head.append(el("span", "bk-who", e.customer_name || "customer"),
      el("span", "muted", `${bkWhen(e.wanted_from, { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" })}` +
        ` – ${bkWhen(e.wanted_to, { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" })}`),
      el("span", "spacer"),
      el("span", "bk-state", e.state === "offered" ? "offered a time" : "waiting"));
    const remove = el("button", "btn small warn", "Remove");
    remove.addEventListener("click", async () => {
      try { await sApi("DELETE", `/waitlist/${e.id}`); bkRender(); } catch (err) { toast(err.message); }
    });
    head.appendChild(remove);
    row.appendChild(head);
    list.appendChild(row);
  }
}

/* ------------------------------------------------------ opening hours */

async function bkHours(box) {
  box.appendChild(el("p", "pf-note", "When bookings can be made, in the client's time zone. Several rows on one day " +
    "make breaks. With no rows at all, any time is accepted as long as it does not overlap another booking. " +
    "Closed days, notice and how far ahead are in Settings → Config under booking."));
  let data;
  try { data = await sApi("GET", "/availability"); } catch (err) { toast(err.message); return; }
  const rules = data.rules.map((r) => ({ ...r }));
  const table = el("table", "cfg-table");
  const draw = () => {
    table.textContent = "";
    const head = el("tr");
    for (const h of ["Day", "From", "To", "Slot (min)", "Gap after (min)", ""]) head.appendChild(el("td", "muted", h));
    table.appendChild(head);
    rules.forEach((r, i) => {
      const tr = el("tr");
      const day = el("select");
      WEEKDAYS.forEach((name, n) => { const o = el("option", null, name); o.value = n; o.selected = n === r.weekday; day.appendChild(o); });
      day.addEventListener("change", () => { r.weekday = Number(day.value); });
      const cell = (key, type) => {
        const input = el("input");
        input.type = type;
        input.value = r[key];
        input.addEventListener("input", () => { r[key] = type === "number" ? Number(input.value) : input.value; });
        return input;
      };
      const remove = el("button", "btn small warn", "Remove");
      remove.addEventListener("click", () => { rules.splice(i, 1); draw(); });
      for (const node of [day, cell("start_time", "time"), cell("end_time", "time"),
                          cell("slot_minutes", "number"), cell("buffer_minutes", "number"), remove]) {
        const td = el("td");
        td.appendChild(node);
        tr.appendChild(td);
      }
      table.appendChild(tr);
    });
  };
  draw();
  box.appendChild(table);
  const actions = el("div", "pf-actions");
  const add = el("button", "btn small", "+ Add hours");
  add.addEventListener("click", () => {
    const last = rules[rules.length - 1];
    rules.push({ weekday: last ? (last.weekday + 1) % 7 : 0, start_time: "09:00", end_time: "17:00",
                 slot_minutes: 60, buffer_minutes: 0 });
    draw();
  });
  const save = el("button", "btn primary", "Save opening hours");
  save.addEventListener("click", async () => {
    save.disabled = true;
    try { await sApi("PUT", "/availability", { rules }); toast("Opening hours saved.", "info"); }
    catch (err) { toast(err.message); }
    finally { save.disabled = false; }
  });
  actions.append(add, save);
  box.appendChild(actions);
}

/* ------------------------------------------------------------ links */

async function bkLinks(box) {
  const feed = el("div", "pf-section");
  feed.appendChild(el("div", "title", "Calendar feed for the owner"));
  box.appendChild(feed);
  try {
    const f = await sApi("GET", "/calendar-feed");
    if (!f.public_base_url_set) {
      feed.appendChild(el("p", "pf-note", "Not reachable yet: set PUBLIC_BASE_URL in .env and start the public " +
        "service (see the README). The link stays the same once it is."));
    } else {
      const url = el("input");
      url.readOnly = true;
      url.value = f.url;
      feed.appendChild(url);
      feed.appendChild(el("p", "pf-note", "Subscribe to it in Google Calendar, Apple Calendar or Outlook. " +
        "Anyone with the link can read the bookings, so share it only with the owner."));
    }
    const replace = el("button", "btn small warn", "Replace the link");
    replace.addEventListener("click", async () => {
      if (!confirm("Make a new link? The old one stops working at once.")) return;
      try { await sApi("POST", "/calendar-feed/regenerate"); bkRender(); } catch (err) { toast(err.message); }
    });
    feed.appendChild(replace);
  } catch (err) { feed.appendChild(el("div", "pf-errors", err.message)); }

  const usage = el("div", "pf-section");
  usage.appendChild(el("div", "title", "AI usage"));
  box.appendChild(usage);
  try {
    const u = await sApi("GET", "/ai-usage");
    const lim = (v, unit) => (v ? `${unit}${v}` : "no limit");
    usage.appendChild(el("div", null, `Today: ${u.today.tokens.toLocaleString()} tokens, €${u.today.eur.toFixed(2)} ` +
      `(limits: ${lim(u.limits.daily_tokens, "")} tokens, ${lim(u.limits.daily_spend_eur, "€")})`));
    usage.appendChild(el("div", null, `This month: ${u.month.tokens.toLocaleString()} tokens, €${u.month.eur.toFixed(2)} ` +
      `(limits: ${lim(u.limits.monthly_tokens, "")} tokens, ${lim(u.limits.monthly_spend_eur, "€")})`));
    if (u.reached) usage.appendChild(el("div", "pf-errors", "Stopped: " + u.reached + ". Messages are still received."));
    usage.appendChild(el("p", "pf-note", "The limits are in Settings → Config: limits.* and api_spend_cap_eur."));
  } catch (err) { usage.appendChild(el("div", "pf-errors", err.message)); }
}

$("open-bookings").addEventListener("click", openBookings);
$("bk-close").addEventListener("click", closeBookings);
$("bookings").addEventListener("click", (ev) => { if (ev.target === $("bookings")) closeBookings(); });
