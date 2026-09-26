"use strict";

const state = {
  sessionId: null,
  sessions: [],
  conversations: [],
  messages: [],
  activeChatId: null,
  // This account's own settings (pause switch, per-contact styles), and the
  // effective config of the tenant it belongs to (see settings.js).
  config: null,
  tenantConfig: null,
  status: null,
  auth: null,
  drafting: new Set(),
  // Chats the open conversation borrows context from, and the picker that
  // adds one. Both belong to whichever chat is on screen, so they reset with it.
  links: [],
  linkOptions: null,
  // The media library, as the server lists it; refreshed over the socket.
  media: [],
};

const $ = (id) => document.getElementById(id);

/* ------------------------------------------------------------------ utils */

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;   // textContent: never inject HTML
  return node;
}

function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return "";
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  return sameDay
    ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleDateString([], { month: "short", day: "numeric" }) + " " +
      d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function toast(text, kind) {
  const node = el("div", "toast" + (kind === "info" ? " info" : ""), text);
  $("toasts").appendChild(node);
  setTimeout(() => node.remove(), kind === "info" ? 4000 : 9000);
}

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = res.statusText;
    let errors = null;
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    // Config validation answers {message, errors: [{path, message}]}.
    if (detail && typeof detail === "object") {
      errors = detail.errors || null;
      detail = detail.message || JSON.stringify(detail);
    }
    const err = new Error(detail);
    err.status = res.status;
    err.errors = errors;
    throw err;
  }
  return res.status === 204 ? null : res.json();
}

// Every per-session route lives under /api/sessions/{session_id}/...; this
// builds that path from whichever session is currently selected (or an
// explicit override, used when a request has to outlive a session switch).
function sPath(subpath, sessionId) {
  const id = sessionId || state.sessionId;
  return `/api/sessions/${encodeURIComponent(id)}${subpath}`;
}

function sApi(method, subpath, body, sessionId) {
  return api(method, sPath(subpath, sessionId), body);
}

