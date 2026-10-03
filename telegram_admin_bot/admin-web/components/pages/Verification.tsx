"use client";

// Identity videos and client photos that wait for the platform admin
// (review_admin_api.py, review.py). Not the Review page, which is about
// the bot's replies for training.
//
// - Videos: a client films themselves holding a random code on paper and
//   doing a random gesture; approve or reject (the client sees the reason).
// - Photos: every photo a client of a reviewed business adds, and every file
//   pulled back for a re-check, waits here before the bot may use it.
// - Businesses: which ones are under review, which are paused until a video
//   is approved, and the industries whose businesses all need review.

import "@/app/staff-verification.css";
import { useCallback, useEffect, useRef, useState } from "react";
import { useDialogs, useToast } from "@/components/feedback";
import { PageShell, Tabs } from "@/components/ui";
import { api, errorText } from "@/lib/api";
import { cx, fmtBytes, fmtDateTime } from "@/lib/format";
import { usePanel } from "@/lib/panel";
import type { ReviewIndustry, ReviewPhoto, ReviewSummary, Verification as VerificationItem } from "@/lib/types";
import { useLoader } from "@/lib/useLoader";

export type VerifyTab = "videos" | "photos" | "businesses";
type Tenant = NonNullable<ReviewSummary["tenants"]>[number];

const STATUS: Record<string, [string, string]> = {
  requested: ["asked, no video yet", "paused"],
  submitted: ["video waiting", "paused"],
  approved: ["verified", "link"],
  rejected: ["rejected", "escalated"],
};
const PHOTO_STATUS: Record<string, [string, string]> = {
  pending: ["waiting", "paused"],
  approved: ["approved", "link"],
  rejected: ["rejected", "escalated"],
  withdrawn: ["withdrawn by the client", ""],
};

type View =
  | { tab: "videos"; items: VerificationItem[] }
  | { tab: "photos"; items: ReviewPhoto[] }
  | { tab: "businesses"; items: ReviewIndustry[] };
/** One answer, tagged with what was asked so a tab never shows another's data. */
type Loaded = { key: string; stamp: number; view: View | null; error: string | null };

/**
 * A decision: tell the result, then refresh the list and the badge. Gives
 * back what the server answered (true when it said nothing), or null when
 * it failed (already told in a toast).
 */
type Call = (method: string, path: string, body: unknown, done: string | ((result: unknown) => string)) =>
  Promise<unknown>;

// Every load gets a new stamp; the lists are keyed by it, so a decision
// draws them afresh (closed forms, cleared inputs) as the old panel did.
let loads = 0;

function Badge({ map, status }: { map: Record<string, [string, string]>; status?: string | null }) {
  const [label, kind] = (status && map[status]) || [status || "not asked", ""];
  return <span className={cx("badge", kind)}>{label}</span>;
}

// "Show all" switch above a list.
function ShowAll({ checked, label, onChange }: { checked: boolean; label: string; onChange: (on: boolean) => void }) {
  return (
    <div className="field check vf-showall">
      <input id="vf-showall" type="checkbox" checked={checked} onChange={(ev) => onChange(ev.target.checked)} />
      <label htmlFor="vf-showall">{label}</label>
    </div>
  );
}

// A hidden inline form: a reason input and a button that needs it.
function ReasonForm({ open, onClose, placeholder, label, onSubmit }: {
  open: boolean; onClose: () => void; placeholder: string; label: string;
  /** Resolves with null when it was cancelled or failed, so the button works again. */
  onSubmit: (reason: string) => Promise<unknown>;
}) {
  const toast = useToast();
  const input = useRef<HTMLInputElement>(null);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (open) input.current?.focus(); }, [open]);

  const submit = async () => {
    const reason = text.trim();
    if (!reason) { toast("Write a reason first."); input.current?.focus(); return; }
    setBusy(true);
    if (!(await onSubmit(reason))) setBusy(false);
  };

  return (
    <div className="pf-actions vf-reason" hidden={!open}>
      <input ref={input} type="text" maxLength={500} placeholder={placeholder} value={text}
             onChange={(ev) => setText(ev.target.value)}
             onKeyDown={(ev) => { if (ev.key === "Enter") void submit(); }} />
      <button type="button" className="btn small warn" disabled={busy} onClick={() => void submit()}>{label}</button>
      <button type="button" className="btn small" onClick={onClose}>Cancel</button>
    </div>
  );
}

