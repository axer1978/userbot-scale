"use client";

// Finetune → Context review (context_review_api.py), for the client picked
// above. How a reply is built: every piece the model is given for one of
// the account's replies, in the order it reads them, where each comes from
// and where to change it, what can stop a reply before the model is called
// and what happens to its text after, with warnings about the mistakes that
// cost before (an own text that keeps the industry's fixes out, writing
// samples that still show an old flow). No model call, no chat text.
// What the chats show: the model reads the account's real chats on the
// server (and other accounts' if ticked) and reports what clients ask, what
// gets the booking, where the bot goes wrong and the lines it repeats, next
// to exact counts from the database. A report only: nothing is applied; the
// admin reads it and edits the boxes. Kept apart from Finetune.tsx, which
// only mounts it.

import Link from "next/link";
import { useRouter } from "next/navigation";
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { useDialogs, useToast } from "@/components/feedback";
import { REASONS as UNANSWERED_REASONS } from "@/components/pages/Unanswered";
import { api, errorText } from "@/lib/api";
import { cx, fmtDateTime } from "@/lib/format";
import { channelName, usePanel } from "@/lib/panel";
import type {
  ContextEditLink, ContextPiece, ContextPieceSource, ContextReview, ContextReviewChatRef, ContextReviewChats,
  ContextReviewCounts, ContextReviewFix, ContextReviewItem, ContextReviewOutcome, ContextReviewReport,
  ContextReviewStatus, ContextStep, ContextView, PlatformTree,
} from "@/lib/types";
import { useLoader } from "@/lib/useLoader";
import "@/app/finetune.css";

const PATH = "/api/finetune";
// A running review is read again this often.
const POLL_MS = 5000;
const DAYS = [7, 14, 30, 60, 90];
const DEFAULT_DAYS = 30;
// Other accounts one review may add (POST /reviews refuses more).
const MAX_ALSO = 10;
const LIST_LIMIT = 20;
// Ticking accounts or changing the days asks for the chats again this long
// after the last change: each answer holds the masked text of every chat.
const PREVIEW_WAIT_MS = 400;

const STATUS: Record<ContextReviewStatus, string> = {
  running: "reading the chats…", done: "ready", failed: "failed",
};

const PARTS: [part: ContextPiece["part"], title: string, hint: string][] = [
  ["system", "System message", "Sent first, before the chat. A piece that is off is not in it."],
  ["history", "Conversation", "The chat so far."],
  ["closing", "Closing note", "Added after the chat: the last thing the model reads."],
];

// The badge colour of each source. An own text that replaces the
// industry's gets a stronger one of its own (PieceRow).
const SOURCE_CLASS: Record<ContextPieceSource, string> = {
  platform: "platform", industry: "industry", client: "client", "industry+client": "mixed", config: "config",
  system: "system", per_chat: "per-chat", empty: "empty",
};

// How a chat read by a review ended (context_review.py), in the order shown.
const OUTCOMES: [ContextReviewOutcome, string][] = [
  ["booked", "Booked"], ["booked_no_show", "Booked, did not come"], ["waiting", "Booking waiting for an answer"],
  ["not_booked", "Booking declined, cancelled or lapsed"], ["went_quiet", "Went quiet after our reply"],
  ["unanswered", "The client wrote last, no answer"], ["open", "Still going"],
];
const LOST = new Set<string>(["booked_no_show", "not_booked", "went_quiet", "unanswered"]);

const LINES: [keyof ContextReviewCounts["lines"], string][] = [
  ["client", "From the client"], ["bot", "Written by the bot"],
  ["bot_edited", "Written by the bot, edited by her before sending"], ["by_hand", "Typed by hand"],
  ["system", "Sent by the system (address, booking notices, reminders)"],
];

// The boxes a fix can name besides the prompt's sections.
const OTHER_BOXES: Record<string, string> = { addendum: "Additional notes", rules: "Operator rules", none: "Not covered" };

type Sections = { key: string; heading: string }[];

function plural(n: number, one: string): string {
  return `${n} ${one}${n === 1 ? "" : "s"}`;
}

function outcomeClass(outcome: string): string {
  return outcome === "booked" ? "ft-good" : LOST.has(outcome) ? "ft-bad" : "";
}

function boxLabel(box: string, sections: Sections): string {
  return sections.find((s) => s.key === box)?.heading ?? OTHER_BOXES[box] ?? box;
}

/** A plain click: Ctrl/⌘-click, shift and the middle button still open a new window. */
function inPlace(ev: React.MouseEvent): boolean {
  return !ev.ctrlKey && !ev.metaKey && !ev.shiftKey && !ev.altKey && ev.button === 0;
}

/* -------------------------------------------------------------- address */

// What the tab has open. Finetune.tsx keeps it in the address with its own
// tab and client (?tab=review&tenant=6&view=chats&review=3&days=60&also=7),
// so a reload, a link, or Back from a chat or the Clients page lands on it.
export type ReviewNav = { view: "built" | "chats"; review: number | null; days: number; also: number[] };

type Query = Pick<URLSearchParams, "get" | "getAll">;

