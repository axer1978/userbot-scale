"use client";

// Flag a finished chat for finetuning (finetune_api.py): was it handled
// well or badly, what kind of client was it, how did it end, how should the
// model read it, and corrections on single messages. Only that judgement is
// saved; a finetune run from flagged chats (Finetune → New run) reads the
// chat itself, with the client's personal data masked. Admin only.

import { useCallback, useEffect, useRef, useState } from "react";
import { useDialogs, useToast } from "@/components/feedback";
import { Overlay } from "@/components/ui";
import { api, errorText } from "@/lib/api";
import { cx, fmtTime } from "@/lib/format";
import type { ChatExampleView, ChatOutcome, ClientKind, ExampleVerdict } from "@/lib/types";
import "@/app/finetune.css";

export const VERDICTS: [ExampleVerdict, string, string][] = [
  ["good", "Good example", "handled the way the business wants: learn from it"],
  ["bad", "Bad example", "replies not to repeat: learn what went wrong"],
  ["plain", "Ordinary", "neither: just how chats go"],
];

export const CLIENT_KINDS: [ClientKind, string, string][] = [
  ["", "Not said", "let the model judge"],
  ["serious", "Serious client", "says what he wants, names his own time, accepts the price, follows instructions"],
  ["time_waster", "Time-waster", "small talk, keeps asking for photos or video, never names a time, haggles, wants the address first"],
  ["safety_risk", "Safety risk", "refuses checks, aggressive, drunk or high, anything about minors, drugs or other people"],
  ["regular", "Regular", "has been before"],
];

export const OUTCOMES: [ChatOutcome, string][] = [
  ["", "Not said"], ["came", "Booked and came"], ["no_show", "Booked, didn't come"],
  ["cancelled", "Booked, then cancelled"], ["never_booked", "Never booked"], ["blocked", "Stopped answering / blocked"],
];

export function verdictLabel(verdict?: string): string {
  return verdict === "good" ? "good example" : verdict === "bad" ? "bad example" : "flagged";
}

const WHO = { client: "them", business: "me", event: "booking" } as const;

type Props = {
  sessionId: string;
  chatId: number;
  name: string;
  onClose: () => void;
  /** After a save or a removal: the chat's verdict now, or null. */
  onChanged: (verdict: ExampleVerdict | null) => void;
};

