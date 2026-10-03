"use client";

// "Message my contacts": pick contacts, say what the message should do;
// each one gets its own message, spaced out and capped per day.

import { useEffect, useMemo, useState } from "react";
import { useToast } from "@/components/feedback";
import { Overlay } from "@/components/ui";
import { errorText } from "@/lib/api";
import { usePanel } from "@/lib/panel";
import type { Contact, OutreachItem, SessionConfig } from "@/lib/types";

function matches(c: Contact, term: string) {
  return !term || c.display_name.toLowerCase().includes(term) || (c.username || "").toLowerCase().includes(term);
}

export function OutreachDialog({ onClose }: { onClose: () => void }) {
  const { state, sApi, dispatch } = usePanel();
  const toast = useToast();
  const [contacts, setContacts] = useState<Contact[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [filter, setFilter] = useState("");
  const [goal, setGoal] = useState("");
  const [busy, setBusy] = useState(false);
  const outreach = state.config?.outreach;
  const [pacing, setPacing] = useState({
    auto: outreach?.auto_send ?? false,
    min: String(outreach?.min_gap_seconds ?? ""),
    max: String(outreach?.max_gap_seconds ?? ""),
    limit: String(outreach?.daily_limit ?? ""),
  });

  useEffect(() => {
    let cancelled = false;
    sApi<Contact[]>("GET", "/contacts")
      .then((list) => { if (!cancelled) setContacts(list); })
      .catch((err) => { if (!cancelled) setLoadError(errorText(err)); });
    sApi<OutreachItem[]>("GET", "/outreach")
      .then((items) => { if (!cancelled) dispatch({ type: "outreach", items }); })
      .catch(() => { /* the websocket will refresh it */ });
    return () => { cancelled = true; };
  }, [sApi, dispatch]);

  const term = filter.trim().toLowerCase();
  const shown = useMemo(() => (contacts || []).filter((c) => matches(c, term)), [contacts, term]);

  const toggle = (chatId: number, on: boolean) => setSelected((s) => {
    const next = new Set(s);
    if (on) next.add(chatId); else next.delete(chatId);
    return next;
  });

  const queue = async () => {
    if (!goal.trim()) { toast("Say what the message should achieve."); return; }
    if (!selected.size) { toast("Pick at least one contact."); return; }
    if (!state.config) { toast("The account's settings have not loaded yet."); return; }
    // Pacing and the daily cap live in config, so save them before queueing.
    try {
      const cfg: SessionConfig = structuredClone(state.config);
      cfg.outreach.auto_send = pacing.auto;
      cfg.outreach.min_gap_seconds = Number(pacing.min);
      cfg.outreach.max_gap_seconds = Number(pacing.max);
      cfg.outreach.daily_limit = Number(pacing.limit);
      dispatch({ type: "config", config: await sApi<SessionConfig>("PUT", "/config", cfg) });
    } catch (err) { toast("Could not save outreach settings: " + errorText(err)); return; }

    setBusy(true);
    try {
      const res = await sApi<{ queued: number; skipped?: number }>("POST", "/outreach",
        { chat_ids: [...selected], goal: goal.trim() });
      toast(`Queued ${res.queued} message(s)${res.skipped ? `, ${res.skipped} already queued` : ""}.`, "info");
      setSelected(new Set());
      dispatch({ type: "outreach", items: await sApi<OutreachItem[]>("GET", "/outreach") });
    } catch (err) { toast(errorText(err)); }
    setBusy(false);
  };

  const cancelQueued = async () => {
    try {
      const res = await sApi<{ cancelled: number }>("POST", "/outreach/cancel");
      toast(`Cancelled ${res.cancelled} queued message(s).`, "info");
      dispatch({ type: "outreach", items: await sApi<OutreachItem[]>("GET", "/outreach") });
    } catch (err) { toast(errorText(err)); }
  };

  return (
    <Overlay onClose={onClose}>
      <div className="sheet">
        <h2>Message my contacts</h2>
        <p className="hint">Pick people from your Telegram contacts and say what the message should
          achieve — each one gets its own message written for them. Sends are spaced out and
          capped per day, because Telegram penalises bursts of new conversations.</p>

        <div className="field">
          <label htmlFor="o-goal">What should the message do?</label>
          <textarea id="o-goal" rows={3} value={goal} onChange={(ev) => setGoal(ev.target.value)}
                    placeholder="e.g. let them know I'm away next week and to reach me by email" />
        </div>

        <div className="field">
          <label>Contacts <span className="muted">— {selected.size} selected of {contacts?.length ?? 0}</span></label>
          <input type="text" placeholder="Filter by name or @username…" value={filter}
                 onChange={(ev) => setFilter(ev.target.value)} />
          <div className="picker">
            {loadError ? <div className="row-item">Could not load contacts: {loadError}</div>
              : contacts === null ? <div className="row-item">Loading contacts…</div>
              : !shown.length ? <div className="row-item">
                  {contacts.length ? "No contacts match that filter." : "No contacts found on this account."}</div>
              : shown.map((c) => (
                <label key={c.chat_id} className="row-item">
                  <input type="checkbox" checked={selected.has(c.chat_id)}
                         onChange={(ev) => toggle(c.chat_id, ev.target.checked)} />
                  <span>{c.display_name}</span>
                  {c.username && <span className="handle">@{c.username}</span>}
                  {c.is_bot && <span className="badge bot">bot</span>}
                </label>
              ))}
          </div>
          <div className="picker-actions">
            <button type="button" className="btn small" onClick={() =>
              setSelected((s) => new Set([...s, ...shown.map((c) => c.chat_id)]))}>Select all shown</button>
            <button type="button" className="btn small" onClick={() => setSelected(new Set())}>Clear selection</button>
          </div>
        </div>

        <div className="field check">
          <input id="o-auto" type="checkbox" checked={pacing.auto}
                 onChange={(ev) => setPacing((p) => ({ ...p, auto: ev.target.checked }))} />
          <label htmlFor="o-auto">Send without asking me (otherwise each one waits for approval)</label>
        </div>

        <div className="row">
          <div className="field"><label htmlFor="o-min">Min gap between sends (s)</label>
            <input id="o-min" type="number" min={5} step={5} value={pacing.min}
                   onChange={(ev) => setPacing((p) => ({ ...p, min: ev.target.value }))} /></div>
          <div className="field"><label htmlFor="o-max">Max gap (s)</label>
            <input id="o-max" type="number" min={5} step={5} value={pacing.max}
                   onChange={(ev) => setPacing((p) => ({ ...p, max: ev.target.value }))} /></div>
          <div className="field"><label htmlFor="o-limit">Max per day</label>
            <input id="o-limit" type="number" min={1} step={1} value={pacing.limit}
                   onChange={(ev) => setPacing((p) => ({ ...p, limit: ev.target.value }))} /></div>
        </div>

        <div className="sheet-actions">
          <button type="button" className="btn" onClick={onClose}>Close</button>
          <button type="button" className="btn warn" onClick={cancelQueued}>Cancel queued</button>
          <button type="button" className="btn primary" disabled={busy} onClick={queue}>Queue messages</button>
        </div>

        <h3 className="sub">Queue</h3>
        {!state.outreachItems.length ? <div className="queue empty-queue">Nothing queued yet.</div> : (
          <div className="queue">
            {state.outreachItems.slice().reverse().map((item, i) => (
              <div key={`${item.chat_id}-${i}`} className="q">
                <span className={`tag ${item.status}`}>{item.status}</span>
                <span className="who">{item.display_name || String(item.chat_id)}</span>
                <span className="txt">{item.error || item.message || item.goal}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </Overlay>
  );
}
