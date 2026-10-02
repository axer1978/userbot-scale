"use strict";

/* --------------------------------------------------------------- sidebar */

function renderSidebar() {
  const sidebar = $("sidebar");
  sidebar.textContent = "";

  if (!state.sessionId) {
    const empty = el("div", "empty", "Pick a session above to get started.");
    empty.style.padding = "24px 16px";
    sidebar.appendChild(empty);
    return;
  }

  if (!state.conversations.length) {
    const empty = el("div", "empty", "No conversations yet.");
    empty.style.padding = "24px 16px";
    sidebar.appendChild(empty);
    return;
  }

  for (const conv of state.conversations) {
    const item = el("button", "conv" + (conv.chat_id === state.activeChatId ? " active" : ""));

    const top = el("div", "conv-top");
    top.appendChild(el("span", "conv-name", conv.display_name || String(conv.chat_id)));
    top.appendChild(el("span", "conv-time", fmtTime(conv.last_message_at)));
    item.appendChild(top);

    item.appendChild(el("div", "conv-preview", conv.last_message_preview || "—"));

    const meta = el("div", "conv-meta");
    if (conv.is_bot) meta.appendChild(el("span", "badge bot", "bot"));
    if (conv.automation_paused) {
      const escalated = (conv.paused_reason || "").startsWith("escalation");
      const badge = el("span", "badge " + (escalated ? "escalated" : "paused"), escalated ? "escalated" : "paused");
      badge.title = conv.paused_reason || "paused by hand";
      meta.appendChild(badge);
    }
    if (takeoverActive(conv)) meta.appendChild(el("span", "badge takeover", "you're handling"));
    if (conv.unread > 0) meta.appendChild(el("span", "unread", String(conv.unread)));

    const spacer = el("span"); spacer.style.flex = "1";
    meta.appendChild(spacer);

    const toggle = el("span", "btn small" + (conv.automation_paused ? " on" : ""),
                      conv.automation_paused ? "Resume" : "Pause");
    toggle.setAttribute("role", "button");
    toggle.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      try {
        await sApi("POST", `/conversations/${conv.chat_id}/pause`,
                  { paused: !conv.automation_paused });
      } catch (err) { toast(err.message); }
    });
    meta.appendChild(toggle);
    item.appendChild(meta);

    item.addEventListener("click", () => selectConversation(conv.chat_id));
    sidebar.appendChild(item);
  }
}

/* ---------------------------------------------------------------- thread */

// Someone wrote in this chat by hand; the bot stays quiet until then.
function takeoverActive(conv) {
  return !!(conv && conv.human_takeover_until && new Date(conv.human_takeover_until) > new Date());
}

function conversationById(chatId) {
  return state.conversations.find((c) => c.chat_id === chatId) || null;
}

function renderThreadHeader() {
  const header = $("thread-header");
  header.textContent = "";
  const conv = conversationById(state.activeChatId);
  if (!conv) {
    const span = el("span", null, "No conversation selected");
    span.style.color = "var(--muted)";
    header.appendChild(span);
    return;
  }

  // Phones: the list and the chat take turns; this goes back to the list.
  const back = el("button", "btn small back-btn", "‹ Chats");
  back.addEventListener("click", () => document.body.classList.remove("chat-open"));
  header.appendChild(back);

  const name = el("strong", null, conv.display_name || String(conv.chat_id));
  header.appendChild(name);
  if (conv.username) {
    const handle = el("span", null, "@" + conv.username);
    handle.style.color = "var(--muted)";
    header.appendChild(handle);
  }
  if (conv.is_bot) header.appendChild(el("span", "badge bot", "bot"));

  for (const link of state.links) header.appendChild(linkChip(link));

  const spacer = el("span", "spacer");
  header.appendChild(spacer);

  header.appendChild(state.linkOptions ? linkPicker(conv) : linkButton(conv));

  if (takeoverActive(conv)) {
    const until = new Date(conv.human_takeover_until);
    const back = el("button", "btn small", "Hand back to the bot");
    back.title = "Someone wrote here by hand, so the bot is quiet until " +
      until.toLocaleString([], { dateStyle: "short", timeStyle: "short" }) + ". It then carries on by itself.";
    back.addEventListener("click", async () => {
      try { upsertConversation(await sApi("POST", `/conversations/${conv.chat_id}/takeover`, { active: false })); }
      catch (err) { toast(err.message); }
    });
    header.appendChild(el("span", "badge takeover", "bot quiet until " +
      until.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })));
    header.appendChild(back);
  }

  const toggle = el("button", "btn small" + (conv.automation_paused ? " on" : ""),
                    conv.automation_paused ? "Automation paused" : "Pause automation");
  if (conv.paused_reason) toggle.title = conv.paused_reason;
  toggle.addEventListener("click", async () => {
    try {
      await sApi("POST", `/conversations/${conv.chat_id}/pause`,
                { paused: !conv.automation_paused });
    } catch (err) { toast(err.message); }
  });
  header.appendChild(toggle);
}

