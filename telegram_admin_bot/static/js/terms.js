"use strict";

/* ------------------------------------------------------ terms & sign-up */
// The terms of service every client login accepts, and the switch that lets
// new clients ask for a login themselves at /owner/ (terms_admin_api.py,
// terms.py). A published version is permanent: the only way to change the
// text is to publish a new version. The text format is tiny on purpose
// ("## " heading, "- " bullet, blank line = new paragraph) so every page
// renders it with textContent only.

const tm = { tab: "edit", state: null, draft: null, preview: false, error: null };

const TM_TABS = [["edit", "Terms & sign-up"], ["history", "History"]];

function tmPublicUrls() {
  return { signup: location.origin + "/owner/", terms: location.origin + "/terms/" };
}

async function openTerms(tab) {
  if (tab) tm.tab = tab;
  $("terms").classList.add("open");
  await tmRender();
}

function closeTerms() { $("terms").classList.remove("open"); }

async function tmRender() {
  const box = $("tm-body");
  box.textContent = "";
  try {
    tm.state = await api("GET", "/api/platform/terms");
  } catch (err) {
    tmTabs();
    box.appendChild(el("div", "pf-errors", err.message));
    return;
  }
  tmDraw();
}

// Draws from tm.state without fetching again (after a publish or a toggle,
// whose answer is the same state).
function tmDraw() {
  if (!tm.draft) tmResetDraft();
  tmTabs();
  const box = $("tm-body");
  box.textContent = "";
  (tm.tab === "history" ? tmHistory : tmEdit)(box);
}

function tmTabs() {
  const tabs = $("tm-tabs");
  tabs.textContent = "";
  for (const [key, label] of TM_TABS) {
    const b = el("button", "pf-tab" + (tm.tab === key ? " on" : ""), label);
    b.addEventListener("click", () => { tm.tab = key; tmDraw(); });
    tabs.appendChild(b);
  }
}

// The editor starts from the version shown now, or from the starter text
// before anything is published.
function tmResetDraft() {
  const s = tm.state;
  const from = s && s.current ? s.current : (s ? s.starter : { title: "", body: "" });
  tm.draft = { title: from.title, body: from.body, note: "", requires: true };
  tm.error = null;
}

function tmCount(text, needle) {
  if (!needle) return 0;
  let n = 0;
  for (let i = text.indexOf(needle); i !== -1; i = text.indexOf(needle, i + needle.length)) n++;
  return n;
}

/* ------------------------------------------------------------ rendering */

// Text with every [[FILL IN …]] marked, still textContent only.
function tmInline(node, text) {
  const ph = (tm.state && tm.state.placeholder) || "[[FILL IN";
  let rest = text;
  for (let i = rest.indexOf(ph); i !== -1; i = rest.indexOf(ph)) {
    const close = rest.indexOf("]]", i);
    const end = close === -1 ? rest.length : close + 2;
    if (i > 0) node.appendChild(document.createTextNode(rest.slice(0, i)));
    node.appendChild(el("mark", "tm-ph", rest.slice(i, end)));
    rest = rest.slice(end);
  }
  if (rest) node.appendChild(document.createTextNode(rest));
}

// "## " heading, "- " bullet, a blank line ends a paragraph (terms.py).
function tmRenderText(target, text) {
  target.textContent = "";
  let para = null;
  let list = null;
  for (const raw of String(text || "").replace(/\r\n/g, "\n").split("\n")) {
    const line = raw.replace(/\s+$/, "");
    if (!line.trim()) { para = null; list = null; continue; }
    if (line.startsWith("## ")) {
      para = null; list = null;
      const h = el("h3");
      tmInline(h, line.slice(3).trim());
      target.appendChild(h);
    } else if (line.startsWith("- ")) {
      para = null;
      if (!list) { list = el("ul"); target.appendChild(list); }
      const li = el("li");
      tmInline(li, line.slice(2).trim());
      list.appendChild(li);
    } else {
      list = null;
      // Joined with a space, as the client pages render it.
      if (para) { para.appendChild(document.createTextNode(" ")); }
      else { para = el("p"); target.appendChild(para); }
      tmInline(para, line.trim());
    }
  }
  if (!target.firstChild) target.appendChild(el("p", "muted", "Nothing to show yet."));
}