// <img>/<video> that turns into a note when the file is gone.
function Media({ kind, src, missing }: { kind: string; src: string; missing: string }) {
  const [failed, setFailed] = useState(false);
  return (
    <div className="vf-media">
      {failed ? <div className="vf-missing">{missing}</div>
        : kind === "video" ? <video controls preload="metadata" playsInline src={src} onError={() => setFailed(true)} />
        // eslint-disable-next-line @next/next/no-img-element
        : <img alt="" loading="lazy" src={src} onError={() => setFailed(true)} />}
    </div>
  );
}

/* ------------------------------------------------------------------ videos */

function VideoCard({ v, call }: { v: VerificationItem; call: Call }) {
  const toast = useToast();
  const { confirm } = useDialogs();
  const reasonInput = useRef<HTMLInputElement>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null);
  const [videoFailed, setVideoFailed] = useState(false);

  const facts: [string, string | null | undefined][] = [
    ["Name", v.display_name],
    ["Company", v.company],
    ["E-mail", v.email],
    ["Phone", v.phone],
    ["Businesses", (v.tenants || []).map((t) => t.name).join(", ") || "none linked"],
    ["Asked because", v.reason + (v.requested_by ? ` (${v.requested_by})` : "")],
    ["Submitted", v.submitted_at ? fmtDateTime(v.submitted_at) + (v.video_bytes ? ` · ${fmtBytes(v.video_bytes)}` : "") : ""],
  ];
  if (v.reviewed_at) facts.push(["Decided", fmtDateTime(v.reviewed_at)]);

  const approve = async () => {
    setBusy("approve");
    const done = await call("POST", `/api/review/verifications/${v.id}/approve`, { reason: reason.trim() },
      `${v.username} verified. Their businesses run again unless something else holds them.`);
    if (!done) setBusy(null);
  };
  const reject = async () => {
    const text = reason.trim();
    if (!text) {
      toast("Write a reason first: the client sees it.");
      reasonInput.current?.focus();
      return;
    }
    if (!(await confirm(`Reject the video of ${v.username}? They will see this reason:\n\n${text}`))) return;
    setBusy("reject");
    const done = await call("POST", `/api/review/verifications/${v.id}/reject`, { reason: text },
      `Video of ${v.username} rejected. They have to send a new one; their businesses stay paused.`);
    if (!done) setBusy(null);
  };

  return (
    <div className={cx("pf-section ow-card vf-card", v.status !== "submitted" && "vf-done")}>
      <div className="title">
        <span>{v.username}</span>
        <span className="ow-badges"><Badge map={STATUS} status={v.status} /></span>
      </div>
      <div className="ow-facts">
        {facts.map(([label, value]) => (
          <div key={label}><span className="muted">{label}</span><span>{value || "—"}</span></div>
        ))}
      </div>
      {v.review_reason && (
        <div className={cx("pf-note", v.status === "rejected" ? "ow-reason" : "ow-meta")}>
          {(v.status === "rejected" ? "Reason given: " : "Note: ") + v.review_reason}
        </div>
      )}

      <div className="vf-video-row">
        <div className="vf-video">
          {v.has_video ? (videoFailed ? <div className="vf-missing">The video could not be loaded.</div> : (
            <video controls preload="metadata" playsInline src={`/api/review/verifications/${v.id}/video`}
                   onError={() => setVideoFailed(true)} />
          )) : v.video_deleted_at ? (
            <div className="vf-missing">{`Video deleted ${fmtDateTime(v.video_deleted_at)} (kept 30 days).`}</div>
          ) : <div className="vf-missing">No video yet.</div>}
        </div>
        <div className="vf-challenge">
          <div className="vf-must-label">The video must show</div>
          {v.challenge ? <>
            <div className="vf-code">{v.challenge}</div>
            <div className="vf-gesture">{v.gesture || "—"}</div>
            {v.challenge_at && <div className="muted vf-small">{`Code given ${fmtDateTime(v.challenge_at)}`}</div>}
          </> : <div className="muted">No code was given yet.</div>}
          <div className="vf-checklist">
            Code readable on paper? Gesture done? Same person throughout? Looks 18+? Not a recording of a screen?
          </div>
        </div>
      </div>

      {v.status === "submitted" && <>
        <div className="pf-actions">
          <input ref={reasonInput} type="text" maxLength={500} value={reason} onChange={(ev) => setReason(ev.target.value)}
                 placeholder="Note: optional to approve, required to reject (the client sees it)" />
        </div>
        <div className="pf-actions">
          <button type="button" className="btn small primary" disabled={busy === "approve"}
                  onClick={() => void approve()}>Approve</button>
          <button type="button" className="btn small warn" disabled={busy === "reject"}
                  onClick={() => void reject()}>Reject</button>
        </div>
      </>}
    </div>
  );
}

