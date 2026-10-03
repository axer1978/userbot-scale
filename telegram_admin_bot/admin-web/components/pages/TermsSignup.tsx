"use client";

// The terms of service every client login accepts, and the switch that lets
// new clients ask for a login themselves at /owner/ (terms_admin_api.py,
// terms.py). A published version is permanent: the only way to change the
// text is to publish a new version. The text format is tiny on purpose
// ("## " heading, "- " bullet, blank line = new paragraph) so every page
// renders it as plain text, never as HTML.

import { useCallback, useRef, useState } from "react";
import { flushSync } from "react-dom";
import { useDialogs, useToast } from "@/components/feedback";
import { PageShell, Tabs } from "@/components/ui";
import { api, errorText } from "@/lib/api";
import { cx, fmtDateTime } from "@/lib/format";
import type { TermsState, TermsVersion } from "@/lib/types";
import { useLoader } from "@/lib/useLoader";
import { useOrigin } from "@/lib/useOrigin";
import "@/app/terms-managers.css";

export type TermsTab = "edit" | "history";

const TABS: [TermsTab, string][] = [["edit", "Terms & sign-up"], ["history", "History"]];
const DEFAULT_PLACEHOLDER = "[[FILL IN";

type Draft = { title: string; body: string; note: string; requires: boolean };

// The editor starts from the version shown now, or from the starter text
// before anything is published.
function draftFrom(s: TermsState): Draft {
  const from = s.current ?? s.starter;
  return { title: from.title, body: from.body, note: "", requires: true };
}

function placeholderOf(s: TermsState): string {
  return s.placeholder || DEFAULT_PLACEHOLDER;
}

function count(text: string, needle: string): number {
  if (!needle) return 0;
  let n = 0;
  for (let i = text.indexOf(needle); i !== -1; i = text.indexOf(needle, i + needle.length)) n++;
  return n;
}

/* ------------------------------------------------------------ rendering */

// Text with every [[FILL IN …]] marked.
function inline(text: string, ph: string, key: string): React.ReactNode[] {
  const out: React.ReactNode[] = [];
  let rest = text;
  let n = 0;
  for (let i = rest.indexOf(ph); i !== -1; i = rest.indexOf(ph)) {
    const close = rest.indexOf("]]", i);
    const end = close === -1 ? rest.length : close + 2;
    if (i > 0) out.push(rest.slice(0, i));
    out.push(<mark key={`${key}-${n++}`} className="tm-ph">{rest.slice(i, end)}</mark>);
    rest = rest.slice(end);
  }
  if (rest) out.push(rest);
  return out;
}

type Block = { kind: "h" | "p" | "ul"; lines: string[] };

// "## " heading, "- " bullet, a blank line ends a paragraph (terms.py).
function TermsText({ text, ph }: { text: string; ph: string }) {
  const blocks: Block[] = [];
  let para: Block | null = null;
  let list: Block | null = null;
  for (const raw of String(text || "").replace(/\r\n/g, "\n").split("\n")) {
    const line = raw.replace(/\s+$/, "");
    if (!line.trim()) { para = null; list = null; continue; }
    if (line.startsWith("## ")) {
      para = null; list = null;
      blocks.push({ kind: "h", lines: [line.slice(3).trim()] });
    } else if (line.startsWith("- ")) {
      para = null;
      if (!list) { list = { kind: "ul", lines: [] }; blocks.push(list); }
      list.lines.push(line.slice(2).trim());
    } else {
      list = null;
      if (!para) { para = { kind: "p", lines: [] }; blocks.push(para); }
      para.lines.push(line.trim());
    }
  }
  if (!blocks.length) return <p className="muted">Nothing to show yet.</p>;
  return blocks.map((b, i) => {
    if (b.kind === "h") return <h3 key={i}>{inline(b.lines[0], ph, `${i}`)}</h3>;
    if (b.kind === "ul") return <ul key={i}>{b.lines.map((l, j) => <li key={j}>{inline(l, ph, `${i}-${j}`)}</li>)}</ul>;
    // Lines joined with a space, as the client pages render them.
    return <p key={i}>{b.lines.flatMap((l, j) => [...(j ? [" "] : []), ...inline(l, ph, `${i}-${j}`)])}</p>;
  });
}

