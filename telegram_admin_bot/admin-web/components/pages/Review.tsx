"use client";

// Review batches for the trainer (review_api.py). A batch is every reply the
// bot wrote and sent to one client's customers in a date range, each with
// the conversation before it. The trainer approves, rejects or corrects
// each one; approved and corrected ones export as JSONL for training.
// Big buttons and no keyboard shortcuts: this is meant to work on a tablet.

import { useCallback, useEffect, useRef, useState } from "react";
import { useDialogs, useToast } from "@/components/feedback";
import { PageShell } from "@/components/ui";
import { api, errorText } from "@/lib/api";
import { cx, fmtTime, isoDate } from "@/lib/format";
import { usePanel } from "@/lib/panel";
import type { PlatformTree, ReviewBatch, ReviewCounts, ReviewItem, TreeTenant } from "@/lib/types";
import { useLoader } from "@/lib/useLoader";

const PAGE = 50;
const CONTEXT_SHORT = 6;
const DECISION = { approve: "Approved", reject: "Rejected", edit: "Edited" } as const;
const COUNT_KEY = { approve: "approved", reject: "rejected", edit: "edited" } as const;
type Decision = keyof typeof DECISION;

function Progress({ counts }: { counts: ReviewCounts }) {
  const total = counts.total || 1;
  return (
    <div className="rv-bar">
      {([["approved", "ok"], ["edited", "edit"], ["rejected", "bad"]] as const).map(([key, cls]) => (
        <span key={key} className={`rv-bar-${cls}`} style={{ width: `${(100 * counts[key]) / total}%` }} />
      ))}
    </div>
  );
}

function countsText(c: ReviewCounts): string {
  return `${c.total - c.undecided} of ${c.total} decided · ${c.approved} approved · ${c.edited} edited · ${c.rejected} rejected`;
}

/* ------------------------------------------------------------- batch list */