function Videos({ items, all, onAll, stamp, call }: {
  items: VerificationItem[]; all: boolean; onAll: (on: boolean) => void; stamp: number; call: Call;
}) {
  return (
    <>
      <p className="pf-note">{"Each client got a random code to write on paper and a random gesture, " +
        "and had 30 minutes to film themselves showing both: a video made earlier can't know the code. " +
        "Only you see these videos; they are deleted 30 days after your decision. Approving lifts the pause on " +
        "their businesses; rejecting asks for a new video, and the client sees your reason."}</p>
      <ShowAll checked={all} label="Show all (also decided and still-open requests)" onChange={onAll} />
      {!items.length && <div className="pf-note">{all ? "No verification yet." : "No video is waiting."}</div>}
      {items.map((v) => <VideoCard key={`${stamp}-${v.id}`} v={v} call={call} />)}
    </>
  );
}

/* ------------------------------------------------------------------ photos */

function PhotoCard({ p, call }: { p: ReviewPhoto; call: Call }) {
  const toast = useToast();
  const reasonInput = useRef<HTMLInputElement>(null);
  const [description, setDescription] = useState(p.description || "");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null);
  const pending = p.status === "pending";
  const from = p.source === "recheck" ? "pulled back for re-check" : `from ${p.username || "a deleted login"}`;

  const approve = async () => {
    setBusy("approve");
    const done = await call("POST", `/api/review/photos/${p.id}/approve`, { reason: "", description: description.trim() },
      `Approved for ${p.tenant_name}: the bot can use it from its next reply.`);
    if (!done) setBusy(null);
  };
  const reject = async () => {
    const text = reason.trim();
    if (!text) {
      toast("Write a reason first: the client sees it.");
      reasonInput.current?.focus();
      return;
    }
    setBusy("reject");
    const done = await call("POST", `/api/review/photos/${p.id}/reject`, { reason: text }, `Rejected for ${p.tenant_name}.`);
    if (!done) setBusy(null);
  };

  return (
    <div className={cx("pf-section vf-photo", !pending && "vf-done")}>
      <Media kind={p.kind} src={`/api/review/photos/${p.id}/file`} missing="File no longer kept." />
      <div className="title vf-photo-title">
        <span>{p.tenant_name}</span>
        <span className="ow-badges">
          <Badge map={PHOTO_STATUS} status={p.status} />
          {p.kind === "video" && <span className="badge">video</span>}
        </span>
      </div>
      <div className="pf-note ow-meta vf-small">
        {[from, fmtDateTime(p.created_at), p.original_name, fmtBytes(p.bytes)].filter(Boolean).join(" · ")}
      </div>

      {p.replaces_item !== null && p.replaces_item !== undefined && (
        <div className="vf-replaces">
          <div className="muted vf-small">Replaces</div>
          {p.session_id ? (
            <Media kind="photo" src={`/api/sessions/${encodeURIComponent(p.session_id)}/media/${p.replaces_item}/file`}
                   missing="That photo is already gone." />
          ) : <div className="vf-missing">{`Photo #${p.replaces_item} (no account to show it from).`}</div>}
        </div>
      )}

      <div className="field vf-desc">
        <label htmlFor={`vf-desc-${p.id}`}>Description (what the bot sees)</label>
        <input id={`vf-desc-${p.id}`} type="text" maxLength={300} disabled={!pending} value={description}
               placeholder="What the bot sees, e.g. the room with the red sofa"
               onChange={(ev) => setDescription(ev.target.value)} />
      </div>

      {p.review_reason && (
        <div className={cx("pf-note", p.status === "rejected" ? "ow-reason" : "ow-meta")}>
          {(p.status === "rejected" ? "Reason given: " : "Note: ") + p.review_reason}
        </div>
      )}
      {p.reviewed_by && !pending && (
        <div className="pf-note ow-meta vf-small">{`${p.status} by ${p.reviewed_by} ${fmtDateTime(p.reviewed_at)}`}</div>
      )}

      {pending && <>
        <div className="pf-actions vf-photo-reason">
          <input ref={reasonInput} type="text" maxLength={500} placeholder="Reason to reject (the client sees it)"
                 value={reason} onChange={(ev) => setReason(ev.target.value)} />
        </div>
        <div className="pf-actions">
          <button type="button" className="btn small primary" disabled={busy === "approve"}
                  onClick={() => void approve()}>Approve</button>
          <button type="button" className="btn small warn" disabled={busy === "reject"}
                  onClick={() => void reject()}>Reject</button>
        </div>
      </>}
    </div>
  );
}