/* -------------------------------------------------------------- sign-up */

function Signup({ s, onState }: { s: TermsState; onState: (next: TermsState) => void }) {
  const toast = useToast();
  const origin = useOrigin();
  const [busy, setBusy] = useState(false);
  const signupUrl = origin + "/owner/";
  const termsUrl = origin + "/terms/";

  let text: string;
  if (s.signup.open) {
    text = "Anyone can ask for a client login at " + signupUrl + ". Each request waits under Client logins → " +
      "Waiting until you or a manager approves it; nobody sees a business before you link one.";
  } else if (s.signup.enabled) {
    text = "Switched on, but closed until the first version of the terms is published.";
  } else {
    text = "Closed: only logins you create under Client logins can sign in.";
  }

  return (
    <div className="pf-section">
      <div className="title">
        <span>Client sign-up</span>
        <span className="ow-badges">
          {s.signup.open ? <span className="badge link">open</span> : <span className="badge paused">closed</span>}
        </span>
      </div>
      <p className="pf-note ow-meta">{text}</p>
      <div className="tm-urls">
        {([["Sign-up and client dashboard", signupUrl], ["Public terms", termsUrl]] as const).map(([label, url]) => (
          <div key={label}>
            <span className="muted">{label}: </span>
            <a href={url} target="_blank" rel="noopener">{url}</a>
          </div>
        ))}
      </div>
      <div className="pf-actions">
        <button type="button" className={cx("btn small", s.signup.enabled ? "warn" : "primary")} disabled={busy}
                onClick={async () => {
                  setBusy(true);
                  try {
                    const next = await api<TermsState>("PUT", "/api/platform/signup", { enabled: !s.signup.enabled });
                    toast(next.signup.enabled ? "Sign-up opened." : "Sign-up closed.", "info");
                    onState(next);
                  } catch (err) {
                    toast(errorText(err));
                  } finally {
                    setBusy(false);
                  }
                }}>{s.signup.enabled ? "Close sign-up" : "Open sign-up"}</button>
      </div>
    </div>
  );
}

/* --------------------------------------------------------------- editor */

function About({ s }: { s: TermsState }) {
  const n = s.outstanding;
  return (
    <div className="pf-note tm-about">
      <p>Published versions can&apos;t be edited or deleted: to change the text, publish a new version. Publishing a
        version everyone must accept makes every client accept it at their next visit before their dashboard opens
        again; a version published without that (a typo fix) is shown but asks nobody again.</p>
      {s.current ? (
        <p>Shown now: v{s.current.version} &quot;{s.current.title}&quot;, published{" "}
          {fmtDateTime(s.current.published_at, "never")} by {s.current.published_by}.</p>
      ) : (
        <p>Nothing is published yet, so client sign-up stays closed. The editor holds the starter template: a strict
          default, not legal advice. Fill in every [[FILL IN …]] part first.</p>
      )}
      {s.required_version ? (
        <p className={n ? "tm-outstanding" : undefined}>
          Outstanding: {n} client{n === 1 ? " has" : "s have"} not accepted v{s.required_version} yet.</p>
      ) : null}
    </div>
  );
}