/* --------------------------------------------------------------- editor */

function tmEdit(box) {
  const s = tm.state;
  tmSignup(box, s);

  // What publishing does, and who still has to accept.
  const about = el("div", "pf-note tm-about");
  about.appendChild(el("p", null, "Published versions can't be edited or deleted: to change the text, " +
    "publish a new version. Publishing a version everyone must accept makes every client accept it at their " +
    "next visit before their dashboard opens again; a version published without that (a typo fix) is shown " +
    "but asks nobody again."));
  if (s.current) {
    about.appendChild(el("p", null, `Shown now: v${s.current.version} "${s.current.title}", published ` +
      `${owTime(s.current.published_at)} by ${s.current.published_by}.`));
  } else {
    about.appendChild(el("p", null, "Nothing is published yet, so client sign-up stays closed. The editor " +
      "holds the starter template: a strict default, not legal advice. Fill in every [[FILL IN …]] part first."));
  }
  if (s.required_version) {
    const n = s.outstanding;
    about.appendChild(el("p", n ? "tm-outstanding" : null,
      `Outstanding: ${n} client${n === 1 ? " has" : "s have"} not accepted v${s.required_version} yet.`));
  }
  box.appendChild(about);

  tmEditor(box, s);
}

function tmSignup(box, s) {
  const sec = el("div", "pf-section");
  const title = el("div", "title");
  title.appendChild(el("span", null, "Client sign-up"));
  const badges = el("span", "ow-badges");
  badges.appendChild(s.signup.open ? el("span", "badge link", "open") : el("span", "badge paused", "closed"));
  title.appendChild(badges);
  sec.appendChild(title);

  const urls = tmPublicUrls();
  let text;
  if (s.signup.open) {
    text = "Anyone can ask for a client login at " + urls.signup + ". Each request waits under Client logins → " +
      "Waiting until you or a manager approves it; nobody sees a business before you link one.";
  } else if (s.signup.enabled) {
    text = "Switched on, but closed until the first version of the terms is published.";
  } else {
    text = "Closed: only logins you create under Client logins can sign in.";
  }
  sec.appendChild(el("p", "pf-note ow-meta", text));

  const links = el("div", "tm-urls");
  for (const [label, url] of [["Sign-up and client dashboard", urls.signup], ["Public terms", urls.terms]]) {
    const row = el("div");
    const a = el("a", null, url);
    a.href = url;
    a.target = "_blank";
    a.rel = "noopener";
    row.append(el("span", "muted", label + ": "), a);
    links.appendChild(row);
  }
  sec.appendChild(links);

  const actions = el("div", "pf-actions");
  const toggle = el("button", "btn small" + (s.signup.enabled ? " warn" : " primary"),
    s.signup.enabled ? "Close sign-up" : "Open sign-up");
  toggle.addEventListener("click", async () => {
    toggle.disabled = true;
    try {
      tm.state = await api("PUT", "/api/platform/signup", { enabled: !s.signup.enabled });
      toast(tm.state.signup.enabled ? "Sign-up opened." : "Sign-up closed.", "info");
      tmDraw();
    } catch (err) {
      toast(err.message);
      toggle.disabled = false;
    }
  });
  actions.appendChild(toggle);
  sec.appendChild(actions);
  box.appendChild(sec);
}