function Photos({ items, all, onAll, stamp, call }: {
  items: ReviewPhoto[]; all: boolean; onAll: (on: boolean) => void; stamp: number; call: Call;
}) {
  return (
    <>
      <p className="pf-note">{"Photos and videos the bot may send, waiting for you before it can use " +
        "them. The description is what the bot sees to pick the right file: correct it before approving if needed. " +
        "Approving a replacement removes the photo it replaces. Rejecting needs a reason: the client sees it."}</p>
      <ShowAll checked={all} label="Show all (also decided ones)" onChange={onAll} />
      {!items.length ? <div className="pf-note">{all ? "Nothing submitted yet." : "No photo is waiting."}</div> : (
        <div className="vf-grid">
          {items.map((p) => <PhotoCard key={`${stamp}-${p.id}`} p={p} call={call} />)}
        </div>
      )}
    </>
  );
}

/* -------------------------------------------------------------- businesses */

function OwnerRow({ o, call }: { o: NonNullable<Tenant["owners"]>[number]; call: Call }) {
  const { confirm } = useDialogs();
  const [open, setOpen] = useState(false);
  return (
    <div className="vf-owner">
      <div className="vf-owner-line">
        <span>{o.username}</span>
        <Badge map={STATUS} status={o.status} />
        <span className="spacer" />
        <button type="button" className="btn small" onClick={() => setOpen((v) => !v)}>Ask to verify again…</button>
      </div>
      <ReasonForm open={open} onClose={() => setOpen(false)} placeholder="Why (the client sees it)" label="Ask to verify"
                  onSubmit={async (reason) => {
                    if (!(await confirm(`Ask ${o.username} to send a new verification video? Every business of this login ` +
                      "pauses until you approve it."))) return null;
                    return call("POST", `/api/owners/${o.id}/request-verification`, { reason },
                      `${o.username} has to verify again; their businesses are paused.`);
                  }} />
    </div>
  );
}