function Editor({ s, draft, onDraft, onReset, preview, onPreview, error, onError, onPublished }: {
  s: TermsState;
  draft: Draft;
  onDraft: (patch: Partial<Draft>) => void;
  onReset: () => void;
  preview: boolean;
  onPreview: (on: boolean) => void;
  error: string | null;
  onError: (message: string) => void;
  onPublished: (next: TermsState) => void;
}) {
  const toast = useToast();
  const { confirm } = useDialogs();
  const [busy, setBusy] = useState(false);
  const text = useRef<HTMLTextAreaElement>(null);
  const view = useRef<HTMLDivElement>(null);
  const errBox = useRef<HTMLDivElement>(null);
  const ph = placeholderOf(s);
  const first = !s.current;
  const left = count(draft.body, ph);

  const nextPlaceholder = () => {
    const area = text.current;
    if (!area) return;
    const value = area.value;
    let i = value.indexOf(ph, area.selectionEnd);
    if (i === -1) i = value.indexOf(ph);
    if (i === -1) return;
    const close = value.indexOf("]]", i);
    const end = close === -1 ? i + ph.length : close + 2;
    // Rough scroll first (lines × line height); browsers that scroll to the
    // selection on their own then correct it.
    const lineHeight = parseFloat(getComputedStyle(area).lineHeight) || 18;
    const line = value.slice(0, i).split("\n").length - 1;
    area.scrollTop = Math.max(0, line * lineHeight - area.clientHeight / 3);
    area.focus();
    area.setSelectionRange(i, end);
  };

  const loadStarter = async () => {
    if (draft.body.trim() && draft.body !== s.starter.body &&
        !(await confirm("Replace the editor with the starter template? Your edits are lost."))) return;
    onDraft({ title: s.starter.title, body: s.starter.body });
  };

  const togglePreview = () => {
    const on = !preview;
    // Rendered first, so there is something to scroll to.
    flushSync(() => onPreview(on));
    if (on) view.current?.scrollIntoView({ block: "nearest" });
  };

  const publish = async () => {
    const mustAccept = first || draft.requires;
    if (!(await confirm("Publish this text? A published version can't be edited or deleted." +
      (mustAccept ? " Every client has to accept it at their next visit." : "")))) return;
    setBusy(true);
    try {
      const result = await api<TermsState>("POST", "/api/platform/terms", {
        title: draft.title, body: draft.body, change_note: draft.note, requires_acceptance: mustAccept,
      });
      toast(`Version ${result.current ? result.current.version : ""} published.`, "info");
      onPublished(result);
    } catch (err) {
      const message = errorText(err);
      flushSync(() => onError(message));
      errBox.current?.scrollIntoView({ block: "nearest" });
      toast(message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="pf-section tm-editor">
      <div className="title">{first ? "Write the first version" : "Publish a new version"}</div>

      <div className="field tm-field">
        <label htmlFor="tm-title">Title</label>
        <input id="tm-title" type="text" maxLength={200} value={draft.title}
               onChange={(ev) => onDraft({ title: ev.target.value })} />
      </div>

      {/* Toolbar: placeholders left, template and preview. */}
      <div className="pf-actions tm-bar">
        <span className={cx("tm-counter", left ? "left" : "done")}>
          {left ? `${left} ${ph} …]] part${left === 1 ? "" : "s"} left to write` : "No placeholders left"}
        </span>
        <button type="button" className="btn small" disabled={!left} onClick={nextPlaceholder}>Next placeholder</button>
        <span className="spacer" />
        <button type="button" className="btn small" onClick={loadStarter}>Load starter template</button>
        {s.current && (
          <button type="button" className="btn small" onClick={async () => {
            if (!(await confirm("Replace the editor with the text shown now? Your edits are lost."))) return;
            onReset();
          }}>Back to the published text</button>
        )}
        <button type="button" className="btn small" onClick={togglePreview}>{preview ? "Hide preview" : "Preview"}</button>
      </div>

      <textarea ref={text} className="mono tm-text" spellCheck value={draft.body}
                onChange={(ev) => onDraft({ body: ev.target.value })} />
      <div className="pf-note tm-format">
        Format: a line starting &quot;## &quot; is a heading, &quot;- &quot; a bullet, a blank line starts a new
        paragraph. Nothing else is interpreted.
      </div>

      <div ref={view} className="tm-preview" hidden={!preview}>
        {preview && <TermsText text={draft.body} ph={ph} />}
      </div>

      {/* What changed, and whether everyone has to accept again. */}
      <div className="field tm-field">
        <label htmlFor="tm-note">What changed (shown in the history and the audit log)</label>
        <input id="tm-note" type="text" maxLength={1000} value={draft.note}
               placeholder={first ? "Optional, e.g. First version" : "e.g. Section 9: new prices from 1 March"}
               onChange={(ev) => onDraft({ note: ev.target.value })} />
      </div>

      {first ? (
        <p className="pf-note">The first version is always one every client must accept.</p>
      ) : (
        <div className="field check">
          <input id="tm-requires" type="checkbox" checked={draft.requires}
                 onChange={(ev) => onDraft({ requires: ev.target.checked })} />
          <label htmlFor="tm-requires">Everyone must accept this version again (untick only for a typo fix)</label>
        </div>
      )}

      <div ref={errBox} className="pf-errors tm-error" hidden={!error}>{error}</div>

      <div className="pf-actions">
        <button type="button" className="btn primary" disabled={busy} onClick={publish}>
          {first ? "Publish the first version" : "Publish new version"}</button>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------- history */

function VersionCard({ s, v, onReuse }: { s: TermsState; v: TermsVersion; onReuse: (v: TermsVersion) => void }) {
  // The text is only rendered once it is first opened.
  const [shown, setShown] = useState(false);
  return (
    <div className="pf-section tm-version">
      <div className="title">
        <span>v{v.version}</span>
        <span className="tm-v-title">{v.title}</span>
        <span className="ow-badges">
          {s.current && s.current.version === v.version && <span className="badge link">shown now</span>}
          {s.required_version === v.version && <span className="badge takeover">required now</span>}
          <span className="badge">{v.requires_acceptance ? "must accept" : "no re-acceptance"}</span>
        </span>
      </div>
      <div className="pf-note ow-meta">
        Published {fmtDateTime(v.published_at, "never")} by {v.published_by} · accepted by {v.accepted_by}{" "}
        login{v.accepted_by === 1 ? "" : "s"}
      </div>
      {v.change_note && <div className="tm-change">What changed: {v.change_note}</div>}
      <details className="tm-details" onToggle={(ev) => { if (ev.currentTarget.open) setShown(true); }}>
        <summary>Show the text</summary>
        <div className="tm-preview">{shown && <TermsText text={v.body} ph={placeholderOf(s)} />}</div>
      </details>
      <div className="pf-actions">
        <button type="button" className="btn small" onClick={() => onReuse(v)}>Start a new version from this text</button>
      </div>
    </div>
  );
}

function History({ s, onReuse }: { s: TermsState; onReuse: (v: TermsVersion) => void }) {
  return <>
    <p className="pf-note">Every published version, newest first. They stay as they were published; every
      acceptance is kept with its time and address (see Client logins → Terms history).</p>
    {!s.history.length ? <div className="pf-note">Nothing published yet.</div>
      : s.history.map((v) => <VersionCard key={v.version} s={s} v={v} onReuse={onReuse} />)}
  </>;
}

/* ----------------------------------------------------------------- page */

export function TermsSignup({ initialTab = "edit" }: { initialTab?: TermsTab }) {
  const { confirm } = useDialogs();
  const [tab, setTab] = useState<TermsTab>(initialTab);
  // A publish or a sign-up switch answers with the whole new state, which
  // is shown without fetching again.
  const [fresh, setFresh] = useState<TermsState | null>(null);
  // null = not edited: the editor shows draftFrom(state).
  const [edited, setEdited] = useState<Draft | null>(null);
  const [preview, setPreview] = useState(false);
  const [publishError, setPublishError] = useState<string | null>(null);

  const fetchTerms = useCallback(() => api<TermsState>("GET", "/api/platform/terms"), []);
  const { data, error } = useLoader(fetchTerms);
  const state = fresh ?? data;
  const draft = edited ?? (state ? draftFrom(state) : null);

  const resetDraft = () => { setEdited(null); setPublishError(null); };

  return (
    <PageShell title="Terms & sign-up" crumb="what clients accept, and who can ask for a login" width="w-980">
      <div className="bk-scroll">
        <Tabs tabs={TABS} value={tab} onChange={setTab} />
        {error && <div className="pf-errors">{error}</div>}
        {state && draft && tab === "edit" && <>
          <Signup s={state} onState={setFresh} />
          <About s={state} />
          <Editor s={state} draft={draft}
                  onDraft={(patch) => setEdited((cur) => ({ ...(cur ?? draftFrom(state)), ...patch }))}
                  onReset={resetDraft}
                  preview={preview} onPreview={setPreview}
                  error={publishError} onError={setPublishError}
                  onPublished={(next) => { setFresh(next); resetDraft(); setPreview(false); }} />
        </>}
        {state && tab === "history" && (
          <History s={state} onReuse={async (v) => {
            if (!(await confirm("Replace the editor with this version's text? Your edits are lost."))) return;
            setEdited({ title: v.title, body: v.body, note: "", requires: true });
            setPublishError(null);
            setTab("edit");
          }} />
        )}
      </div>
    </PageShell>
  );
}