export function readReviewNav(query: Query): ReviewNav {
  const review = query.get("review") ?? "";
  const days = Number(query.get("days"));
  const also = query.getAll("also").filter((id) => /^\d+$/.test(id)).map(Number);
  return {
    view: query.get("view") === "chats" ? "chats" : "built",
    review: /^\d+$/.test(review) ? Number(review) : null,
    days: DAYS.includes(days) ? days : DEFAULT_DAYS,
    also: [...new Set(also)].slice(0, MAX_ALSO),
  };
}

export function writeReviewNav(nav: ReviewNav, query: URLSearchParams) {
  query.set("view", nav.view);
  if (nav.review !== null) query.set("review", String(nav.review));
  if (nav.days !== DEFAULT_DAYS) query.set("days", String(nav.days));
  for (const id of nav.also) query.append("also", String(id));
}

/* ---------------------------------------------------------------- links */

type Where = {
  tenantId: number;
  tenantName: string;
  industryId: number;
  industryName: string;
  // The account's session: the Media and Style dialogs belong to one.
  sessionId: string | null;
  // Switches the Finetune page to one of its tabs; false when it has none by that name.
  onTab: (tab: string) => boolean;
};

const WhereContext = createContext<Where | null>(null);

function useWhere(): Where {
  const where = useContext(WhereContext);
  if (!where) throw new Error("useWhere() outside <ContextReviewTab>");
  return where;
}

const DIALOGS = ["media", "style"] as const;

/**
 * A link to where something is changed. The Media and Style dialogs have no
 * address of their own, they belong to whichever account is open: so
 * "/?modal=media" opens the account (session=…, or this client's) in the top
 * bar and then the dialog, right here. A link to another tab of this page for
 * this client switches the tab instead of loading the page again.
 */
function EditLinkView({ edit }: { edit: ContextEditLink }) {
  const where = useWhere();
  const { selectSession, setModal } = usePanel();
  const [opening, setOpening] = useState(false);
  const url = new URL(edit.href, "http://panel.invalid");
  const modal = url.pathname === "/" ? DIALOGS.find((d) => d === url.searchParams.get("modal")) : undefined;
  if (modal) {
    const session = url.searchParams.get("session") || where.sessionId;
    if (!session) return <span className="muted">{edit.label} (no account connected)</span>;
    // The dialogs read the account's chats and config as they open: load them first.
    const open = async () => {
      setOpening(true);
      try {
        await selectSession(session);
        setModal(modal);
      } finally { setOpening(false); }
    };
    return (
      <button type="button" className="ft-link" disabled={opening} onClick={open}>
        {opening ? `${edit.label}…` : edit.label}</button>
    );
  }
  const tenant = url.searchParams.get("tenant");
  const tab = url.pathname === "/finetune" && (tenant === null || Number(tenant) === where.tenantId)
    ? url.searchParams.get("tab") : null;
  return (
    <Link href={edit.href} onClick={(ev) => { if (tab && inPlace(ev) && where.onTab(tab)) ev.preventDefault(); }}>
      {edit.label}</Link>
  );
}

/** Opens one chat in Conversations: its account first, then the chat. */
function useOpenChat() {
  const { selectSession, selectChat } = usePanel();
  const router = useRouter();
  return useCallback((sessionId: string, chatId: number) => {
    // selectSession switches the account at once and loads the rest after,
    // so the chat opens in the right one.
    void selectSession(sessionId);
    void selectChat(chatId);
    router.push("/");
  }, [selectSession, selectChat, router]);
}

// ref ("C4") → the chat of the open review.
const RefsContext = createContext<Map<string, ContextReviewChatRef>>(new Map());

/**
 * Chat refs as links to the chats; a ref the review does not know stays
 * text. A plain click opens the chat in this window; the address
 * (/?session=…&chat=…, read once by lib/panel.tsx) serves a new one.
 */
function Refs({ refs }: { refs: string[] }) {
  const chats = useContext(RefsContext);
  const openChat = useOpenChat();
  if (!refs.length) return null;
  return (
    <span className="ft-refs">
      {refs.map((ref) => {
        const c = chats.get(ref);
        return c ? (
          <a key={ref} className="ft-ref" title={`${c.account} · ${c.outcome_label}: open the chat`}
             href={`/?session=${encodeURIComponent(c.session_id)}&chat=${c.chat_id}`}
             onClick={(ev) => { if (inPlace(ev)) { ev.preventDefault(); openChat(c.session_id, c.chat_id); } }}>
            {ref}</a>
        ) : <span key={ref} className="muted">{ref}</span>;
      })}
    </span>
  );
}

function CopyButton({ text }: { text: string }) {
  const toast = useToast();
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      toast("Copied.", "info");
    } catch { toast("Could not copy here: select the line and copy it by hand."); }
  };
  return <button type="button" className="btn small" onClick={copy}>Copy</button>;
}

function BoxBadge({ box, sections }: { box: string; sections: Sections }) {
  return <span className={cx("badge", box === "none" && "ft-bad")}>{boxLabel(box, sections)}</span>;
}

