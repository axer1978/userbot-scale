"use client";

// How the bot's messages to the booker (the person who answers booking
// requests) look: her language and the account's own wording of the main
// messages, with a live preview for a made-up booking. The same editor in
// the admin panel (Clients › a client › Booker messages) and on the owner's
// pages (Booking messages); the host hands it the API calls and its own
// button classes.

import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { latestDebounced, ownTexts, splitProblems, type FieldProblem } from "@/lib/bookerMessages";
import { cx } from "@/lib/format";
import type {
  BookerLanguage, BookerMessagesBody, BookerMessagesView, BookerPreview, BookerTemplate,
} from "@/lib/types";
import st from "./BookerMessages.module.css";

export type BookerMessagesClient = {
  load: () => Promise<BookerMessagesView>;
  preview: (body: { language: BookerLanguage; messages: Record<string, string> }) => Promise<BookerPreview>;
  save: (body: BookerMessagesBody) => Promise<BookerMessagesView>;
};

/** The host's button classes, so the editor looks like the page it is on. */
export type BookerUi = { btn: string; primary: string; small: string };

type Edit = {
  view: BookerMessagesView;
  language: BookerLanguage;
  texts: Record<string, string>;
  shown: BookerPreview;
};

type Outcome = { kind: "saved" | "conflict" | "error"; text: string; list?: string[] };

const PREVIEW_DELAY = 400;