export function FinetuneExampleDialog({ sessionId, chatId, name, onClose, onChanged }: Props) {
  const toast = useToast();
  const { confirm } = useDialogs();
  const path = `/api/finetune/sessions/${encodeURIComponent(sessionId)}/chats/${chatId}/example`;
  const [view, setView] = useState<ChatExampleView | null>(null);
  const [verdict, setVerdict] = useState<ExampleVerdict | "">("");
  const [clientKind, setClientKind] = useState<ClientKind>("");
  const [outcome, setOutcome] = useState<ChatOutcome>("");
  const [note, setNote] = useState("");
  const [corrections, setCorrections] = useState<Record<string, string>>({});
  const [editing, setEditing] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);

  const show = useCallback((data: ChatExampleView) => {
    setView(data);
    const e = data.example;
    setVerdict(e?.verdict ?? "");
    setClientKind(e?.client_kind ?? "");
    setOutcome(e?.outcome ?? "");
    setNote(e?.note ?? "");
    setCorrections(e?.corrections ?? {});
    setEditing(null);
  }, []);

  // Loaded once per chat; the callbacks may change identity on every render.
  const closeRef = useRef(onClose);
  const toastRef = useRef(toast);
  useEffect(() => { closeRef.current = onClose; toastRef.current = toast; });
  useEffect(() => {
    let cancelled = false;
    api<ChatExampleView>("GET", path)
      .then((data) => { if (!cancelled) show(data); })
      .catch((err) => { if (!cancelled) { toastRef.current(errorText(err)); closeRef.current(); } });
    return () => { cancelled = true; };
  }, [path, show]);

  const save = async () => {
    if (!verdict) { toast("Mark the chat good, bad or ordinary first."); return; }
    setBusy(true);
    try {
      const data = await api<ChatExampleView>("PUT", path, {
        verdict, client_kind: clientKind, outcome, note: note.trim(), corrections,
      });
      show(data);
      onChanged(data.example?.verdict ?? null);
      toast("Flagged. Use it under Finetune → New run → Flagged chats.", "info");
    } catch (err) { toast(errorText(err)); } finally { setBusy(false); }
  };

  const remove = async () => {
    const id = view?.example?.id;
    if (!id || !(await confirm("Remove the flag? The chat itself stays as it is."))) return;
    setBusy(true);
    try {
      await api("DELETE", `/api/finetune/examples/${id}`);
      onChanged(null);
      onClose();
    } catch (err) { toast(errorText(err)); setBusy(false); }
  };

  const setCorrection = (id: number, text: string) =>
    setCorrections((all) => {
      const next = { ...all };
      if (text) next[String(id)] = text; else delete next[String(id)];
      return next;
    });

  const kindHint = CLIENT_KINDS.find(([k]) => k === clientKind)?.[2];
  const upto = view?.example?.upto_message_id ?? Infinity;

  return (
    <Overlay onClose={onClose}>
      <div className="sheet w-860 ft-flag">
        <h2>Flag for finetuning <span className="muted">— {name}</span></h2>
        <p className="hint">Tell the model how to read this chat. Only your judgement is saved: the chat stays where it
          is, and a finetune run reads it (with names, numbers and the address masked) when you pick it under
          Finetune → New run → Flagged chats.</p>
        {!view ? <div className="empty">Loading the chat…</div> : <>
          {view.newer > 0 && (
            <div className="pf-note warn-note">{view.newer} message{view.newer === 1 ? "" : "s"} since it was flagged
              (marked new below). Saving takes them in.</div>
          )}

          <fieldset>
            <legend>How it was handled</legend>
            <div className="ft-verdicts" role="radiogroup" aria-label="How it was handled">
              {VERDICTS.map(([key, label, hint]) => (
                <label key={key} className={cx("ft-verdict", `mark-${key}`, verdict === key && "on")}>
                  <input type="radio" name="ft-verdict" checked={verdict === key} onChange={() => setVerdict(key)} />
                  <b>{label}</b><span className="muted">{hint}</span>
                </label>
              ))}
            </div>
          </fieldset>

          <div className="row">
            <div className="field">
              <label htmlFor="ft-kind">The client</label>
              <select id="ft-kind" value={clientKind} onChange={(ev) => setClientKind(ev.target.value as ClientKind)}>
                {CLIENT_KINDS.map(([key, label]) => <option key={key} value={key}>{label}</option>)}
              </select>
              {kindHint && <div className="pf-note">{kindHint}</div>}
            </div>
            <div className="field">
              <label htmlFor="ft-outcome">How it ended</label>
              <select id="ft-outcome" value={outcome} onChange={(ev) => setOutcome(ev.target.value as ChatOutcome)}>
                {OUTCOMES.map(([key, label]) => <option key={key} value={key}>{label}</option>)}
              </select>
            </div>
          </div>

          <div className="field">
            <label htmlFor="ft-note">How to read this chat <span className="muted">— what should the model learn?</span></label>
            <textarea id="ft-note" rows={4} maxLength={4000} value={note} onChange={(ev) => setNote(ev.target.value)}
                      placeholder={"e.g. Flattery and small talk from the start, asked for photos three times and a video, " +
                        "never named a time: a time-waster. After the second stall the reply should have been one " +
                        "short question for his time, then nothing."} />
          </div>

          <fieldset>
            <legend>Corrections <span className="muted">— ✎ on a message: what was wrong, or what it should have said</span></legend>
            <div className="ft-lines">
              {view.lines.map((line) => {
                const text = corrections[String(line.id)] ?? "";
                const open = editing === line.id || !!text;
                return (
                  <div key={line.id} className={cx("ft-line", `ft-${line.who}`, line.id > upto && "newer")}>
                    <div className="ft-line-head">
                      <span className="ft-who">{WHO[line.who]}{line.id > upto ? " · new" : ""}</span>
                      <span className="ft-text">{line.text}</span>
                      <span className="muted ft-time">{fmtTime(line.at)}</span>
                      {line.who !== "event" && (
                        <button type="button" className={cx("btn small", text && "ft-has")} title="Correct this message"
                                aria-label={`Correct message ${line.id}`}
                                onClick={() => setEditing(editing === line.id ? null : line.id)}>✎</button>
                      )}
                    </div>
                    {open && (
                      <textarea rows={2} maxLength={1000} value={text} autoFocus={editing === line.id && !text}
                                placeholder={line.who === "business"
                                  ? "e.g. Should have been: \"text me when you're ready to come love 😘\""
                                  : "e.g. This is where he shows he is a time-waster: asks for the address before a time."}
                                onChange={(ev) => setCorrection(line.id, ev.target.value)} />
                    )}
                  </div>
                );
              })}
              {!view.lines.length && <div className="empty">No messages in this chat yet.</div>}
            </div>
          </fieldset>

          {view.preview && (
            <details className="ft-raw"><summary className="muted">What the model reads (as saved)</summary>
              <pre className="pf-rendered">{view.preview}</pre></details>
          )}
        </>}

        <div className="sheet-actions">
          {view?.example && <button type="button" className="btn warn" disabled={busy} onClick={remove}>Remove flag</button>}
          <span className="grow" />
          <button type="button" className="btn" onClick={onClose}>Close</button>
          <button type="button" className="btn primary" disabled={busy || !view || !view.lines.length} onClick={save}>
            {view?.example ? "Save changes" : "Flag this chat"}</button>
        </div>
      </div>
    </Overlay>
  );
}