/** Where to edit a box: this client's Prompt tab, or the industry's template for every client in it. */
function BoxLinks({ box }: { box: string }) {
  const w = useWhere();
  if (box === "none") return null;
  if (box === "rules") return <EditLinkView edit={{ label: "Rules", href: `/finetune?tab=rules&tenant=${w.tenantId}` }} />;
  const own = <Link href={`/clients?node=tenant-${w.tenantId}&tab=prompt`}>Edit for {w.tenantName}</Link>;
  if (box === "addendum") return own;
  return <>{own} · <Link href={`/clients?node=industry-${w.industryId}&tab=template`}>for all of {w.industryName}</Link></>;
}

/** A line to add to a box or change in it, to copy in by hand. */
function Fix({ fix, sections }: { fix: ContextReviewFix; sections: Sections }) {
  return (
    <div className="ft-cr-fix">
      <BoxBadge box={fix.box} sections={sections} />
      <span className="ft-cr-fix-line">{fix.line}</span>
      <span className="ft-cr-fix-actions">
        <CopyButton text={fix.line} />
        <span className="muted"><BoxLinks box={fix.box} /></span>
      </span>
    </div>
  );
}

/* ------------------------------------------------- how a reply is built */

function Steps({ steps }: { steps: ContextStep[] }) {
  if (!steps.length) return <p className="pf-note">None.</p>;
  return (
    <div className="ft-shots">
      {steps.map((s) => (
        <div key={s.key} className={cx("ft-cr-step", !s.active && "ft-off")}>
          <span className={cx("badge", s.active && "ft-good")}>{s.active ? "on" : "off"}</span>
          <div className="grow">
            <b>{s.title}</b>{s.detail && <span className="muted"> · {s.detail}</span>}
            {s.edit && <div className="ft-cr-edit"><EditLinkView edit={s.edit} /></div>}
          </div>
        </div>
      ))}
    </div>
  );
}

function PieceRow({ n, piece }: { n: number; piece: ContextPiece }) {
  const [open, setOpen] = useState(false);
  const inherited = piece.inherited?.trim() ? piece.inherited : "";
  // The account's own text in place of the industry's: a fix to the
  // industry's box never reaches this account.
  const replaces = piece.source === "client" && !!inherited;
  const size = piece.chars ? `${piece.chars.toLocaleString()} characters`
    : piece.enabled ? "size depends on the chat" : "";
  return (
    <div className={cx("ft-cr-piece", !piece.enabled && "ft-off")}>
      <div className="ft-cr-piece-head">
        <span className="ft-cr-num">{n}</span>
        <div className="grow">
          <div className="ft-example-badges">
            <b>{piece.title}</b>
            <span className={cx("src", SOURCE_CLASS[piece.source], replaces && "replaces")}>{piece.source_label}</span>
            {!piece.enabled && <span className="badge">off</span>}
            {size && <span className="muted">{size}</span>}
          </div>
          {piece.note && <div className="ft-example-note">{piece.note}</div>}
          {piece.edit && <div className="ft-cr-edit"><EditLinkView edit={piece.edit} /></div>}
        </div>
        {(piece.text || inherited) && (
          <button type="button" className="btn small" aria-expanded={open} onClick={() => setOpen(!open)}>
            {open ? "Hide" : "Text"}</button>
        )}
      </div>
      {open && (inherited ? (
        <div className="ft-cols">
          <div>
            <div className="rv-label">What the model reads</div>
            <pre className="pf-rendered">{piece.text || "(empty)"}</pre>
          </div>
          <div>
            <div className="rv-label">{replaces ? "The industry's text, replaced: not read" : "The industry's text"}</div>
            <pre className="pf-rendered">{inherited}</pre>
          </div>
        </div>
      ) : <pre className="pf-rendered">{piece.text}</pre>)}
    </div>
  );
}

function HowBuilt() {
  const { tenantId } = useWhere();
  const fetchView = useCallback(() => api<ContextView>("GET", `${PATH}/context?tenant_id=${tenantId}`), [tenantId]);
  const { data: v, error } = useLoader(fetchView);
  if (!v) return error ? <div className="pf-errors">{error}</div> : <div className="empty">Loading…</div>;

  // Warnings first, then notes, each in the server's order.
  const warnings = [...v.warnings.filter((w) => w.level === "warn"), ...v.warnings.filter((w) => w.level !== "warn")];
  const numbered = v.pieces.map((piece, i) => ({ piece, n: i + 1 }));

  return (
    <>
      <p className="pf-note">Every piece the model is given for a reply of {v.account}, in the order it reads them:
        where it comes from, how big it is and where to change it. Nothing here calls the model or reads a chat.</p>
      {warnings.length > 0 && (
        <div className="ft-cr-warnings">
          {warnings.map((w, i) => (
            <div key={`${w.level}-${i}`} className={cx("ft-cr-warning", w.level)}>
              <span className={cx("badge", w.level === "warn" ? "ft-bad" : "ft-plain")}>
                {w.level === "warn" ? "check" : "note"}</span>
              <div className="grow">{w.text}{w.edit && <> <EditLinkView edit={w.edit} /></>}</div>
            </div>
          ))}
        </div>
      )}
      <div className="rv-top ft-cr-head">
        <b>{v.account}</b>
        <span className={cx("badge channel", v.network === "whatsapp" ? "wa" : "tg")}>{channelName(v.network)}</span>
        <span>{v.industry.name} v{v.industry.template_version}</span>
        <code title="The prompt's version tag">{v.version_tag}</code>
        {v.pinned !== null && <span className="warn-note">pinned to version {v.pinned}</span>}
      </div>
      <p className="pf-note">System message: {v.size.system_chars.toLocaleString()} characters (about{" "}
        {v.size.approx_tokens.toLocaleString()} tokens) as it starts for every reply, before the pieces made for each
        chat. Then the last {plural(v.size.history_messages, "message")} of the chat.</p>

      <div className="sub">Before the model</div>
      <p className="pf-note">What can stop or delay a reply before the model is called, as set for this account.</p>
      <Steps steps={v.before} />

      {PARTS.map(([part, title, hint]) => {
        const list = numbered.filter((p) => p.piece.part === part);
        if (!list.length) return null;
        const off = list.filter((p) => !p.piece.enabled).length;
        return (
          <div key={part}>
            <div className="sub">{title} ({list.length}{off ? `, ${off} off` : ""})</div>
            <p className="pf-note">{hint}</p>
            <div className="ft-shots">
              {list.map(({ piece, n }) => <PieceRow key={`${piece.key}-${n}`} n={n} piece={piece} />)}
            </div>
          </div>
        );
      })}

      <div className="sub">After the model</div>
      <p className="pf-note">What happens to the model&apos;s text before it goes out.</p>
      <Steps steps={v.after} />

      <details className="ft-history">
        <summary className="sub">The whole system message ({v.system_text.length.toLocaleString()} characters)</summary>
        <pre className="pf-rendered">{v.system_text}</pre>
      </details>
    </>
  );
}