function TenantCard({ t, call }: { t: Tenant; call: Call }) {
  const { confirm } = useDialogs();
  const [open, setOpen] = useState(false);
  const owners = t.owners || [];
  return (
    <div className="pf-section ow-card vf-tenant">
      <div className="title">
        <span>{t.name}</span>
        <span className="muted">{t.industry}</span>
        <span className="ow-badges">
          {t.held && <span className="badge escalated">Bot paused until verified</span>}
          {!!t.pending_photos && (
            <span className="badge paused">{`${t.pending_photos} photo${t.pending_photos === 1 ? "" : "s"} waiting`}</span>
          )}
        </span>
      </div>
      <div className="sf-sub muted">Client logins</div>
      {!owners.length && (
        <div className="pf-note ow-meta">No client login linked yet (link one under Client logins).</div>
      )}
      {owners.map((o) => <OwnerRow key={o.id} o={o} call={call} />)}
      <div className="pf-actions">
        <button type="button" className="btn small warn" onClick={() => setOpen((v) => !v)}>Re-check all photos…</button>
      </div>
      <ReasonForm open={open} onClose={() => setOpen(false)} placeholder="Why (kept in the audit log)"
                  label="Re-check all photos" onSubmit={async (reason) => {
                    if (!(await confirm(`Every photo and video of ${t.name} goes back into review and the bot stops using them now.`))) {
                      return null;
                    }
                    return call("POST", `/api/tenants/${t.id}/recheck-media`, { reason }, (r) => {
                      const moved = (r as { moved?: number } | null)?.moved;
                      return moved ? `${moved} file${moved === 1 ? "" : "s"} of ${t.name} moved back into review.`
                        : `${t.name} had no photo or video to re-check.`;
                    });
                  }} />
    </div>
  );
}

function IndustryRow({ i, call }: { i: ReviewIndustry; call: Call }) {
  const { confirm } = useDialogs();
  const [checked, setChecked] = useState(!!i.requires_review);
  const [busy, setBusy] = useState(false);
  const id = `vf-ind-${i.id}`;
  return (
    <div className="field check vf-industry">
      <input id={id} type="checkbox" checked={checked} disabled={busy} onChange={async (ev) => {
        const on = ev.target.checked;
        setChecked(on);
        if (on && !(await confirm(`Every business in ${i.name} is paused until its client sends an approved verification video.`))) {
          setChecked(false);
          return;
        }
        setBusy(true);
        const done = await call("PUT", `/api/review/industries/${i.id}`, { requires_review: on },
          on ? `${i.name}: review required.` : `${i.name}: review no longer required.`);
        if (!done) { setChecked(!on); setBusy(false); }
      }} />
      <label htmlFor={id}>{i.name}</label>
      <span className="muted vf-small">
        {`${i.tenants} business${i.tenants === 1 ? "" : "es"}` + (i.requires_review ? " · review required" : "")}
      </span>
    </div>
  );
}

function Businesses({ tenants, industries, stamp, call }: {
  tenants: Tenant[]; industries: ReviewIndustry[]; stamp: number; call: Call;
}) {
  return (
    <>
      <p className="pf-note">{"Businesses in an industry marked below, and any whose client you asked to " +
        "verify again. A paused bot receives messages but sends nothing until a client login linked to the " +
        "business has an approved video."}</p>
      {!tenants.length && (
        <div className="pf-note">{"No business is under review: no industry is marked below and " +
          "nobody was asked to verify again."}</div>
      )}
      {tenants.map((t) => <TenantCard key={`${stamp}-${t.id}`} t={t} call={call} />)}
      <div className="pf-section vf-industries">
        <div className="title">Industries</div>
        <p className="pf-note">{"Mark the escort market here. Clients of a marked industry must verify " +
          "their identity and age by video before their bot runs, and every photo they add waits for you here before " +
          "the bot may send it."}</p>
        {!industries.length && <div className="pf-note">No industry yet.</div>}
        {industries.map((i) => <IndustryRow key={`${stamp}-${i.id}`} i={i} call={call} />)}
      </div>
    </>
  );
}

