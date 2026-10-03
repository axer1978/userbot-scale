"use client";

// Per-contact style: length, notes, samples and timing overrides for one
// chat, stored in the account's own config (contacts.{chat_id}).

import { useCallback, useEffect, useState } from "react";
import { useToast } from "@/components/feedback";
import { Overlay } from "@/components/ui";
import { errorText } from "@/lib/api";
import { contactLabel } from "@/lib/format";
import { usePanel } from "@/lib/panel";
import type { Contact, ContactStyle, SessionConfig } from "@/lib/types";

function emptyContactStyle(): ContactStyle {
  return {
    persona_extra: "", style_notes: "", chat_samples: "", message_length: "auto",
    min_delay_seconds: null, max_delay_seconds: null,
    typing_speed_cps: null, typing_max_seconds: null,
    online_delay_min: null, online_delay_max: null,
    offline_delay_min: null, offline_delay_max: null,
  };
}

type NumKey = "min_delay_seconds" | "max_delay_seconds" | "typing_speed_cps" | "typing_max_seconds" |
  "online_delay_min" | "online_delay_max" | "offline_delay_min" | "offline_delay_max";

const TIMING: [NumKey, string, number, number?][][] = [
  [["min_delay_seconds", "Reply delay min (s)", 0], ["max_delay_seconds", "Reply delay max (s)", 0]],
  [["typing_speed_cps", "Typing speed (chars/s)", 1, 100], ["typing_max_seconds", "Longest typing (s)", 1, 300]],
  [["online_delay_min", "Online delay min (s)", 0, 120], ["online_delay_max", "Online delay max (s)", 0, 120]],
  [["offline_delay_min", "Offline delay min (s)", 0, 3600], ["offline_delay_max", "Offline delay max (s)", 0, 3600]],
];