/* ------------------------------------------------ what the chats show */

function ChatsPreview({ also, days }: { also: number[]; days: number }) {
  const key = `${days}:${also.join(",")}`;
  // What is asked for: the accounts and days as they were a moment ago, not
  // on every click (PREVIEW_WAIT_MS).
  const [asked, setAsked] = useState({ key, also, days });
  useEffect(() => {
    if (key === asked.key) return;
    const timer = setTimeout(() => setAsked({ key, also, days }), PREVIEW_WAIT_MS);
    return () => clearTimeout(timer);
  }, [key, asked.key, also, days]);

  return (
    <div className="pf-section">
      <div className="title">The chats it would read</div>
      <PreviewChats asked={asked} current={key} />
    </div>
  );
}

// The answer says which accounts and days it is for: counts of others are
// never shown, "Loading…" is while the new ones come.
function PreviewChats({ asked, current }: { asked: { key: string; also: number[]; days: number }; current: string }) {
  const { tenantId } = useWhere();
  const fetchChats = useCallback(async () => {
    const query = new URLSearchParams({ tenant_id: String(tenantId), days: String(asked.days) });
    for (const id of asked.also) query.append("also", String(id));
    return { key: asked.key, chats: await api<ContextReviewChats>("GET", `${PATH}/review-chats?${query}`) };
  }, [tenantId, asked]);
  const { data: loaded, error } = useLoader(fetchChats);
  const [open, setOpen] = useState<string | null>(null);
  const data = loaded?.key === current ? loaded.chats : null;
  if (!data) {
    return error && asked.key === current ? <div className="pf-errors">{error}</div> : <div className="empty">Loading…</div>;
  }

  const read = data.chats.filter((c) => c.picked);
  const chars = read.reduce((n, c) => n + c.chars, 0);
  return (
    <>
      {error && <div className="pf-errors">{error}</div>}
      <p className="pf-note ft-cr-preview-note">A chat is in when the client wrote in it in the last {data.days} days.
        The ones that fit one model call are read, numbered C1, C2… in the order the model gets them: chats that
        booked and chats that did not in turns, the newest first. Names, numbers, the address and the way in are
        masked.</p>
      {data.accounts.length > 0 && (
        <p className="pf-note">{data.accounts.map((a) => `${a.name}: ${plural(a.chats, "chat")}, ${a.picked} read`)
          .join(" · ")}</p>
      )}
      {!data.chats.length && <div className="empty">No chat with a message from a client in the last {data.days} days.</div>}
      {data.chats.length > 0 && (
        <div className="ft-shots">
          {data.chats.map((c) => (
            <div key={c.key}>
              <div className={cx("ft-example", !c.picked && "ft-off")}>
                <span className={cx("badge", c.ref && "ft-plain")}>{c.ref ?? "not read"}</span>
                <div className="grow">
                  <b>{c.display_name || `Chat ${c.chat_id}`}</b>{" "}
                  <span className="badge">{c.account}</span>{" "}
                  <span className={cx("badge", outcomeClass(c.outcome))}>{c.outcome_label}</span>{" "}
                  <span className="muted">{plural(c.messages, "message")} · {c.chars.toLocaleString()} characters</span>
                </div>
                {/* The server sends the text of the chats it reads only. */}
                {c.text ? (
                  <button type="button" className="btn small" onClick={() => setOpen(open === c.key ? null : c.key)}>
                    {open === c.key ? "Hide" : "What the model reads"}</button>
                ) : <span className="muted ft-cr-unread">{c.picked ? "no text" : "not read: over the limit"}</span>}
              </div>
              {open === c.key && c.text && <pre className="pf-rendered">{c.text}</pre>}
            </div>
          ))}
        </div>
      )}
      <div className="pf-note ft-count">{read.length} of {data.chats.length} chats read · {chars.toLocaleString()} /{" "}
        {data.limits.chars.toLocaleString()} characters · at most {data.limits.chats} chats</div>
    </>
  );
}