/* -------------------------------------------------------------------- page */

const TAB_LABELS: [VerifyTab, string][] = [["videos", "Videos"], ["photos", "Photos"], ["businesses", "Businesses"]];

export function Verification({ initialTab }: { initialTab?: VerifyTab }) {
  const { setVerifyCount } = usePanel();
  const toast = useToast();
  const [tab, setTab] = useState<VerifyTab>(initialTab || "videos");
  const [allVideos, setAllVideos] = useState(false);
  const [allPhotos, setAllPhotos] = useState(false);
  // The newest summary, whichever tab asked for it: tab badges and the
  // businesses under review.
  const [summary, setSummary] = useState<ReviewSummary | null>(null);

  const key = tab === "videos" ? `videos:${allVideos}` : tab === "photos" ? `photos:${allPhotos}` : tab;
  const fetchView = useCallback(async (): Promise<Loaded> => {
    const stamp = ++loads;
    try {
      const load = async (): Promise<View> => {
        if (tab === "videos") {
          return { tab, items: await api<VerificationItem[]>("GET", "/api/review/verifications" + (allVideos ? "" : "?status=submitted")) };
        }
        if (tab === "photos") {
          return { tab, items: await api<ReviewPhoto[]>("GET", "/api/review/photos?status=" + (allPhotos ? "all" : "pending")) };
        }
        return { tab, items: await api<ReviewIndustry[]>("GET", "/api/review/industries") };
      };
      const [fresh, view] = await Promise.all([api<ReviewSummary>("GET", "/api/review/summary"), load()]);
      setSummary(fresh);
      const p = fresh?.pending || {};
      setVerifyCount((p.verifications || 0) + (p.photos || 0));
      return { key, stamp, view, error: null };
    } catch (err) {
      return { key, stamp, view: null, error: errorText(err) };
    }
  }, [key, tab, allVideos, allPhotos, setVerifyCount]);
  const { data: loaded, reload } = useLoader(fetchView);
  const current = loaded && loaded.key === key ? loaded : null;
  const view = current?.view ?? null;
  const stamp = current?.stamp ?? 0;

  const call: Call = async (method, path, body, done) => {
    try {
      const result = await api(method, path, body);
      const text = typeof done === "function" ? done(result) : done;
      if (text) toast(text, "info");
      await reload();
      return result ?? true;
    } catch (err) {
      toast(errorText(err));
      return null;
    }
  };

  const showTab = (next: VerifyTab) => {
    if (next === tab) void reload();
    else setTab(next);
  };

  const pending = summary?.pending || {};
  const tabs = TAB_LABELS.map(([k, label]) => {
    const n = k === "videos" ? pending.verifications : k === "photos" ? pending.photos : 0;
    return [k, <>{label}<span className="count-badge">{n ? String(n) : ""}</span></>] as const;
  });

  return (
    <div className="sv-w1080">
      <PageShell title="Verification" crumb="identity videos and client photos, checked by you">
        <div className="bk-scroll">
          <Tabs tabs={tabs} value={tab} onChange={showTab} />
          {!current && <div className="pf-note">Loading…</div>}
          {current?.error && <div className="pf-errors">{current.error}</div>}
          {view?.tab === "videos" && (
            <Videos items={view.items} all={allVideos} onAll={setAllVideos} stamp={stamp} call={call} />
          )}
          {view?.tab === "photos" && (
            <Photos items={view.items} all={allPhotos} onAll={setAllPhotos} stamp={stamp} call={call} />
          )}
          {view?.tab === "businesses" && (
            <Businesses tenants={summary?.tenants || []} industries={view.items} stamp={stamp} call={call} />
          )}
        </div>
      </PageShell>
    </div>
  );
}
