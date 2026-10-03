"use client";

// The unanswered queue across all clients (unanswered_api.py): customer
// messages the bot did not answer, or answered with a fallback phrase.
// Mark them reviewed, reopen them, or write an answer that is added to the
// client's industry FAQ. The top-bar badge counts open items.

import { useCallback, useState } from "react";
import { useToast } from "@/components/feedback";
import { PageShell, Tabs } from "@/components/ui";
import { api, errorText } from "@/lib/api";
import { cx, fmtDateTime } from "@/lib/format";
import { usePanel, useSocketEvent } from "@/lib/panel";
import type { UnansweredItem, UnansweredList } from "@/lib/types";
import { useLoader } from "@/lib/useLoader";

type Tab = "open" | "reviewed" | "added_to_template" | "all";

const TABS: [Tab, string][] = [["open", "Open"], ["reviewed", "Reviewed"], ["added_to_template", "Added to FAQ"], ["all", "All"]];
const REASONS: Record<string, string> = {
  skipped: "not answered (limit / no-reply rule)", ai_error: "AI error", soft_off: "client switched off",
  paused: "chat paused / taken over", escalated: "escalated", policy_hold: "held by policy",
  fallback: "fallback reply", staging: "staging (not a test chat)",
};
const STATUS: Record<string, string> = { open: "open", reviewed: "reviewed", added_to_template: "added to FAQ" };

function PromoteForm({ item, onDone }: { item: UnansweredItem; onDone: () => Promise<void> }) {
  const toast = useToast();
  const [question, setQuestion] = useState((item.text || "").replace(/\s+/g, " ").trim());
  const [answer, setAnswer] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <div className="pf-section ua-promote">
      <p className="pf-note">Adds a Q&amp;A entry to the FAQ of this client&apos;s industry template, as a new template
        version: every client in that industry gets it. Write the answer yourself; nothing is generated.</p>
      <div className="field">
        <label htmlFor={`ua-q-${item.id}`}>Question</label>
        <input id={`ua-q-${item.id}`} type="text" value={question} onChange={(ev) => setQuestion(ev.target.value)} />
      </div>
      <div className="field">
        <label htmlFor={`ua-a-${item.id}`}>Answer</label>
        <textarea id={`ua-a-${item.id}`} rows={4} placeholder="The answer the bot should give" value={answer}
                  onChange={(ev) => setAnswer(ev.target.value)} />
      </div>
      <button type="button" className="btn primary" disabled={busy} onClick={async () => {
        if (!answer.trim()) { toast("Write the answer first."); return; }
        setBusy(true);
        try {
          const r = await api<{ industry: { name: string; template_version: number }; pinned_clients?: number }>(
            "POST", `/api/unanswered/${item.id}/promote`, { question, answer });
          toast(`Added to the FAQ of ${r.industry.name} (template v${r.industry.template_version}).` +
            (r.pinned_clients ? ` ${r.pinned_clients} pinned client(s) won't see it until unpinned.` : ""), "info");
        } catch (err) { toast(errorText(err)); setBusy(false); return; }
        await onDone();
      }}>Add to the FAQ</button>
    </div>
  );
}

export function Unanswered() {
  const { setUnansweredOpen } = usePanel();
  const toast = useToast();
  const [tab, setTab] = useState<Tab>("open");
  const [tenantId, setTenantId] = useState("");
  const [promoting, setPromoting] = useState<number | null>(null);

  const fetchList = useCallback(async () => {
    const params = new URLSearchParams({ status: tab });
    if (tenantId) params.set("tenant_id", tenantId);
    const list = await api<UnansweredList>("GET", "/api/unanswered?" + params.toString());
    setUnansweredOpen(list.open);
    return list;
  }, [tab, tenantId, setUnansweredOpen]);
  const { data, error, reload: load } = useLoader(fetchList);

  // A running account says when it queued something.
  useSocketEvent(["unanswered"], () => { void load(); });

  const post = async (path: string) => {
    try { await api("POST", path); } catch (err) { toast(errorText(err)); }
    await load();
  };

  return (
    <PageShell title="Unanswered" crumb="customer messages the bot did not answer" width="w-980">
      <div className="bk-scroll">
        <Tabs tabs={TABS} value={tab} onChange={(key) => { setTab(key); setPromoting(null); }} />
        {error && <div className="pf-errors">{error}</div>}
        {data && <>
          <div className="bk-nav">
            <label className="muted" htmlFor="ua-client">Client</label>
            <select id="ua-client" value={tenantId} onChange={(ev) => setTenantId(ev.target.value)}>
              <option value="">All clients</option>
              {data.tenants.map((t) => <option key={t.id} value={String(t.id)}>{t.name}</option>)}
            </select>
          </div>
          {!data.items.length && (
            <div className="empty">{tab === "open" ? "Nothing waiting: every message got an answer." : "Nothing here."}</div>
          )}
          {data.items.map((item) => (
            <div key={item.id} className={cx("bk-row ua-item", `ua-${item.status}`)}>
              <div className="bk-head">
                <span className={cx("bk-state ua-reason", `ua-r-${item.reason}`)}>{REASONS[item.reason] || item.reason}</span>
                <span className="bk-time">{fmtDateTime(item.created_at)}</span>
                <span>{item.tenant_name}</span>
                <span className="muted">{item.customer || `Chat ${item.chat_id}`}</span>
                {item.status !== "open" && <span className="muted">{STATUS[item.status] || item.status}</span>}
              </div>
              <div className="bk-detail">
                <div className="ua-text">{item.text || "(the message is no longer stored)"}</div>
                {item.detail && <div className="muted ua-why">{item.detail}</div>}
                {item.reviewed_by && (
                  <div className="muted ua-why">{STATUS[item.status]} by {item.reviewed_by}, {fmtDateTime(item.reviewed_at)}</div>
                )}
                <div className="bk-actions">
                  {item.status === "open"
                    ? <button type="button" className="btn small" onClick={() => post(`/api/unanswered/${item.id}/reviewed`)}>Reviewed</button>
                    : <button type="button" className="btn small" onClick={() => post(`/api/unanswered/${item.id}/reopen`)}>Reopen</button>}
                  {item.status !== "added_to_template" && (
                    <button type="button" className="btn small"
                            onClick={() => setPromoting((p) => (p === item.id ? null : item.id))}>Promote to FAQ…</button>
                  )}
                </div>
                {promoting === item.id && (
                  <PromoteForm item={item} onDone={async () => { setPromoting(null); await load(); }} />
                )}
              </div>
            </div>
          ))}
        </>}
      </div>
    </PageShell>
  );
}