function ReviewForm({ tree, also, onAlso, days, onDays, running, onStarted, onRefresh }: {
  tree: PlatformTree; also: number[]; onAlso: (also: number[]) => void; days: number; onDays: (days: number) => void;
  running: boolean; onStarted: (review: ContextReview) => void; onRefresh: () => void;
}) {
  const { tenantId, tenantName, industryId } = useWhere();
  const toast = useToast();
  const [preview, setPreview] = useState(false);
  const [busy, setBusy] = useState(false);
  // The other accounts, the same industry's first: they share one tuning.
  const others = useMemo(() => {
    const rest = tree.tenants.filter((t) => t.id !== tenantId);
    return [...rest.filter((t) => t.industry_id === industryId), ...rest.filter((t) => t.industry_id !== industryId)];
  }, [tree, tenantId, industryId]);
  const industryName = (id: number) => tree.industries.find((i) => i.id === id)?.name ?? "";
  const full = also.length >= MAX_ALSO;
  const toggle = (id: number) => onAlso(also.includes(id) ? also.filter((x) => x !== id) : [...also, id]);

  const start = async () => {
    setBusy(true);
    try {
      onStarted(await api<ContextReview>("POST", `${PATH}/reviews`, { tenant_id: tenantId, also_tenant_ids: also, days }));
    } catch (err) {
      toast(errorText(err));
      // A review of this client already running (409): the list shows it and follows it.
      onRefresh();
    } finally { setBusy(false); }
  };

  return (
    <>
      <p className="pf-note">The model reads the real chats of {tenantName} on the server, the bot&apos;s replies and
        the ones typed by hand, and reports what clients ask and how each is answered, what gets the booking, where
        the bot goes wrong and the lines it repeats. The numbers next to it are counted from the database, not by the
        model. Nothing changes in the prompt: read it, then edit the boxes.</p>
      {others.length > 0 && (
        <div className="ft-accounts" role="group" aria-label="Also read the chats of">
          <span className="muted">Also read the chats of{full ? ` (at most ${MAX_ALSO})` : ""}:</span>
          {others.map((t) => (
            <label key={t.id} className="check">
              <input type="checkbox" checked={also.includes(t.id)} disabled={full && !also.includes(t.id)}
                     onChange={() => toggle(t.id)} /> {t.name}
              {industryName(t.industry_id) && <span className="muted"> ({industryName(t.industry_id)})</span>}
            </label>
          ))}
        </div>
      )}
      <div className="pf-actions">
        <label className="muted" htmlFor="ft-cr-days">Chats of the last</label>
        <select id="ft-cr-days" value={String(days)} onChange={(ev) => onDays(Number(ev.target.value))}>
          {DAYS.map((d) => <option key={d} value={String(d)}>{d} days</option>)}
        </select>
        <span className="grow" />
        <button type="button" className="btn" aria-expanded={preview} onClick={() => setPreview(!preview)}>
          {preview ? "Hide the chats" : "Show the chats it would read"}</button>
        <button type="button" className="btn primary" disabled={busy || running} onClick={start}>
          {busy ? "Starting…" : running ? "A review is running" : "Start review"}</button>
      </div>
      <p className="pf-note ft-cr-cost">One DeepSeek call on the platform key (a few cents). The chats are read on the
        server; nothing is applied.</p>
      {preview && <ChatsPreview also={also} days={days} />}
    </>
  );
}

function accountsOf(r: ContextReviewItem, names: Map<number, string>): string {
  return [r.tenant_id, ...r.also_tenant_ids].map((id) => names.get(id) ?? `client ${id}`).join(" + ");
}

function ReviewList({ reviews, names, onOpen }: {
  reviews: ContextReviewItem[]; names: Map<number, string>; onOpen: (id: number) => void;
}) {
  if (!reviews.length) return <div className="empty">No reviews of this client yet.</div>;
  return (
    <>
      {reviews.map((r) => (
        <div key={r.id} className={cx("bk-row", `ft-row-${r.status}`)}>
          <div className="bk-head" role="button" tabIndex={0} onClick={() => onOpen(r.id)}
               onKeyDown={(ev) => { if (ev.key === "Enter") onOpen(r.id); }}>
            <span className={cx("bk-state", `ft-${r.status}`)}>{STATUS[r.status]}</span>
            <span className="bk-time">{fmtDateTime(r.created_at)}</span>
            <span>Review {r.id}</span>
            <span className="muted">{accountsOf(r, names)} · last {r.days} days · {plural(r.chats_read, "chat")} read</span>
          </div>
          {r.status === "failed" && r.error && <div className="pf-errors ft-cr-row-err">{r.error}</div>}
        </div>
      ))}
    </>
  );
}

/* ----------------------------------------------------------- one review */

type Quality = ContextReviewCounts["quality"][number];
type Account = ContextReviewCounts["chats"]["by_account"][number];
type NumberRow = [label: string, value: (a: Account) => number | string];

