"use strict";

/* The public terms of service page (GET /api/terms). Same strict
   Content-Security-Policy as the dashboard: nodes are made with
   createElement and filled with textContent, never innerHTML, and the
   styles come from /owner/owner.css. */

const $ = (id) => document.getElementById(id);

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

function fmtDate(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return "";
  return d.toLocaleString([], { day: "numeric", month: "long", year: "numeric" });
}

/* Terms text: "## " starts a heading, "- " a bullet (consecutive bullets
   share one list), a blank line ends the paragraph or list, and other
   consecutive lines join into one paragraph. Text only, never markup.
   The same function is in owner.js. */
function renderTerms(container, body) {
  let para = null;
  let list = null;
  const flush = () => {
    if (para) container.appendChild(el("p", null, para.join(" ")));
    para = null;
  };
  for (const raw of String(body || "").split("\n")) {
    const line = raw.replace(/\s+$/, "");
    if (!line.trim()) {
      flush();
      list = null;
    } else if (line.startsWith("## ")) {
      flush();
      list = null;
      container.appendChild(el("h2", null, line.slice(3).trim()));
    } else if (line.startsWith("- ")) {
      flush();
      if (!list) list = container.appendChild(el("ul"));
      list.appendChild(el("li", null, line.slice(2).trim()));
    } else {
      list = null;
      if (!para) para = [];
      para.push(line.trim());
    }
  }
  flush();
  return container;
}

function status(text, className) {
  const n = $("terms-status");
  n.className = className || "empty";
  n.textContent = text || "";
  n.hidden = !text;
}

async function load() {
  let res;
  try {
    res = await fetch("/api/terms", { credentials: "same-origin" });
  } catch (_) {
    status("Could not reach the server. Please try again later.", "error-box");
    return;
  }
  if (res.status === 404) { status("No terms are published yet."); return; }
  if (!res.ok) { status(`Could not load the terms (error ${res.status}). Please try again later.`, "error-box"); return; }
  let terms;
  try { terms = await res.json(); } catch (_) {
    status("Could not read the terms. Please try again later.", "error-box");
    return;
  }
  $("terms-title").textContent = terms.title || "Terms of Service";
  const date = fmtDate(terms.published_at);
  $("terms-meta").textContent = `Version ${terms.version}` + (date ? `, published ${date}` : "");
  status("");
  renderTerms(clear($("terms-body")), terms.body);
}

load();