/* A chat this conversation draws on. The reason is on the chip's tooltip:
   a link the app made on its own should never be unexplained. */
function linkChip(link) {
  const chip = el("span", "badge link");
  chip.appendChild(el("span", null, "context: " + link.source_name));
  chip.title = (link.origin === "auto" ? "Detected: " : "Linked by hand: ") +
               (link.reason || "same person");
  const cut = el("button", null, "×");
  cut.title = "Stop drawing on this chat";
  cut.addEventListener("click", async () => {
    try {
      const res = await sApi(
        "DELETE", `/conversations/${link.chat_id}/links/${link.source_id}`);
      state.links = res.links;
      renderThreadHeader();
      toast("Unlinked. This chat's replies won't use that one.", "info");
    } catch (err) { toast(err.message); }
  });
  chip.appendChild(cut);
  return chip;
}

function linkButton(conv) {
  const button = el("button", "btn small", "Link chat…");
  button.title = "Answer this chat with what another chat already knows";
  button.addEventListener("click", async () => {
    try {
      const data = await sApi("GET", `/conversations/${conv.chat_id}/links`);
      state.links = data.links;
      state.linkOptions = data.suggestions;
    } catch (err) { toast(err.message); return; }
    renderThreadHeader();
  });
  return button;
}

function linkPicker(conv) {
  const picker = el("select", "link-picker");
  picker.appendChild(new Option("Draw on which chat?", ""));

  const linked = new Set(state.links.map((l) => l.source_id));
  // Whatever detection thought was the same person but would not act on by
  // itself goes to the top — the rest is every conversation, in case it is
  // someone it had no way of recognising.
  if (state.linkOptions.length) {
    const suggested = document.createElement("optgroup");
    suggested.label = "Looks like the same person";
    for (const s of state.linkOptions) {
      suggested.appendChild(new Option(`${contactLabel(s)} — ${s.reason}`, s.chat_id));
    }
    picker.appendChild(suggested);
  }
  const all = document.createElement("optgroup");
  all.label = "All conversations";
  for (const c of state.conversations) {
    if (c.chat_id === conv.chat_id || linked.has(c.chat_id)) continue;
    all.appendChild(new Option(contactLabel(c), c.chat_id));
  }
  picker.appendChild(all);

  picker.addEventListener("change", async () => {
    const sourceId = Number(picker.value);
    state.linkOptions = null;
    if (sourceId) {
      try {
        const res = await sApi("POST", `/conversations/${conv.chat_id}/links`,
                              { source_id: sourceId });
        state.links = res.links;
        toast("Linked. Replies here will draw on that chat.", "info");
      } catch (err) { toast(err.message); }
    }
    renderThreadHeader();
  });
  picker.addEventListener("blur", () => {
    state.linkOptions = null;
    renderThreadHeader();
  });
  setTimeout(() => picker.focus(), 0);
  return picker;
}

