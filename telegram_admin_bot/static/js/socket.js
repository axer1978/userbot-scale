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
      applyTenantConfig(data.tenant_config);
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
      break;

    // The tenant's config changed (Settings / Clients, or the industry).
    case "tenant_config":
      applyTenantConfig(data.config);
      break;

    case "status":
      applyStatus(data.status);
      break;

    // A kill switch changed for this client (controls.py).
    case "controls":
      applyControls(data);
      sfPoll();
      break;

    case "halted":
      toast("Stopped after a Telegram error: " + data.reason);
      sfPoll();
      break;

    case "escalation":
      toast(`${data.name || "A customer"} wrote “${data.keyword}”: the chat is paused and the owner was pinged.`);
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
      const when = new Date(b.starts_at).toLocaleString([], { dateStyle: "medium", timeStyle: "short", timeZone: b.tz });
      const what = { requested: "requested — not sent to the owner yet", pending: "waiting for the owner",
                     confirmed: "confirmed", cancelled: "cancelled", no_show: "marked as missed",
                     completed: "done" }[b.state] || b.state;
      toast(`Booking #${b.number} for ${b.customer_name || "a customer"} on ${when}: ${what}`, "info");
      if ($("bookings").classList.contains("open")) bkLoad();
      break;
    }

    case "waitlist":
      if ($("bookings").classList.contains("open") && bk.tab === "waitlist") bkRender();
      break;

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
  state.status = { ...(state.status || {}), ...status };
  applyControls(status);
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
    $("mode").textContent = "no business details in the prompt yet — open Settings";
  }
}