// Exact, from the database: per account over every chat of the window, and
// how the chats read ended and who wrote their messages.
function Numbers({ counts, chats }: { counts: ContextReviewCounts; chats: ContextReviewChatRef[] }) {
  const accounts = counts.chats.by_account;
  const quality = new Map(counts.quality.map((q) => [q.tenant_id, q]));
  const reasons = [...new Set(counts.quality.flatMap((q) => Object.keys(q.unanswered_by_reason)))].sort();
  const of = (pick: (q: Quality) => number) => (a: Account) => {
    const q = quality.get(a.tenant_id);
    return q ? pick(q) : "—";
  };
  const groups: [string, NumberRow[]][] = [
    ["Chats", [
      ["With a message from a client", (a) => a.in_window],
      ["Read by this review", (a) => a.read],
    ]],
    ["The bot", [["Replies it wrote", of((q) => q.replies_written)]]],
    ["Drafts waiting for approval", [
      ["Approved as written", of((q) => q.drafts.approved - q.drafts.edited)],
      ["Edited, then approved", of((q) => q.drafts.edited)],
      ["Rejected", of((q) => q.drafts.rejected)],
    ]],
    ["People", [["Chats taken over", of((q) => q.takeovers)]]],
    ["Unanswered", [
      ["All", of((q) => Object.values(q.unanswered_by_reason).reduce((n, x) => n + x, 0))],
      ...reasons.map((r): NumberRow => [UNANSWERED_REASONS[r] || r, of((q) => q.unanswered_by_reason[r] ?? 0)]),
    ]],
  ];
  const w = counts.window;

  return (
    <>
      <p className="pf-note">{fmtDateTime(w.since)} to {fmtDateTime(w.until)} ({plural(w.days, "day")}): per account
        over every chat of the window. How the chats ended and who wrote their messages is over the{" "}
        {plural(counts.chats.read, "chat")} read.</p>
      {/* One column per account, up to 11: it scrolls rather than push the page wider. */}
      <div className="ft-cr-scroll">
        <table className="cfg-table ft-quality">
          <tbody>
            <tr>
              <td />
              {accounts.map((a) => <td key={a.tenant_id} className="muted num ft-cr-name">{a.name}</td>)}
            </tr>
            {groups.map(([group, rows]) => [
              <tr key={group} className="cfg-group"><td colSpan={accounts.length + 1}>{group}</td></tr>,
              ...rows.map(([label, value]) => (
                <tr key={`${group}-${label}`}>
                  <td>{label}</td>
                  {accounts.map((a) => <td key={a.tenant_id} className="num">{value(a)}</td>)}
                </tr>
              )),
            ])}
          </tbody>
        </table>
      </div>
      <div className="ft-cols">
        <div>
          <div className="rv-label">How the chats read ended</div>
          <table className="cfg-table ft-cr-counts">
            <tbody>{OUTCOMES.map(([key, label]) => (
              <tr key={key}><td>{label}</td><td className="num">{counts.outcomes[key] ?? 0}</td></tr>
            ))}</tbody>
          </table>
        </div>
        <div>
          <div className="rv-label">Their messages, by who wrote them</div>
          <table className="cfg-table ft-cr-counts">
            <tbody>{LINES.map(([key, label]) => (
              <tr key={key}><td>{label}</td><td className="num">{counts.lines[key] ?? 0}</td></tr>
            ))}</tbody>
          </table>
        </div>
      </div>
      {chats.length > 0 && (
        <details className="ft-raw">
          <summary className="muted">The {plural(chats.length, "chat")} it read</summary>
          <ul className="ft-list">
            {chats.map((c) => <li key={c.ref}><Refs refs={[c.ref]} /> {c.account} · {c.outcome_label}</li>)}
          </ul>
        </details>
      )}
    </>
  );
}