function BatchList({ tenants, tenantId, setTenantId, open }: {
  tenants: TreeTenant[]; tenantId: number | null; setTenantId: (id: number) => void; open: (batchId: number) => void;
}) {
  const toast = useToast();
  const { confirm } = useDialogs();
  const [form, setForm] = useState(() => {
    const to = new Date();
    return { from: isoDate(new Date(to.getTime() - 6 * 86400000)), to: isoDate(to), name: "" };
  });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const tenantName = (id: number | null) => tenants.find((t) => t.id === id)?.name ?? `Client ${id}`;

  const fetchBatches = useCallback(async () => tenantId === null ? []
    : api<ReviewBatch[]>("GET", `/api/review/batches?tenant_id=${tenantId}`), [tenantId]);
  const { data: batches, error: listError, reload: load } = useLoader(fetchBatches);

  return (
    <>
      <div className="pf-section rv-new">
        <div className="title">New review batch</div>
        <div className="row">
          <div className="field"><label htmlFor="rv-client">Client</label>
            <select id="rv-client" value={tenantId ?? ""} onChange={(ev) => setTenantId(Number(ev.target.value))}>
              {tenants.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
            </select></div>
          <div className="field"><label htmlFor="rv-from">From</label>
            <input id="rv-from" type="date" value={form.from} onChange={(ev) => setForm({ ...form, from: ev.target.value })} /></div>
          <div className="field"><label htmlFor="rv-to">To (including)</label>
            <input id="rv-to" type="date" value={form.to} onChange={(ev) => setForm({ ...form, to: ev.target.value })} /></div>
          <div className="field"><label htmlFor="rv-name">Name</label>
            <input id="rv-name" type="text" maxLength={200} placeholder="e.g. September, week 1" value={form.name}
                   onChange={(ev) => setForm({ ...form, name: ev.target.value })} /></div>
        </div>
        <p className="pf-note">Takes every reply the bot wrote and sent in those days (the client&apos;s own timezone), with
          up to 20 messages of the conversation before each. At most 2000 replies per batch.</p>
        <div className="pf-errors">{error}</div>
        <div className="pf-actions">
          <button type="button" className="btn primary" disabled={busy || tenantId === null} onClick={async () => {
            setError("");
            const label = form.name.trim() || `${tenantName(tenantId)} ${form.from} – ${form.to}`;
            setBusy(true);
            try {
              const batch = await api<ReviewBatch>("POST", "/api/review/batches",
                { tenant_id: tenantId, name: label, date_from: form.from, date_to: form.to });
              open(batch.id);
            } catch (err) { setError(errorText(err)); }
            finally { setBusy(false); }
          }}>Create batch</button>
        </div>
      </div>

      <h3 className="sf-sub">Batches of {tenantName(tenantId)}</h3>
      {listError && <div className="pf-errors">{listError}</div>}
      {batches && !batches.length && <p className="pf-note">No batches yet for this client.</p>}
      {(batches || []).map((b) => (
        <div key={b.id} className={cx("bk-row rv-batch", b.status === "done" && "done")}>
          <div className="bk-head">
            <b>{b.name}</b>
            <span className="bk-num">{b.date_from} – {b.date_to}</span>
            <span className="bk-state">{b.status === "done" ? "done" : "open"}</span>
          </div>
          <div className="rv-batch-detail">
            <Progress counts={b.counts} />
            <div className="muted">{countsText(b.counts)}</div>
            <div className="bk-actions">
              <button type="button" className="btn primary" onClick={() => open(b.id)}>{b.counts.undecided ? "Continue" : "Open"}</button>
              <a className="btn" href={`/api/review/batches/${b.id}/export.jsonl`}>Export JSONL</a>
              <button type="button" className="btn warn" onClick={async () => {
                if (!(await confirm(`Delete the batch "${b.name}" and its decisions? The messages themselves stay.`))) return;
                try { await api("DELETE", `/api/review/batches/${b.id}`); await load(); }
                catch (err) { toast(errorText(err)); }
              }}>Delete</button>
            </div>
          </div>
        </div>
      ))}
    </>
  );
}

/* ------------------------------------------------------------- one by one */

function BatchItems({ batchId, tenantName, back }: { batchId: number; tenantName: (id: number) => string; back: () => void }) {
  const toast = useToast();
  const { confirm } = useDialogs();
  const [batch, setBatch] = useState<ReviewBatch | null>(null);
  const [items, setItems] = useState<Map<number, ReviewItem>>(new Map());
  const [index, setIndex] = useState(0);
  const [editing, setEditing] = useState(false);
  const [editText, setEditText] = useState("");
  const [showAll, setShowAll] = useState(false);
  const [error, setError] = useState("");

  // A page of items around `offset`, which also brings the batch's counts.
  const fetchPage = useCallback(async (offset: number) => {
    const start = Math.floor(offset / PAGE) * PAGE;
    const page = await api<ReviewBatch & { items: ReviewItem[] }>("GET", `/api/review/batches/${batchId}?offset=${start}&limit=${PAGE}`);
    return { start, page };
  }, [batchId]);

  const applyPage = useCallback(({ start, page }: Awaited<ReturnType<typeof fetchPage>>) => {
    setBatch((b) => ({ ...(b || page), counts: page.counts, status: page.status, first_undecided: page.first_undecided }));
    setItems((m) => {
      const next = new Map(m);
      page.items.forEach((item, i) => next.set(start + i, item));
      return next;
    });
  }, []);

  useEffect(() => {
    let cancelled = false;
    api<ReviewBatch>("GET", `/api/review/batches/${batchId}?offset=0&limit=1`).then((first) => {
      if (cancelled) return;
      setBatch(first);
      setIndex(first.first_undecided === null ? 0 : first.first_undecided);
    }, (err) => { toast(errorText(err)); back(); });
    return () => { cancelled = true; };
  }, [batchId, toast, back]);

  // The page the item on screen is on, once.
  const requested = useRef(new Set<number>());
  const total = batch?.counts.total ?? 0;
  useEffect(() => {
    const start = Math.floor(index / PAGE) * PAGE;
    if (!total || items.has(index) || index >= total || requested.current.has(start)) return;
    requested.current.add(start);
    fetchPage(index).then(applyPage, (err) => { requested.current.delete(start); toast(errorText(err)); });
  }, [total, index, items, fetchPage, applyPage, toast]);

  if (!batch) return null;
  const item = items.get(index);
  const c = batch.counts;

  const go = (to: number) => {
    setIndex(Math.max(0, Math.min(to, c.total - 1)));
    setEditing(false);
    setShowAll(false);
    setError("");
  };

  const decide = async (decision: Decision, editedText?: string) => {
    if (!item) return;
    setError("");
    try {
      const saved = await api<ReviewItem>("POST", `/api/review/items/${item.id}`, { decision, edited_text: editedText });
      setItems((m) => new Map(m).set(index, saved));
      const counts = { ...c };
      if (item.decision) counts[COUNT_KEY[item.decision]] -= 1; else counts.undecided -= 1;
      counts[COUNT_KEY[decision]] += 1;
      setBatch({ ...batch, counts });
      setEditing(false);
      if (index < counts.total - 1) go(index + 1);
    } catch (err) { setError(errorText(err)); }
  };

  const messages = item?.context || [];
  const shown = showAll ? messages : messages.slice(-CONTEXT_SHORT);

  return (
    <>
      <div className="rv-top">
        <button type="button" className="btn" onClick={back}>← Batches</button>
        <b>Reply {index + 1} of {c.total}</b>
        <span className="muted">{countsText(c)}</span>
        <span className="spacer" />
        <a className="btn" href={`/api/review/batches/${batch.id}/export.jsonl`}>Export JSONL</a>
        <button type="button" className={cx("btn", batch.status !== "done" && "primary")} disabled={batch.status === "done"}
                onClick={async () => {
                  if (c.undecided && !(await confirm(`${c.undecided} replies have no decision yet; they are left out of ` +
                                                     "the export. Mark the batch done anyway?"))) return;
                  try {
                    const saved = await api<ReviewBatch>("POST", `/api/review/batches/${batch.id}/done`);
                    setBatch({ ...batch, status: saved.status, counts: saved.counts });
                  } catch (err) { toast(errorText(err)); }
                }}>{batch.status === "done" ? "Done ✓" : "Mark done"}</button>
      </div>
      <Progress counts={c} />
      <p className="sf-sub muted">{tenantName(batch.tenant_id)} · {batch.name}</p>

      {!c.total && <p className="pf-note">This batch has no replies.</p>}
      {item && <>
        {/* The conversation before the reply, compact: the last few, all on request. */}
        <div className="rv-context">
          <div className="rv-label">Conversation with {item.chat_name || `chat ${item.chat_id}`}
            {item.sent_at ? ` · ${fmtTime(item.sent_at)}` : ""}</div>
          {messages.length > shown.length && (
            <button type="button" className="btn small" onClick={() => setShowAll(true)}>
              Show all {messages.length} earlier messages</button>
          )}
          {!messages.length && <div className="muted">(no earlier messages)</div>}
          {shown.map((m, i) => <div key={i} className={cx("rv-msg", m.role === "user" ? "user" : "assistant")}>{m.content}</div>)}
        </div>

        <div className={cx("rv-reply", item.decision)}>
          <div className="rv-label">The bot&apos;s reply{item.decision ? ` — ${DECISION[item.decision]}` : ""}</div>
          <div className={cx("rv-text", item.decision === "edit" && "struck")}>{item.reply}</div>
          {item.decision === "edit" && item.edited_text && <>
            <div className="rv-label">Corrected</div>
            <div className="rv-text">{item.edited_text}</div>
          </>}
        </div>

        {editing ? <>
          <textarea className="rv-edit" rows={5} autoFocus value={editText} onChange={(ev) => setEditText(ev.target.value)} />
          <div className="pf-errors">{error}</div>
          <div className="rv-buttons">
            <button type="button" className="btn primary rv-big" onClick={() => {
              if (!editText.trim()) { setError("The corrected reply cannot be empty."); return; }
              void decide("edit", editText);
            }}>Save correction</button>
            <button type="button" className="btn rv-big" onClick={() => setEditing(false)}>Cancel</button>
          </div>
        </> : <>
          <div className="rv-buttons">
            <button type="button" className={cx("btn rv-big rv-approve", item.decision === "approve" && "on")}
                    onClick={() => decide("approve")}>Approve</button>
            <button type="button" className={cx("btn rv-big rv-reject", item.decision === "reject" && "on")}
                    onClick={() => decide("reject")}>Reject</button>
            <button type="button" className={cx("btn rv-big rv-editbtn", item.decision === "edit" && "on")}
                    onClick={() => { setEditText(item.edited_text || item.reply); setEditing(true); }}>Edit</button>
          </div>
          <div className="pf-errors">{error}</div>
        </>}
      </>}

      <div className="rv-buttons rv-nav">
        <button type="button" className="btn rv-big" disabled={index === 0} onClick={() => go(index - 1)}>← Previous</button>
        <button type="button" className="btn rv-big" disabled={index >= c.total - 1} onClick={() => go(index + 1)}>Next →</button>
        {batch.first_undecided !== null && c.undecided > 0 && (
          <button type="button" className="btn rv-big" onClick={async () => {
            try {
              const first = await fetchPage(0);
              applyPage(first);
              if (first.page.first_undecided !== null) go(first.page.first_undecided);
            } catch (err) { toast(errorText(err)); }
          }}>First undecided</button>
        )}
      </div>
    </>
  );
}

/* -------------------------------------------------------------------- page */

export function Review() {
  const { state } = usePanel();
  const toast = useToast();
  const [tenants, setTenants] = useState<TreeTenant[] | null>(null);
  const [tenantId, setTenantId] = useState<number | null>(null);
  const [batchId, setBatchId] = useState<number | null>(null);

  useEffect(() => {
    api<PlatformTree>("GET", "/api/platform/tree").then((tree) => {
      setTenants(tree.tenants);
      // Start on the client of the account that is open, if there is one.
      const current = tree.tenants.find((t) => t.session_id === state.sessionId);
      setTenantId((id) => id ?? (current || tree.tenants[0])?.id ?? null);
    }).catch((err) => toast(errorText(err)));
    // Once per visit; the open account only picks the starting client.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const tenantName = useCallback((id: number) => tenants?.find((t) => t.id === id)?.name ?? `Client ${id}`, [tenants]);
  const back = useCallback(() => setBatchId(null), []);

  return (
    <PageShell title="Review" crumb="the bot's replies, for training" width="w-900">
      <div className="bk-scroll top-pad">
        {tenants && (batchId === null
          ? <BatchList tenants={tenants} tenantId={tenantId} setTenantId={setTenantId} open={setBatchId} />
          : <BatchItems key={batchId} batchId={batchId} tenantName={tenantName} back={back} />)}
      </div>
    </PageShell>
  );
}