function renderThread() {
  const thread = $("thread");
  const stick = thread.scrollTop + thread.clientHeight >= thread.scrollHeight - 60;
  thread.textContent = "";

  if (state.activeChatId === null) {
    thread.appendChild(el("div", "empty", "Incoming DMs appear here as they arrive."));
    return;
  }

  for (const msg of state.messages) {
    thread.appendChild(msg.status === "pending_approval" ? draftNode(msg) : messageNode(msg));
  }

  if (state.drafting.has(state.activeChatId)) {
    thread.appendChild(el("div", "typing", "AI is preparing a reply…"));
  }

  if (stick) thread.scrollTop = thread.scrollHeight;
}

function messageNode(msg) {
  if (msg.status === "note") {
    const node = el("div", "msg note");
    node.appendChild(el("div", "label", "note"));
    node.appendChild(el("div", null, msg.text));
    node.appendChild(el("div", "time", fmtTime(msg.created_at)));
    return node;
  }
  if (msg.status === "error") {
    const node = el("div", "msg error");
    node.appendChild(el("div", "label", "error"));
    node.appendChild(el("div", null, msg.text));
    node.appendChild(el("div", "time", fmtTime(msg.created_at)));
    return node;
  }

  const isIn = msg.direction === "in";
  const cls = msg.status === "rejected" ? "msg rejected" : (isIn ? "msg in" : "msg out");
  const node = el("div", cls + (msg.deleted_at ? " deleted" : ""));
  node.appendChild(el("div", "label",
    (isIn ? "them" : (msg.status === "rejected" ? "rejected draft" : "me")) +
    (msg.deleted_at ? " · deleted " + fmtTime(msg.deleted_at) : "")));
  if (msg.text) node.appendChild(el("div", null, msg.text));
  const files = attachmentsNode(msg);
  if (files) node.appendChild(files);
  node.appendChild(el("div", "time", fmtTime(msg.created_at)));
  return node;
}

function mediaById(id) {
  return state.media.find((m) => m.id === id) || null;
}

function mediaPreview(item, small) {
  const src = sPath(`/media/${item.id}/file`);
  if (item.kind === "video") {
    const v = el("video");
    v.src = src; v.controls = true; v.preload = "metadata"; v.muted = true;
    if (small) v.className = "preview";
    return v;
  }
  const img = el("img");
  img.src = src; img.alt = item.description || item.file; img.loading = "lazy";
  if (small) img.className = "preview";
  return img;
}

// Thumbnails for the files a row carries: a sent file, or what a draft will
// send with it. A file gone from the library is named rather than shown.
function attachmentsNode(msg) {
  const ids = msg.attachments || [];
  if (!ids.length) return null;
  const box = el("div", "attachments");
  for (const id of ids) {
    const item = mediaById(id);
    if (!item) { box.appendChild(el("span", "chip", `file #${id} (removed)`)); continue; }
    box.appendChild(mediaPreview(item, false));
  }
  return box;
}

// Text of drafts currently being edited, keyed by draft id, so a re-render
// triggered by an unrelated incoming message doesn't discard my edit.
const draftEdits = new Map();