export function StyleDialog({ onClose }: { onClose: () => void }) {
  const { state, sApi, dispatch } = usePanel();
  const toast = useToast();
  const [contacts, setContacts] = useState<Contact[] | null>(null);
  const [chatId, setChatId] = useState("");
  const [style, setStyle] = useState<ContactStyle>(emptyContactStyle());
  const styled = state.config?.contacts || {};

  // Anyone you actually talk to, saved contact or not; the address book is a bonus.
  const buildContacts = useCallback(async (styledIds: string[]) => {
    const byId = new Map<string, Contact>();
    for (const c of state.conversations) {
      byId.set(String(c.chat_id), { chat_id: c.chat_id, display_name: c.display_name || String(c.chat_id), username: c.username });
    }
    try {
      for (const c of await sApi<Contact[]>("GET", "/contacts")) {
        if (!byId.has(String(c.chat_id))) byId.set(String(c.chat_id), c);
      }
    } catch { /* conversations are enough */ }
    // Anything already styled stays selectable even if the chat has scrolled away.
    for (const id of styledIds) {
      if (!byId.has(id)) byId.set(id, { chat_id: Number(id), display_name: `Chat ${id}`, username: null });
    }
    return [...byId.values()].sort((a, b) => a.display_name.localeCompare(b.display_name));
  }, [state.conversations, sApi]);

  useEffect(() => {
    // Once, when the dialog opens; a save rebuilds it.
    let cancelled = false;
    buildContacts(Object.keys(state.config?.contacts || {})).then((list) => { if (!cancelled) setContacts(list); });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const pick = (id: string) => {
    setChatId(id);
    if (id) setStyle({ ...emptyContactStyle(), ...(styled[id] || {}) });
  };

  const saveConfig = async (change: (cfg: SessionConfig) => void, done: string) => {
    if (!chatId) { toast("Pick a contact first."); return; }
    if (!state.config) { toast("The account's settings have not loaded yet."); return; }
    try {
      const cfg: SessionConfig = structuredClone(state.config);
      change(cfg);
      const saved = await sApi<SessionConfig>("PUT", "/config", cfg);
      dispatch({ type: "config", config: saved });
      setContacts(await buildContacts(Object.keys(saved.contacts || {})));
      toast(done, "info");
    } catch (err) { toast(errorText(err)); }
  };

  const num = (key: NumKey, label: string, min: number, max?: number) => (
    <div className="field" key={key}>
      <label htmlFor={`cs-${key}`}>{label}</label>
      <input id={`cs-${key}`} type="number" min={min} max={max} step={1} value={style[key] ?? ""}
             onChange={(ev) => setStyle((s) => ({ ...s, [key]: ev.target.value === "" ? null : Number(ev.target.value) }))} />
    </div>
  );

  const text = (key: "style_notes" | "persona_extra" | "chat_samples", label: string, rows: number, placeholder: string) => (
    <div className="field">
      <label htmlFor={`cs-${key}`}>{label}</label>
      <textarea id={`cs-${key}`} rows={rows} placeholder={placeholder} value={style[key]}
                onChange={(ev) => setStyle((s) => ({ ...s, [key]: ev.target.value }))} />
    </div>
  );

  return (
    <Overlay onClose={onClose}>
      <div className="sheet">
        <h2>Style &amp; fine-tuning</h2>
        <p className="hint">Two ways to shape how it writes: samples that apply everywhere, and
          per-contact overrides for timing, length and tone.</p>
        <p className="hint">Writing samples for every conversation are part of the client&apos;s prompt now:{" "}
          <b>Settings → Prompt → Examples of how we write</b>.</p>

        <fieldset>
          <legend>Per-contact style</legend>
          <div className="field">
            <label htmlFor="cs-select">Chat <span className="muted">— anyone you&apos;ve messaged (✓ = already styled)</span></label>
            <select id="cs-select" value={chatId} onChange={(ev) => pick(ev.target.value)}>
              {contacts === null ? <option value="">Loading contacts…</option>
                : !contacts.length ? <option value="">No chats yet — message someone first</option>
                : <>
                    <option value="">Choose a chat…</option>
                    {contacts.map((c) => (
                      <option key={c.chat_id} value={String(c.chat_id)}>
                        {contactLabel(c)}{styled[String(c.chat_id)] ? " ✓" : ""}
                      </option>
                    ))}
                  </>}
            </select>
          </div>

          {chatId && (
            <div>
              <div className="field"><label htmlFor="cs-length">Message length</label>
                <select id="cs-length" value={style.message_length || "auto"}
                        onChange={(ev) => setStyle((s) => ({ ...s, message_length: ev.target.value }))}>
                  <option value="auto">Auto (match how they write)</option>
                  <option value="short">Short — a few words to one line</option>
                  <option value="medium">Medium — a couple of sentences</option>
                  <option value="long">Long — detailed messages are fine</option>
                </select>
              </div>
              {text("style_notes", "Style notes for this person", 3, "e.g. old friend, very casual, lots of inside jokes, swears a lot")}
              {text("persona_extra", "Extra persona instructions for this person", 2, "e.g. never discuss work with this person")}
              {text("chat_samples", "Writing samples with this specific person", 5, "Paste real past messages to them, to match this specific voice")}

              <p className="sub" style={{ marginTop: 18 }}>Timing overrides <span className="muted">— blank uses the global Settings value</span></p>
              {TIMING.map((pair, i) => (
                <div className="row" key={i}>{pair.map(([key, label, min, max]) => num(key, label, min, max))}</div>
              ))}

              <div className="sheet-actions">
                <button type="button" className="btn warn" onClick={() => saveConfig((cfg) => {
                  if (cfg.contacts) delete cfg.contacts[chatId];
                  setStyle(emptyContactStyle());
                }, "Reset to global settings.")}>Reset to global</button>
                <button type="button" className="btn primary" onClick={() => saveConfig((cfg) => {
                  cfg.contacts = { ...(cfg.contacts || {}), [chatId]: style };
                }, "Contact style saved.")}>Save this contact&apos;s style</button>
              </div>
            </div>
          )}
        </fieldset>

        <div className="sheet-actions">
          <button type="button" className="btn" onClick={onClose}>Close</button>
        </div>
      </div>
    </Overlay>
  );
}