function errorText(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

function fromView(view: BookerMessagesView): Edit {
  const texts: Record<string, string> = {};
  for (const t of view.templates) texts[t.key] = view.messages[t.key] ?? "";
  return { view, language: view.language, texts, shown: { preview: view.preview, problems: view.problems } };
}

function rowsFor(text: string): number {
  return Math.min(14, Math.max(3, text.split("\n").length + 1));
}

function keptNote(kept: string[]): string {
  if (!kept.length) return "";
  const names = kept.map((k) => `{${k}}`);
  const list = names.length === 1 ? names[0] : `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
  return `${list} ${names.length === 1 ? "is" : "are"} added back automatically if your text leaves ${names.length === 1 ? "it" : "them"} out.`;
}

export function BookerMessagesEditor({ client, canSave, ui, askReason = false, readOnlyNote, onError, onDirtyChange }: {
  /** Memoise it: a new one loads the page again. */
  client: BookerMessagesClient;
  canSave: boolean;
  ui: BookerUi;
  /** Show a "reason" field (goes into the audit log). */
  askReason?: boolean;
  readOnlyNote?: string;
  /** Errors the host deals with itself (signed out, ...): returns true when it did. */
  onError?: (err: unknown) => boolean;
  /** Told when there are (no longer) unsaved changes, to warn before moving away. */
  onDirtyChange?: (dirty: boolean) => void;
}) {
  const uid = useId();
  const [edit, setEdit] = useState<Edit | null>(null);
  const [loadError, setLoadError] = useState("");
  const [previewing, setPreviewing] = useState(false);
  const [previewError, setPreviewError] = useState("");
  const [saveProblems, setSaveProblems] = useState<Record<string, string[]>>({});
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [saving, setSaving] = useState(false);
  const [reason, setReason] = useState("");
  const areas = useRef<Record<string, HTMLTextAreaElement | null>>({});
  // Textareas whose cursor the user has placed: a blank goes there, else at the end.
  const placed = useRef<Set<string>>(new Set());

  const handled = useCallback((err: unknown) => !!onError?.(err), [onError]);

  const previewer = useMemo(() => latestDebounced(
    (body: { language: BookerLanguage; messages: Record<string, string> }) => client.preview(body),
    PREVIEW_DELAY,
    {
      onStart: () => setPreviewing(true),
      onResult: (shown) => {
        setEdit((e) => e && { ...e, shown });
        setPreviewing(false);
        setPreviewError("");
      },
      onError: (err) => {
        setPreviewing(false);
        if (!handled(err)) setPreviewError("The preview could not be updated: " + errorText(err));
      },
    },
  ), [client, handled]);

  useEffect(() => () => previewer.cancel(), [previewer]);

  const show = useCallback((view: BookerMessagesView) => {
    setEdit(fromView(view));
    setSaveProblems({});
    setPreviewing(false);
    setPreviewError("");
    setLoadError("");
  }, []);

  useEffect(() => {
    let cancelled = false;
    client.load().then(
      (view) => { if (!cancelled) show(view); },
      (err) => { if (!cancelled && !handled(err)) setLoadError(errorText(err)); },
    );
    return () => { cancelled = true; };
  }, [client, handled, show]);

  const reload = async () => {
    previewer.cancel();
    setOutcome(null);
    try { show(await client.load()); } catch (err) { if (!handled(err)) setLoadError(errorText(err)); }
  };

  const dirty = useMemo(() => {
    if (!edit) return false;
    if (edit.language !== edit.view.language) return true;
    return edit.view.templates.some((t) => (edit.texts[t.key] ?? "") !== (edit.view.messages[t.key] ?? ""));
  }, [edit]);

  useEffect(() => { onDirtyChange?.(dirty); }, [dirty, onDirtyChange]);
  useEffect(() => () => onDirtyChange?.(false), [onDirtyChange]);

  // Closing the tab or reloading the page with unsaved changes asks first.
  useEffect(() => {
    if (!dirty) return;
    const warn = (ev: BeforeUnloadEvent) => { ev.preventDefault(); ev.returnValue = ""; };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  if (loadError && !edit) {
    return (
      <div className={st.wrap}>
        <div className={cx(st.outcome, st.err)} role="alert">Could not load the messages: {loadError}</div>
        <div><button type="button" className={ui.btn} onClick={() => void reload()}>Try again</button></div>
      </div>
    );
  }
  if (!edit) return <div className={st.loading}>Loading…</div>;

  const { view, language, texts, shown } = edit;
  const readOnly = !canSave;
  const keys = view.templates.map((t) => t.key);

  const change = (nextLanguage: BookerLanguage, nextTexts: Record<string, string>) => {
    setEdit((e) => e && { ...e, language: nextLanguage, texts: nextTexts });
    setOutcome((o) => (o?.kind === "saved" ? null : o));
    previewer.schedule({ language: nextLanguage, messages: ownTexts(nextTexts) });
  };

  const setText = (key: string, value: string) => {
    change(language, { ...texts, [key]: value });
    setSaveProblems((p) => {
      if (!(key in p)) return p;
      const next = { ...p };
      delete next[key];
      return next;
    });
  };

  const insertBlank = (key: string, name: string) => {
    const el = areas.current[key];
    const text = texts[key] ?? "";
    const token = `{${name}}`;
    const usable = el && placed.current.has(key);
    const start = usable ? el.selectionStart : text.length;
    const end = usable ? el.selectionEnd : text.length;
    setText(key, text.slice(0, start) + token + text.slice(end));
    const caret = start + token.length;
    placed.current.add(key);
    requestAnimationFrame(() => {
      const area = areas.current[key];
      if (!area) return;
      area.focus();
      area.setSelectionRange(caret, caret);
    });
  };

  const save = async () => {
    setSaving(true);
    setOutcome(null);
    try {
      const body: BookerMessagesBody = { language, messages: ownTexts(texts), expected_revision: view.revision };
      if (askReason && reason.trim()) body.reason = reason.trim();
      const saved = await client.save(body);
      previewer.cancel();
      show(saved);
      setReason("");
      setOutcome({ kind: "saved", text: "Saved — the bot uses it from its next message." });
    } catch (err) {
      if (handled(err)) return;
      const e = err as { status?: number; errors?: FieldProblem[] | null };
      if (e.status === 409) {
        setOutcome({ kind: "conflict", text: "Someone else changed this account's settings — reload to see them." });
      } else if (e.status === 422 && e.errors?.length) {
        const split = splitProblems(e.errors, keys);
        setSaveProblems(split.byKey);
        const labels = view.templates.filter((t) => split.byKey[t.key]).map((t) => t.label);
        setOutcome({
          kind: "error",
          text: labels.length
            ? `Not saved. Fix the ${labels.length === 1 ? "message" : "messages"} marked in red: ${labels.join(", ")}.`
            : "Not saved.",
          list: split.other,
        });
      } else {
        setOutcome({ kind: "error", text: "Not saved: " + errorText(err) });
      }
    } finally {
      setSaving(false);
    }
  };

  const langLabel = view.languages.find((l) => l.key === language)?.label ?? language;

  return (
    <div className={st.wrap}>
      <div className={st.top}>
        <label className={st.langField} htmlFor={`${uid}-lang`}>
          Her language
          <select id={`${uid}-lang`} value={language} disabled={readOnly}
                  onChange={(ev) => change(ev.target.value as BookerLanguage, texts)}>
            {view.languages.map((l) => <option key={l.key} value={l.key}>{l.label}</option>)}
          </select>
        </label>
        <p className={st.note}>Everything the bot sends the booker is in this language. Your own texts below are
          sent as you write them; only the defaults change with the language.</p>
      </div>
      <p className={st.note}>Leave a message empty to use the default. Blanks in curly braces, like{" "}
        <code>{"{customer}"}</code>, are filled in for each booking — click one under a message to put it at the cursor.
        A line whose blanks are all empty is left out. To write a brace itself, type <code>{"{{"}</code> or{" "}
        <code>{"}}"}</code>.</p>
      {readOnly && (
        <div className={st.readOnly}>
          {readOnlyNote || "You can look at these but not change them: saving needs the “Messages to the booker” permission."}
        </div>
      )}
      {previewError && <div className={cx(st.outcome, st.err)} role="alert">{previewError}</div>}

      {view.templates.map((t) => (
        <TemplateCard key={t.key} t={t} uid={uid} text={texts[t.key] ?? ""} fallback={t.defaults[language] ?? t.defaults.en}
                      langLabel={langLabel} maxLength={view.max_length} readOnly={readOnly} ui={ui}
                      problems={[...new Set([...(shown.problems[t.key] || []), ...(saveProblems[t.key] || [])])]}
                      preview={shown.preview[t.key] ?? ""} previewing={previewing}
                      areaRef={(el) => { areas.current[t.key] = el; }}
                      onPlace={() => placed.current.add(t.key)}
                      onText={(value) => setText(t.key, value)}
                      onBlank={(name) => insertBlank(t.key, name)} />
      ))}

      {!readOnly && (
        <div className={st.bar}>
          {outcome && (
            <div className={cx(st.outcome, outcome.kind === "saved" ? st.ok : st.err)}
                 role={outcome.kind === "saved" ? "status" : "alert"}>
              <div className={st.outcomeRow}>
                <span>{outcome.text}</span>
                {outcome.kind === "conflict" && (
                  <button type="button" className={ui.small} onClick={() => void reload()}>Reload</button>
                )}
              </div>
              {!!outcome.list?.length && <ul>{outcome.list.map((m, i) => <li key={i}>{m}</li>)}</ul>}
            </div>
          )}
          <div className={st.barRow}>
            {askReason && (
              <>
                <label className={st.srOnly} htmlFor={`${uid}-reason`}>Reason for the change</label>
                <input id={`${uid}-reason`} placeholder="Reason for the change (goes into the audit log)"
                       value={reason} maxLength={500} onChange={(ev) => setReason(ev.target.value)} />
              </>
            )}
            <span className={st.dirty} aria-live="polite">{dirty ? "Unsaved changes" : "No changes"}</span>
            <button type="button" className={ui.primary} disabled={!dirty || saving} onClick={() => void save()}>
              {saving ? "Saving…" : "Save"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function TemplateCard({
  t, uid, text, fallback, langLabel, maxLength, readOnly, ui, problems, preview, previewing,
  areaRef, onPlace, onText, onBlank,
}: {
  t: BookerTemplate;
  uid: string;
  text: string;
  fallback: string;
  langLabel: string;
  maxLength: number;
  readOnly: boolean;
  ui: BookerUi;
  problems: string[];
  preview: string;
  previewing: boolean;
  areaRef: (el: HTMLTextAreaElement | null) => void;
  onPlace: () => void;
  onText: (value: string) => void;
  onBlank: (name: string) => void;
}) {
  const id = `${uid}-${t.key}`;
  const own = text.trim() !== "";
  const over = text.length > maxLength;
  const kept = keptNote(t.kept);
  const describedBy = [`${id}-about`, kept && `${id}-kept`, problems.length && `${id}-problems`].filter(Boolean).join(" ");

  return (
    <section className={cx(st.card, own && st.own, problems.length > 0 && st.bad)} aria-labelledby={`${id}-title`}>
      <div className={st.cardHead}>
        <h3 id={`${id}-title`} className={st.title}>{t.label}</h3>
        <span className={cx(st.tag, own && st.tagOwn)}>{own ? "Own text" : `Default (${langLabel})`}</span>
      </div>
      <p id={`${id}-about`} className={st.about}>{t.about}</p>

      <textarea id={id} ref={areaRef} className={st.area} rows={rowsFor(text || fallback)} value={text}
                placeholder={fallback} readOnly={readOnly} spellCheck
                aria-labelledby={`${id}-title`} aria-describedby={describedBy} aria-invalid={problems.length > 0}
                onSelect={onPlace} onChange={(ev) => onText(ev.target.value)} />

      {!readOnly && (
        <div className={st.chips} role="group" aria-label={`Blanks for “${t.label}”: click to insert at the cursor`}>
          {t.blanks.map((b) => (
            <button key={b.name} type="button" className={st.chip} title={b.about}
                    aria-label={`Insert {${b.name}}: ${b.about}`} onClick={() => onBlank(b.name)}>
              {`{${b.name}}`}
            </button>
          ))}
        </div>
      )}

      <div className={st.row}>
        {!readOnly && !own && (
          <button type="button" className={ui.small} onClick={() => onText(fallback)}>Use the default as a start</button>
        )}
        {!readOnly && (
          <button type="button" className={ui.small} disabled={!text} onClick={() => onText("")}
                  title="Clear your text: the default is sent">Back to default</button>
        )}
        <span className={cx(st.count, over && st.over)}>
          {text.length} / {maxLength}{over ? " — too long" : ""}
        </span>
      </div>
      {kept && <p id={`${id}-kept`} className={st.kept}>{kept}</p>}

      {problems.length > 0 && (
        <ul id={`${id}-problems`} className={st.problems}>
          {problems.map((p, i) => <li key={i}>{p}</li>)}
          <li>Until this is fixed the bot sends the default, as the preview shows.</li>
        </ul>
      )}

      <div id={`${id}-preview-label`} className={st.previewLabel}>How it will look (made-up booking)</div>
      <pre className={cx(st.preview, previewing && st.stale)} aria-labelledby={`${id}-preview-label`}
           aria-busy={previewing}>
        {preview || <span className={st.empty}>(nothing would be sent)</span>}
      </pre>
    </section>
  );
}