function draftNode(msg) {
  const node = el("div", "draft");
  node.appendChild(el("div", "label", "AI draft — awaiting approval"));

  const body = el("div", null, msg.text);
  const box = el("textarea");
  box.rows = 4;
  box.value = draftEdits.has(msg.id) ? draftEdits.get(msg.id) : msg.text;
  box.addEventListener("input", () => draftEdits.set(msg.id, box.value));

  let editing = draftEdits.has(msg.id);
  node.appendChild(body);
  node.appendChild(box);
  const files = attachmentsNode(msg);
  if (files) {
    const ids = msg.attachments || [];
    const kinds = ids.map((id) => (mediaById(id) || {}).kind || "file");
    node.appendChild(el("div", "label", "will send " + kinds.join(" + ") + " with it"));
    node.appendChild(files);
  }

  const actions = el("div", "draft-actions");
  const approve = el("button", "btn small primary", "Approve & Send");
  const edit = el("button", "btn small", "Edit then Send");
  const reject = el("button", "btn small warn", "Reject");

  const showEditor = () => {
    editing = true;
    body.style.display = "none";
    box.style.display = "block";
    approve.style.display = "none";
    edit.textContent = "Send edited";
    edit.classList.add("primary");
  };
  const showPreview = () => {
    editing = false;
    body.style.display = "";
    box.style.display = "none";
  };

  approve.addEventListener("click", () => sendDraft(msg.id, null, approve));
  edit.addEventListener("click", () => {
    if (!editing) { showEditor(); box.focus(); return; }
    sendDraft(msg.id, box.value, edit);
  });
  reject.addEventListener("click", async () => {
    reject.disabled = true;
    draftEdits.delete(msg.id);
    try { await sApi("POST", `/drafts/${msg.id}/reject`); }
    catch (err) { toast(err.message); reject.disabled = false; }
  });

  actions.appendChild(approve);
  actions.appendChild(edit);
  actions.appendChild(reject);
  node.appendChild(actions);
  node.appendChild(el("div", "time", fmtTime(msg.created_at)));

  if (editing) showEditor(); else showPreview();
  return node;
}

async function sendDraft(draftId, text, button) {
  button.disabled = true;
  try {
    await sApi("POST", `/drafts/${draftId}/approve`, text === null ? {} : { text });
    draftEdits.delete(draftId);
  } catch (err) {
    toast(err.message);
    button.disabled = false;
  }
}

/* ----------------------------------------------------------- composer */

function renderComposer() {
  let composer = $("composer");
  if (state.activeChatId === null) {
    if (composer) composer.remove();
    return;
  }
  if (composer) return;

  composer = el("div");
  composer.id = "composer";
  const box = el("textarea");
  box.placeholder = "Write a message as yourself…";
  box.rows = 1;
  const send = el("button", "btn primary", "Send");

  const submit = async () => {
    const text = box.value.trim();
    if (!text) return;
    send.disabled = true;
    try {
      await sApi("POST", `/conversations/${state.activeChatId}/send`, { text });
      box.value = "";
    } catch (err) { toast(err.message); }
    send.disabled = false;
    box.focus();
  };

  send.addEventListener("click", submit);
  box.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); submit(); }
  });

  composer.appendChild(box);
  composer.appendChild(send);
  $("panel").appendChild(composer);
}

/* ------------------------------------------------------------ selection */

async function selectConversation(chatId) {
  state.activeChatId = chatId;
  document.body.classList.add("chat-open");
  document.body.classList.remove("menu-open");
  state.links = [];
  state.linkOptions = null;
  renderSidebar();
  renderThreadHeader();
  renderComposer();
  try {
    const data = await sApi("GET", `/conversations/${chatId}/messages`);
    state.messages = data.messages;
    state.links = data.links || [];
    renderThreadHeader();
    renderThread();
    const conv = await sApi("POST", `/conversations/${chatId}/read`);
    upsertConversation(conv);
  } catch (err) { toast(err.message); }
}

function upsertConversation(conv) {
  if (!conv) return;
  const idx = state.conversations.findIndex((c) => c.chat_id === conv.chat_id);
  if (idx === -1) state.conversations.unshift(conv);
  else state.conversations[idx] = conv;
  state.conversations.sort((a, b) =>
    String(b.last_message_at || "").localeCompare(String(a.last_message_at || "")));
  renderSidebar();
  if (conv.chat_id === state.activeChatId) renderThreadHeader();
}

function upsertMessage(msg) {
  if (msg.chat_id !== state.activeChatId) return;
  const idx = msg.id === null ? -1 : state.messages.findIndex((m) => m.id === msg.id);
  if (idx === -1) state.messages.push(msg);
  else state.messages[idx] = msg;
  renderThread();
}