function tmEditor(box, s) {
  const d = tm.draft;
  const ph = s.placeholder || "[[FILL IN";
  const first = !s.current;
  const sec = el("div", "pf-section tm-editor");
  sec.appendChild(el("div", "title", first ? "Write the first version" : "Publish a new version"));

  const titleWrap = el("div", "field tm-field");
  const title = el("input");
  title.type = "text";
  title.maxLength = 200;
  title.value = d.title;
  title.addEventListener("input", () => { d.title = title.value; });
  titleWrap.append(el("label", null, "Title"), title);
  sec.appendChild(titleWrap);

  // Toolbar: placeholders left, template and preview.
  const bar = el("div", "pf-actions tm-bar");
  const counter = el("span", "tm-counter");
  const next = el("button", "btn small", "Next placeholder");
  next.type = "button";
  const starter = el("button", "btn small", "Load starter template");
  starter.type = "button";
  const preview = el("button", "btn small", tm.preview ? "Hide preview" : "Preview");
  preview.type = "button";
  bar.append(counter, next, el("span", "spacer"), starter);
  if (s.current) {
    const reset = el("button", "btn small", "Back to the published text");
    reset.type = "button";
    reset.addEventListener("click", () => {
      if (!confirm("Replace the editor with the text shown now? Your edits are lost.")) return;
      tmResetDraft();
      tmDraw();
    });
    bar.appendChild(reset);
  }
  bar.appendChild(preview);
  sec.appendChild(bar);

  const text = el("textarea", "mono tm-text");
  text.spellcheck = true;
  text.value = d.body;
  sec.appendChild(text);
  sec.appendChild(el("div", "pf-note tm-format",
    'Format: a line starting "## " is a heading, "- " a bullet, a blank line starts a new paragraph. ' +
    "Nothing else is interpreted."));

  const view = el("div", "tm-preview");
  view.hidden = !tm.preview;
  sec.appendChild(view);

  const refresh = () => {
    const left = tmCount(text.value, ph);
    counter.textContent = left ? `${left} ${ph} …]] part${left === 1 ? "" : "s"} left to write` : "No placeholders left";
    counter.className = "tm-counter" + (left ? " left" : " done");
    next.disabled = !left;
    if (tm.preview) tmRenderText(view, text.value);
  };
  text.addEventListener("input", () => { d.body = text.value; refresh(); });

  next.addEventListener("click", () => {
    const value = text.value;
    let i = value.indexOf(ph, text.selectionEnd);
    if (i === -1) i = value.indexOf(ph);
    if (i === -1) return;
    const close = value.indexOf("]]", i);
    const end = close === -1 ? i + ph.length : close + 2;
    // Rough scroll first (lines × line height); browsers that scroll to the
    // selection on their own then correct it.
    const lineHeight = parseFloat(getComputedStyle(text).lineHeight) || 18;
    const line = value.slice(0, i).split("\n").length - 1;
    text.scrollTop = Math.max(0, line * lineHeight - text.clientHeight / 3);
    text.focus();
    text.setSelectionRange(i, end);
  });

  starter.addEventListener("click", () => {
    if (text.value.trim() && text.value !== s.starter.body &&
        !confirm("Replace the editor with the starter template? Your edits are lost.")) return;
    d.title = s.starter.title;
    d.body = s.starter.body;
    title.value = d.title;
    text.value = d.body;
    refresh();
  });

  preview.addEventListener("click", () => {
    tm.preview = !tm.preview;
    view.hidden = !tm.preview;
    preview.textContent = tm.preview ? "Hide preview" : "Preview";
    refresh();
    if (tm.preview) view.scrollIntoView({ block: "nearest" });
  });

  // What changed, and whether everyone has to accept again.
  const noteWrap = el("div", "field tm-field");
  const note = el("input");
  note.type = "text";
  note.maxLength = 1000;
  note.value = d.note;
  note.placeholder = first ? "Optional, e.g. First version" : "e.g. Section 9: new prices from 1 March";
  note.addEventListener("input", () => { d.note = note.value; });
  noteWrap.append(el("label", null, "What changed (shown in the history and the audit log)"), note);
  sec.appendChild(noteWrap);

  const requires = el("input");
  requires.type = "checkbox";
  requires.id = "tm-requires";
  if (first) {
    sec.appendChild(el("p", "pf-note", "The first version is always one every client must accept."));
  } else {
    requires.checked = d.requires;
    requires.addEventListener("change", () => { d.requires = requires.checked; });
    const check = el("div", "field check");
    const label = el("label", null, "Everyone must accept this version again (untick only for a typo fix)");
    label.htmlFor = "tm-requires";
    check.append(requires, label);
    sec.appendChild(check);
  }

  const errBox = el("div", "pf-errors tm-error");
  errBox.hidden = !tm.error;
  if (tm.error) errBox.textContent = tm.error;
  sec.appendChild(errBox);

  const actions = el("div", "pf-actions");
  const publish = el("button", "btn primary", first ? "Publish the first version" : "Publish new version");
  publish.addEventListener("click", async () => {
    const mustAccept = first || requires.checked;
    if (!confirm("Publish this text? A published version can't be edited or deleted." +
      (mustAccept ? " Every client has to accept it at their next visit." : ""))) return;
    publish.disabled = true;
    try {
      const result = await api("POST", "/api/platform/terms", {
        title: title.value, body: text.value, change_note: note.value, requires_acceptance: mustAccept,
      });
      tm.state = result;
      tmResetDraft();
      tm.preview = false;
      toast(`Version ${result.current ? result.current.version : ""} published.`, "info");
      tmDraw();
    } catch (err) {
      tm.error = err.message;
      errBox.textContent = err.message;
      errBox.hidden = false;
      errBox.scrollIntoView({ block: "nearest" });
      toast(err.message);
      publish.disabled = false;
    }
  });
  actions.appendChild(publish);
  sec.appendChild(actions);

  box.appendChild(sec);
  refresh();
}

