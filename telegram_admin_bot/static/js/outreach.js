"use strict";

/* ------------------------------------------------------------- outreach */

const outreach = { contacts: [], selected: new Set(), items: [] };

function renderContacts() {
  const box = $("o-contacts");
  const term = $("o-filter").value.trim().toLowerCase();
  const shown = outreach.contacts.filter((c) =>
    !term || c.display_name.toLowerCase().includes(term) ||
    (c.username || "").toLowerCase().includes(term));

  box.textContent = "";
  if (!shown.length) {
    const empty = el("div", "row-item", outreach.contacts.length
      ? "No contacts match that filter." : "No contacts found on this account.");
    box.appendChild(empty);
  }

  for (const c of shown) {
    const row = el("label", "row-item");
    const cb = el("input");
    cb.type = "checkbox";
    cb.checked = outreach.selected.has(c.chat_id);
    cb.addEventListener("change", () => {
      if (cb.checked) outreach.selected.add(c.chat_id);
      else outreach.selected.delete(c.chat_id);
      updateCount();
    });
    row.appendChild(cb);
    row.appendChild(el("span", null, c.display_name));
    if (c.username) row.appendChild(el("span", "handle", "@" + c.username));
    if (c.is_bot) row.appendChild(el("span", "badge bot", "bot"));
    box.appendChild(row);
  }
  updateCount();
}

function updateCount() {
  $("o-count").textContent =
    `— ${outreach.selected.size} selected of ${outreach.contacts.length}`;
}

function renderQueue() {
  const box = $("o-status");
  box.textContent = "";
  if (!outreach.items.length) {
    box.textContent = "Nothing queued yet.";
    return;
  }
  for (const item of outreach.items.slice().reverse()) {
    const row = el("div", "q");
    row.appendChild(el("span", "tag " + item.status, item.status));
    row.appendChild(el("span", "who", item.display_name || String(item.chat_id)));
    row.appendChild(el("span", "txt", item.error || item.message || item.goal));
    box.appendChild(row);
  }
}

async function loadContacts() {
  $("o-contacts").textContent = "Loading contacts…";
  try {
    outreach.contacts = await sApi("GET", "/contacts");
    renderContacts();
  } catch (err) {
    $("o-contacts").textContent = "Could not load contacts: " + err.message;
  }
}

async function loadQueue() {
  try {
    outreach.items = await sApi("GET", "/outreach");
    renderQueue();
  } catch (err) { /* the websocket will refresh it */ }
}

$("open-outreach").addEventListener("click", async () => {
  $("outreach").classList.add("open");
  if (state.config) {
    $("o-auto").checked = state.config.outreach.auto_send;
    $("o-min").value = state.config.outreach.min_gap_seconds;
    $("o-max").value = state.config.outreach.max_gap_seconds;
    $("o-limit").value = state.config.outreach.daily_limit;
  }
  await Promise.all([loadContacts(), loadQueue()]);
});

$("o-close").addEventListener("click", () => $("outreach").classList.remove("open"));
$("outreach").addEventListener("click", (ev) => {
  if (ev.target === $("outreach")) $("outreach").classList.remove("open");
});
$("o-filter").addEventListener("input", renderContacts);

$("o-all").addEventListener("click", () => {
  const term = $("o-filter").value.trim().toLowerCase();
  for (const c of outreach.contacts) {
    if (!term || c.display_name.toLowerCase().includes(term) ||
        (c.username || "").toLowerCase().includes(term)) {
      outreach.selected.add(c.chat_id);
    }
  }
  renderContacts();
});

$("o-none").addEventListener("click", () => {
  outreach.selected.clear();
  renderContacts();
});

$("o-queue").addEventListener("click", async () => {
  const goal = $("o-goal").value.trim();
  if (!goal) { toast("Say what the message should achieve."); return; }
  if (!outreach.selected.size) { toast("Pick at least one contact."); return; }

  // Pacing and the daily cap live in config, so save them before queueing.
  try {
    const cfg = JSON.parse(JSON.stringify(state.config));
    cfg.outreach.auto_send = $("o-auto").checked;
    cfg.outreach.min_gap_seconds = Number($("o-min").value);
    cfg.outreach.max_gap_seconds = Number($("o-max").value);
    cfg.outreach.daily_limit = Number($("o-limit").value);
    applyConfig(await sApi("PUT", "/config", cfg));
  } catch (err) { toast("Could not save outreach settings: " + err.message); return; }

  $("o-queue").disabled = true;
  try {
    const res = await sApi("POST", "/outreach",
                          { chat_ids: [...outreach.selected], goal });
    const skipped = res.skipped ? `, ${res.skipped} already queued` : "";
    toast(`Queued ${res.queued} message(s)${skipped}.`, "info");
    outreach.selected.clear();
    renderContacts();
    await loadQueue();
  } catch (err) { toast(err.message); }
  $("o-queue").disabled = false;
});

$("o-cancel").addEventListener("click", async () => {
  try {
    const res = await sApi("POST", "/outreach/cancel");
    toast(`Cancelled ${res.cancelled} queued message(s).`, "info");
    await loadQueue();
  } catch (err) { toast(err.message); }
});