function Questions({ questions, sections }: { questions: ContextReviewReport["questions"]; sections: Sections }) {
  if (!questions.length) return <p className="pf-note">None found.</p>;
  return (
    <table className="cfg-table ft-cr-table">
      <thead>
        <tr>
          <th>Question</th><th>Chats</th><th>Her answer, by hand</th><th>The bot&apos;s answer</th><th>Covered by</th>
          <th>Problem, and a line for the box</th>
        </tr>
      </thead>
      <tbody>
        {questions.map((q, i) => (
          <tr key={i}>
            <td data-label="Question">
              <b>{q.question}</b>
              {q.wordings.length > 0 && (
                <ul className="ft-cr-wordings">{q.wordings.map((w, j) => <li key={j}>&ldquo;{w}&rdquo;</li>)}</ul>
              )}
            </td>
            <td data-label="Chats"><b>{q.chat_count}</b> <Refs refs={q.chats} /></td>
            <td data-label="Her answer, by hand">{q.her_answer || <span className="muted">never answered by hand</span>}</td>
            <td data-label="The bot's answer">{q.bot_answer || <span className="muted">—</span>}</td>
            <td data-label="Covered by"><BoxBadge box={q.covered_by} sections={sections} /></td>
            <td data-label="Problem, and a line for the box">
              {q.problem}
              {q.suggested && <Fix fix={q.suggested} sections={sections} />}
              {!q.problem && !q.suggested && <span className="muted">—</span>}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Points({ points }: { points: ContextReviewReport["booking"]["works"] }) {
  if (!points.length) return <p className="pf-note">Nothing found.</p>;
  return <ul className="ft-list">{points.map((p, i) => <li key={i}>{p.point} <Refs refs={p.chats} /></li>)}</ul>;
}

function ReviewBody({ review: r, names }: { review: ContextReview; names: Map<number, string> }) {
  const refs = useMemo(() => new Map(r.chats.map((c) => [c.ref, c])), [r.chats]);
  const report = r.report;
  const counts = r.counts;
  const sections = r.sections ?? [];

  return (
    <RefsContext.Provider value={refs}>
      <div className="rv-top">
        <span className={cx("bk-state", `ft-${r.status}`)}>{STATUS[r.status]}</span>
        <span>Review {r.id}</span>
        <span className="muted">{fmtDateTime(r.created_at)} · {accountsOf(r, names)} · last {r.days} days ·{" "}
          {plural(r.chats_read, "chat")} read · {r.model}</span>
      </div>
      {r.status === "running" && (
        <div className="empty ft-cr-running">The model is reading the chats. This can take a few minutes.</div>
      )}
      {r.error && <div className="pf-errors">{r.error}</div>}
      {r.raw_output && (
        <details className="ft-raw"><summary className="muted">The model&apos;s full answer</summary>
          <pre className="pf-rendered">{r.raw_output}</pre></details>
      )}

      {report?.summary && <>
        <div className="sub">Summary</div>
        <p className="ft-cr-summary">{report.summary}</p>
      </>}

      {counts && <>
        <div className="sub">The numbers</div>
        <Numbers counts={counts} chats={r.chats} />
      </>}

      {report && <>
        <div className="sub">What clients ask ({report.questions.length})</div>
        <p className="pf-note">The questions asked most, most chats first. A chat ref opens that chat. Copy a line into
          the box it names: nothing is applied from here.</p>
        <Questions questions={report.questions} sections={sections} />

        <div className="sub">What gets the booking</div>
        <div className="ft-cols">
          <div><div className="rv-label">What the booked chats did</div><Points points={report.booking.works} /></div>
          <div><div className="rv-label">What the lost ones did</div><Points points={report.booking.loses} /></div>
        </div>
        {report.booking.speed && <p className="pf-note ft-cr-speed"><b>Speed:</b> {report.booking.speed}</p>}

        <div className="sub">Where the bot goes wrong ({report.wrong.length})</div>
        {report.wrong.length > 0 ? (
          <div className="ft-shots">
            {report.wrong.map((w, i) => (
              <div key={i} className="ft-rule">
                <div className="grow">
                  <div><b>{w.what}</b> <Refs refs={w.chats} /></div>
                  {w.why && <div className="ft-example-note">{w.why}</div>}
                  {w.fix && <Fix fix={w.fix} sections={sections} />}
                </div>
              </div>
            ))}
          </div>
        ) : <p className="pf-note">Nothing found.</p>}
      </>}

      {(counts || report) && <>
        <div className="sub">Repeated bot lines</div>
        <p className="pf-note">Lines the bot sent in 3 or more chats, the same give or take capitals, punctuation, emojis
          and numbers: counted in code over every chat of the accounts in the window. The same text to many people is
          how WhatsApp flags a number. The prompt&apos;s own fixed texts, like the rates, are left out.</p>
        {counts && (counts.phrases.length > 0 ? (
          <table className="cfg-table ft-cr-phrases">
            <thead><tr><th>Line</th><th>Chats</th><th>Times</th></tr></thead>
            <tbody>
              {counts.phrases.map((p, i) => (
                <tr key={i}><td>{p.text}</td><td className="num">{p.chats}</td><td className="num">{p.times}</td></tr>
              ))}
            </tbody>
          </table>
        ) : <p className="pf-note">None.</p>)}
        {report && report.phrases.length > 0 && <>
          <div className="rv-label ft-cr-label">The model&apos;s notes</div>
          <ul className="ft-list">
            {report.phrases.map((p, i) => (
              <li key={i}>&ldquo;{p.text}&rdquo; <Refs refs={p.chats} />{p.note && <div className="muted">{p.note}</div>}</li>
            ))}
          </ul>
        </>}
      </>}

      {report && <>
        <div className="sub">Box problems ({report.boxes.length})</div>
        {report.boxes.length > 0 ? (
          <div className="ft-shots">
            {report.boxes.map((b, i) => (
              <div key={i} className="ft-rule">
                <div className="grow">
                  <div className="ft-example-badges">
                    {b.boxes.map((box) => <BoxBadge key={box} box={box} sections={sections} />)}
                  </div>
                  <div className="ft-rule-text ft-cr-box-problem">{b.problem}</div>
                  {b.fix && <div className="ft-example-note">Fix: {b.fix}</div>}
                </div>
              </div>
            ))}
          </div>
        ) : <p className="pf-note">None found: the boxes agree with each other and with the chats.</p>}
      </>}

      {r.errors.length > 0 && (
        <details className="ft-raw">
          <summary className="muted">What the check left out of the model&apos;s answer ({r.errors.length})</summary>
          <ul className="ft-list">{r.errors.map((e, i) => <li key={i}>{e}</li>)}</ul>
        </details>
      )}
    </RefsContext.Provider>
  );
}

function ReviewView({ id, names, onBack, onChanged }: {
  id: number; names: Map<number, string>; onBack: () => void; onChanged: () => void;
}) {
  const toast = useToast();
  const { confirm } = useDialogs();
  const fetchReview = useCallback(() => api<ContextReview>("GET", `${PATH}/reviews/${id}`), [id]);
  const { data: review, error, reload } = useLoader(fetchReview);
  const [deleting, setDeleting] = useState(false);
  const running = review?.status === "running";

  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => { void reload(); }, POLL_MS);
    // When it stops running, the list behind "All reviews" needs the new status.
    return () => { clearInterval(timer); onChanged(); };
  }, [running, reload, onChanged]);

  const remove = async () => {
    if (!(await confirm(`Delete review ${id}? Only the report goes; the chats and the prompt stay as they are.`))) return;
    setDeleting(true);
    try {
      await api("DELETE", `${PATH}/reviews/${id}`);
      onBack();
    } catch (err) { toast(errorText(err)); setDeleting(false); }
  };

  return (
    <>
      <div className="pf-actions ft-back"><button type="button" className="btn small" onClick={onBack}>← All reviews</button></div>
      {error && <div className="pf-errors">{error}</div>}
      {!review && !error && <div className="empty">Loading…</div>}
      {review && <ReviewBody review={review} names={names} />}
      {review && (
        <div className="pf-actions">
          <span className="muted grow">{running ? "A running review can be deleted once it ends."
            : "Nothing in a review is applied; deleting it removes only the report."}</span>
          <button type="button" className="btn" disabled={running || deleting} onClick={remove}>
            {deleting ? "Deleting…" : "Delete"}</button>
        </div>
      )}
    </>
  );
}

type OnNav = (patch: Partial<ReviewNav>) => void;

function ChatsShow({ tree, nav, onNav }: { tree: PlatformTree; nav: ReviewNav; onNav: OnNav }) {
  const { tenantId } = useWhere();
  const openId = nav.review;
  const fetchList = useCallback(
    () => api<ContextReviewItem[]>("GET", `${PATH}/reviews?tenant_id=${tenantId}&limit=${LIST_LIMIT}`), [tenantId]);
  const { data: reviews, error, reload } = useLoader(fetchList);
  // Stable, so ReviewView can call it when a review stops running.
  const refresh = useCallback(() => { void reload(); }, [reload]);
  const names = useMemo(() => new Map(tree.tenants.map((t) => [t.id, t.name])), [tree]);
  const running = !!reviews?.some((r) => r.status === "running");

  // A review running in the list (started before a reload, or in another
  // window) shows when it ends here too.
  useEffect(() => {
    if (!running || openId !== null) return;
    const timer = setInterval(refresh, POLL_MS);
    return () => clearInterval(timer);
  }, [running, openId, refresh]);

  if (openId !== null) {
    return <ReviewView id={openId} names={names} onBack={() => { onNav({ review: null }); refresh(); }} onChanged={refresh} />;
  }
  return (
    <>
      <ReviewForm tree={tree} also={nav.also} onAlso={(also) => onNav({ also })} days={nav.days}
                  onDays={(days) => onNav({ days })} running={running} onRefresh={refresh}
                  onStarted={(review) => { refresh(); onNav({ review: review.id }); }} />
      <div className="sub">Past reviews{reviews?.length ? ` (${reviews.length})` : ""}</div>
      {error && <div className="pf-errors">{error}</div>}
      {!reviews && !error && <div className="empty">Loading…</div>}
      {reviews && <ReviewList reviews={reviews} names={names} onOpen={(id) => onNav({ review: id })} />}
    </>
  );
}

/* ------------------------------------------------------------------ tab */

// What is open (nav) lives in Finetune.tsx, which keeps it in the address:
// switching to Rules and back, or the view, keeps an open review open.
export function ContextReviewTab({ tenantId, tenantName, industryId, industryName, sessionId, tree, onTab, nav, onNav }: {
  tenantId: number; tenantName: string; industryId: number; industryName: string; sessionId: string | null;
  tree: PlatformTree; onTab: (tab: string) => boolean; nav: ReviewNav; onNav: OnNav;
}) {
  const where = useMemo<Where>(() => ({ tenantId, tenantName, industryId, industryName, sessionId, onTab }),
    [tenantId, tenantName, industryId, industryName, sessionId, onTab]);

  return (
    <WhereContext.Provider value={where}>
      <div className="ft-cr">
        <div className="ft-source" role="radiogroup" aria-label="What to show">
          <label className="check"><input type="radio" name="ft-cr-view" checked={nav.view === "built"}
                                          onChange={() => onNav({ view: "built" })} /> How a reply is built</label>
          <label className="check"><input type="radio" name="ft-cr-view" checked={nav.view === "chats"}
                                          onChange={() => onNav({ view: "chats" })} /> What the chats show</label>
        </div>
        {nav.view === "built" ? <HowBuilt /> : <ChatsShow tree={tree} nav={nav} onNav={onNav} />}
      </div>
    </WhereContext.Provider>
  );
}