/* -------------------------------------------------------------- history */

function tmHistory(box) {
  const s = tm.state;
  box.appendChild(el("p", "pf-note", "Every published version, newest first. They stay as they were " +
    "published; every acceptance is kept with its time and address (see Client logins → Terms history)."));
  if (!s.history.length) {
    box.appendChild(el("div", "pf-note", "Nothing published yet."));
    return;
  }
  for (const v of s.history) {
    const card = el("div", "pf-section tm-version");
    const title = el("div", "title");
    title.append(el("span", null, `v${v.version}`), el("span", "tm-v-title", v.title));
    const badges = el("span", "ow-badges");
    if (s.current && s.current.version === v.version) badges.appendChild(el("span", "badge link", "shown now"));
    if (s.required_version === v.version) badges.appendChild(el("span", "badge takeover", "required now"));
    badges.appendChild(el("span", "badge", v.requires_acceptance ? "must accept" : "no re-acceptance"));
    title.appendChild(badges);
    card.appendChild(title);
    card.appendChild(el("div", "pf-note ow-meta",
      `Published ${owTime(v.published_at)} by ${v.published_by} · accepted by ${v.accepted_by} ` +
      `login${v.accepted_by === 1 ? "" : "s"}`));
    if (v.change_note) card.appendChild(el("div", "tm-change", "What changed: " + v.change_note));

    const details = el("details", "tm-details");
    details.appendChild(el("summary", null, "Show the text"));
    const body = el("div", "tm-preview");
    details.appendChild(body);
    details.addEventListener("toggle", () => { if (details.open && !body.firstChild) tmRenderText(body, v.body); });
    card.appendChild(details);

    const actions = el("div", "pf-actions");
    const reuse = el("button", "btn small", "Start a new version from this text");
    reuse.addEventListener("click", () => {
      if (!confirm("Replace the editor with this version's text? Your edits are lost.")) return;
      tm.draft = { title: v.title, body: v.body, note: "", requires: true };
      tm.error = null;
      tm.tab = "edit";
      tmDraw();
    });
    actions.appendChild(reuse);
    card.appendChild(actions);
    box.appendChild(card);
  }
}

$("open-terms").addEventListener("click", () => openTerms());
$("tm-close").addEventListener("click", closeTerms);
$("terms").addEventListener("click", (ev) => { if (ev.target === $("terms")) closeTerms(); });
