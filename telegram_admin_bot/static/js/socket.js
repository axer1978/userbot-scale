"use strict";

/* ------------------------------------------------------------ websocket */

let socket = null;
let retryDelay = 1000;
let reconnectTimer = null;

// Closes whatever socket is open (if any) and stops it from reconnecting.
// Called right before switching to a different session_id, so the old
// session's socket never delivers events into the newly selected session.
function teardownSocket() {
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
  retryDelay = 1000;
  if (socket) {
    const old = socket;
    socket = null;
    old.close();
  }
}

function connectSocket(sessionId) {
  if (!sessionId) return;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/${encodeURIComponent(sessionId)}`);
  socket = ws;

  ws.addEventListener("open", () => {
    if (socket !== ws) return; // superseded by a later session switch
    retryDelay = 1000;
    $("conn-dot").classList.add("on");
    $("conn-text").textContent = "live";
  });

  ws.addEventListener("close", () => {
    if (socket !== ws) return;
    $("conn-dot").classList.remove("on");
    $("conn-text").textContent = "reconnecting…";
    reconnectTimer = setTimeout(() => connectSocket(sessionId), retryDelay);
    retryDelay = Math.min(retryDelay * 2, 15000);
  });

  ws.addEventListener("message", (ev) => {
    if (socket !== ws) return;
    let data;
    try { data = JSON.parse(ev.data); } catch (_) { return; }
    handleEvent(data);
  });
}

function handleEvent(data) {
  switch (data.type) {
    case "hello":
      state.conversations = data.conversations;
      state.media = data.media || [];
      if ($("media").classList.contains("open")) renderMedia();
      applyConfig(data.config);
      fillSettings(data.config);
      applyStatus(data.status);
      renderSidebar();
      break;

    case "message":
      state.drafting.delete(data.message.chat_id);
      upsertConversation(data.conversation);
      upsertMessage(data.message);
      break;

    case "conversation":
      upsertConversation(data.conversation);
      break;

    case "media":
      state.media = data.media || [];
      if ($("media").classList.contains("open")) renderMedia();
      renderThread();
      break;

    case "config":
      applyConfig(data.config);
      if (!$("settings").classList.contains("open")) fillSettings(data.config);
      break;

    case "status":
      applyStatus(data.status);
      break;

    case "drafting":
      state.drafting.add(data.chat_id);
      if (data.chat_id === state.activeChatId) renderThread();
      break;

    // A link made itself while I was looking at something else; say so, and
    // fold it into the header if it belongs to the chat that's open.
    case "chat_link":
      toast(`${data.link.source_name} looks like the same person as another ` +
            `chat (${data.link.reason}) — their context is now shared.`, "info");
      if (data.link.chat_id === state.activeChatId) {
        if (!state.links.some((l) => l.source_id === data.link.source_id)) {
          state.links.push(data.link);
        }
        renderThreadHeader();
      }
      break;

    case "chat_unlink":
      if (data.chat_id === state.activeChatId) {
        state.links = state.links.filter((l) => l.source_id !== data.source_id);
        renderThreadHeader();
      }
      break;

    case "outreach":
      outreach.items = data.items;
      renderQueue();
      break;

    case "outreach_paused":
      toast(data.reason, "info");
      break;

    case "booking": {
      const b = data.booking;
      const when = new Date(b.start).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
      const what = { pending: "requested — waiting for the provider",
                     confirmed: "confirmed", declined: "declined",
                     superseded: "replaced by a newer request" }[b.status] || b.status;
      toast(`Booking #${b.id} for ${b.client_name} on ${when}: ${what}`, "info");
      break;
    }

    case "error":
      state.drafting.delete(data.chat_id);
      toast(data.text);
      if (data.message) upsertMessage(data.message);
      else if (data.chat_id === state.activeChatId) renderThread();
      break;
  }
}

function applyStatus(status) {
  if (!status) return;
  state.status = status;
  // Several instances can be open in separate tabs; name this one.
  if (status.instance) {
    $("brand-instance").textContent = status.instance;
    document.title = `${status.instance} — Telegram AI Assistant`;
  }
  const dot = $("conn-dot");
  if (status.telegram_connected) {
    dot.classList.add("on");
    $("conn-text").textContent = status.me && status.me.name
      ? `connected as ${status.me.name}` : "connected";
  } else {
    dot.classList.remove("on");
    $("conn-text").textContent = "Telegram offline";
    if (status.telegram_error) toast(status.telegram_error);
  }
  if (status.persona_configured === false) {
    $("mode").textContent = "persona not configured — open Settings";
  }
}

