"use strict";

/* ----------------------------------------------------------------- style */

const styleState = { contacts: [], activeChatId: null };

function contactLabel(c) {
  return c.display_name + (c.username ? ` (@${c.username})` : "");
}

// Sourced from conversations, not the address book: styling should apply to
// anyone you actually talk to, whether or not they're a saved contact.
async function loadStyleContacts() {
  const select = $("cs-select");
  select.innerHTML = "";
  select.appendChild(new Option("Loading…", ""));

  const byId = new Map();
  for (const c of state.conversations) {
    byId.set(String(c.chat_id), { chat_id: c.chat_id, display_name: c.display_name, username: c.username });
  }
  try {
    for (const c of await sApi("GET", "/contacts")) {
      if (!byId.has(String(c.chat_id))) byId.set(String(c.chat_id), c);
    }
  } catch (_) { /* address book is a bonus; conversations are enough */ }

  // Anything already styled stays selectable even if the chat has scrolled away.
  for (const id of Object.keys((state.config && state.config.contacts) || {})) {
    if (!byId.has(id)) byId.set(id, { chat_id: Number(id), display_name: `Chat ${id}`, username: null });
  }

  styleState.contacts = [...byId.values()]
    .sort((a, b) => a.display_name.localeCompare(b.display_name));

  select.innerHTML = "";
  if (!styleState.contacts.length) {
    select.appendChild(new Option("No chats yet — message someone first", ""));
    return;
  }
  select.appendChild(new Option("Choose a chat…", ""));
  const styled = (state.config && state.config.contacts) || {};
  for (const c of styleState.contacts) {
    const mark = styled[String(c.chat_id)] ? " ✓" : "";
    select.appendChild(new Option(contactLabel(c) + mark, String(c.chat_id)));
  }
}

function emptyContactStyle() {
  return {
    persona_extra: "", style_notes: "", chat_samples: "", message_length: "auto",
    min_delay_seconds: null, max_delay_seconds: null,
    typing_speed_cps: null, typing_max_seconds: null,
    online_delay_min: null, online_delay_max: null,
    offline_delay_min: null, offline_delay_max: null,
  };
}

function fillContactForm(style) {
  $("cs-length").value = style.message_length || "auto";
  $("cs-notes").value = style.style_notes || "";
  $("cs-extra").value = style.persona_extra || "";
  $("cs-samples").value = style.chat_samples || "";
  $("cs-min").value = style.min_delay_seconds ?? "";
  $("cs-max").value = style.max_delay_seconds ?? "";
  $("cs-cps").value = style.typing_speed_cps ?? "";
  $("cs-tmax").value = style.typing_max_seconds ?? "";
  $("cs-on-min").value = style.online_delay_min ?? "";
  $("cs-on-max").value = style.online_delay_max ?? "";
  $("cs-off-min").value = style.offline_delay_min ?? "";
  $("cs-off-max").value = style.offline_delay_max ?? "";
}

function numOrNull(id) {
  const v = $(id).value;
  return v === "" ? null : Number(v);
}

function collectContactForm() {
  return {
    message_length: $("cs-length").value,
    style_notes: $("cs-notes").value,
    persona_extra: $("cs-extra").value,
    chat_samples: $("cs-samples").value,
    min_delay_seconds: numOrNull("cs-min"),
    max_delay_seconds: numOrNull("cs-max"),
    typing_speed_cps: numOrNull("cs-cps"),
    typing_max_seconds: numOrNull("cs-tmax"),
    online_delay_min: numOrNull("cs-on-min"),
    online_delay_max: numOrNull("cs-on-max"),
    offline_delay_min: numOrNull("cs-off-min"),
    offline_delay_max: numOrNull("cs-off-max"),
  };
}

$("cs-select").addEventListener("change", () => {
  const id = $("cs-select").value;
  styleState.activeChatId = id || null;
  const form = $("cs-form");
  if (!id) { form.style.display = "none"; return; }
  form.style.display = "block";
  const existing = (state.config && state.config.contacts && state.config.contacts[id]) || emptyContactStyle();
  fillContactForm(existing);
});

$("cs-save").addEventListener("click", async () => {
  if (!styleState.activeChatId) { toast("Pick a contact first."); return; }
  try {
    const cfg = JSON.parse(JSON.stringify(state.config));
    cfg.contacts = cfg.contacts || {};
    cfg.contacts[styleState.activeChatId] = collectContactForm();
    applyConfig(await sApi("PUT", "/config", cfg));
    const keep = styleState.activeChatId;
    await loadStyleContacts();
    $("cs-select").value = keep;
    toast("Contact style saved.", "info");
  } catch (err) { toast(err.message); }
});

$("cs-reset").addEventListener("click", async () => {
  if (!styleState.activeChatId) { toast("Pick a contact first."); return; }
  try {
    const cfg = JSON.parse(JSON.stringify(state.config));
    if (cfg.contacts) delete cfg.contacts[styleState.activeChatId];
    applyConfig(await sApi("PUT", "/config", cfg));
    fillContactForm(emptyContactStyle());
    const keep = styleState.activeChatId;
    await loadStyleContacts();
    $("cs-select").value = keep;
    toast("Reset to global settings.", "info");
  } catch (err) { toast(err.message); }
});

$("fs-save").addEventListener("click", async () => {
  try {
    const cfg = JSON.parse(JSON.stringify(state.config));
    cfg.finetune = cfg.finetune || { writing_samples: "" };
    cfg.finetune.writing_samples = $("fs-samples").value;
    applyConfig(await sApi("PUT", "/config", cfg));
    toast("Writing samples saved.", "info");
  } catch (err) { toast(err.message); }
});

$("open-style").addEventListener("click", async () => {
  $("style").classList.add("open");
  $("fs-samples").value = state.config && state.config.finetune ? state.config.finetune.writing_samples : "";
  $("cs-form").style.display = "none";
  $("cs-select").value = "";
  styleState.activeChatId = null;
  await loadStyleContacts();
});

$("st-close").addEventListener("click", () => $("style").classList.remove("open"));
$("style").addEventListener("click", (ev) => {
  if (ev.target === $("style")) $("style").classList.remove("open");
});

