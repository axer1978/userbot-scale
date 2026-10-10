"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { cx } from "@/lib/format";
import { OwnerApiError, ownerApi } from "@/lib/owner/api";
import {
  BOOKING_STATES, fmtDay, fmtDayKey, fmtShortDay, fmtTime, fmtWhen, percent, QUEUE_REASONS,
} from "@/lib/owner/format";
import type { Booking, Dashboard as DashboardData, Overview, QueueItem } from "@/lib/owner/types";
import { useMe, useOwner } from "./context";
import { PhotosSection } from "./Photos";
import { Section, Segmented, StatusLine, Tile, s } from "./parts";
import { SettingsSheet } from "./Settings";
import { useOwnerData } from "./useOwnerData";

type View = "all" | number | null;

const VIEW_KEY = "owner.view";

function readView(): string | null {
  try { return localStorage.getItem(VIEW_KEY); } catch { return null; }
}

function writeView(value: View) {
  try { localStorage.setItem(VIEW_KEY, String(value)); } catch { /* private mode: not remembered */ }
}

function firstView(tenantIds: number[]): View {
  if (tenantIds.length === 1) return tenantIds[0];
  const saved = readView();
  if (saved && (saved === "all" || tenantIds.some((id) => String(id) === saved))) {
    return saved === "all" ? "all" : Number(saved);
  }
  return tenantIds.length ? "all" : null;
}

export function Dashboard() {
  const me = useMe();
  const ids = useMemo(() => me.tenants.map((t) => t.id), [me.tenants]);
  const [view, setViewState] = useState<View>(() => firstView(ids));
  const [reloadKey, setReloadKey] = useState(0);
  const [settings, setSettings] = useState(false);
  const top = useRef<HTMLDivElement>(null);

  function setView(next: View) {
    setViewState(next);
    writeView(next);
    top.current?.scrollIntoView({ block: "start" });
  }

  return (
    <>
      <div ref={top} />
      <header className={s.topbar}>
        <div className={s.who}>
          <span className={s.brand}>My business</span>
          <span className={cx(s.muted, s.small)}>{me.display_name || me.username}</span>
        </div>
        <button type="button" className={`${s.btn} ${s.ghost}`} title="Load the latest numbers"
                onClick={() => setReloadKey((k) => k + 1)}>Refresh</button>
        <button type="button" className={s.btn} onClick={() => setSettings(true)}>Settings</button>
      </header>
      {me.tenants.length > 1 && (
        <div className={s.switcher}>
          <label htmlFor="switcher" className={s.srOnly}>Business</label>
          <select id="switcher" value={String(view)} onChange={(e) => setView(e.target.value === "all" ? "all" : Number(e.target.value))}>
            <option value="all">All my businesses</option>
            {me.tenants.map((t) => <option key={t.id} value={String(t.id)}>{t.name}</option>)}
          </select>
        </div>
      )}
      <main className={s.content}>
        {!me.tenants.length ? (
          <div className={s.empty}>No business is linked to your login yet. Please contact your provider.</div>
        ) : view === "all" ? (
          <OverviewView key={`all:${reloadKey}`} onOpen={setView} />
        ) : view !== null ? (
          <TenantView key={`${view}:${reloadKey}`} tenantId={view} />
        ) : null}
      </main>
      {settings && <SettingsSheet onClose={() => setSettings(false)} />}
    </>
  );
}

// ------------------------------------------------------ all my businesses

function OverviewView({ onOpen }: { onOpen: (view: number) => void }) {
  const load = useCallback(() => ownerApi<Overview>("GET", "/api/owner/overview"), []);
  const { data, error } = useOwnerData(load);
  if (error) return <div className={s.errorBox}>Could not load: {error.message}</div>;
  if (!data) return <div className={s.loading}>Loading…</div>;
  return (
    <>
      <h1>All my businesses</h1>
      <p className={s.hint}>Tap a business for its bookings and messages.</p>
      <div className={s.bizGrid}>
        {data.tenants.map((t) => (
          <button key={t.id} type="button" className={s.biz} onClick={() => onOpen(t.id)}>
            <div className={s.bizName}>{t.name}</div>
            <StatusLine tenant={t} />
            <div className={s.nums}>
              {([[t.today.bookings.booked, "today"], [t.week.bookings.booked, "this week"],
                 [t.unanswered_open, "to review"]] as const).map(([value, label]) => (
                <div key={label}><b>{value}</b><span>{label}</span></div>
              ))}
            </div>
          </button>
        ))}
      </div>
    </>
  );
}

// ------------------------------------------------------- one business

function TenantView({ tenantId }: { tenantId: number }) {
  const load = useCallback(
    () => ownerApi<DashboardData>("GET", `/api/owner/dashboard?tenant_id=${encodeURIComponent(tenantId)}`),
    [tenantId]);
  const { data, error } = useOwnerData(load);
  const gone = error instanceof OwnerApiError && error.status === 404;
  useEffect(() => {
    // A business that is no longer theirs: the next visit starts from "all".
    if (gone) writeView("all");
  }, [gone]);
  if (error) return <div className={s.errorBox}>Could not load: {error.message}</div>;
  if (!data) return <div className={s.loading}>Loading…</div>;
  return <TenantBody d={data} />;
}

function TenantBody({ d }: { d: DashboardData }) {
  const tz = d.tenant.timezone;
  const week = d.summary.week;
  const days = useMemo(() => {
    const groups: { key: string; label: string; items: Booking[] }[] = [];
    for (const b of d.bookings_upcoming) {
      const key = fmtDayKey(b.starts_at, tz);
      const last = groups[groups.length - 1];
      if (last && last.key === key) last.items.push(b);
      else groups.push({ key, label: fmtDay(b.starts_at, tz), items: [b] });
    }
    return groups;
  }, [d.bookings_upcoming, tz]);

  return (
    <>
      <h1>{d.tenant.name}</h1>
      <StatusLine tenant={d.tenant} />

      <Section title="This week">
        <div className={s.tiles}>
          <Tile value={week.bookings.booked} label="Bookings" />
          <Tile value={percent(week.bookings.no_show_rate)} label="No-show rate" />
          <Tile value={week.messages.received} label="Messages received" />
          <Tile value={week.messages.sent_by_bot} label="Answered by the bot" />
          <Tile value={week.messages.sent_by_hand} label="Answered by hand" />
          <Tile value={week.unanswered} label="Not answered" alert={week.unanswered > 0} />
        </div>
      </Section>

      <Section title="Today" extra={<span className={cx(s.muted, s.small)}>{fmtDay(d.summary.today.since, tz)}</span>}>
        <BookingList bookings={d.bookings_today} tz={tz} empty="No bookings today." />
      </Section>

      <Section title="Next 7 days">
        {!days.length && <div className={s.empty}>Nothing booked for the coming week yet.</div>}
        {days.map((g) => (
          <div key={g.key}>
            <div className={s.dayLabel}>{g.label}</div>
            <div className={s.list}>{g.items.map((b) => <BookingRow key={b.number} b={b} tz={tz} />)}</div>
          </div>
        ))}
      </Section>

      <WeeksChart weeks={d.weeks} tz={tz} />

      <QueueSection tenantId={d.tenant.id} tz={tz} openCount={d.unanswered_open} />

      {d.tenant.requires_review && <PhotosSection tenant={d.tenant} tz={tz} />}
    </>
  );
}

function BookingList({ bookings, tz, empty }: { bookings: Booking[]; tz: string; empty: string }) {
  if (!bookings.length) return <div className={s.empty}>{empty}</div>;
  return <div className={s.list}>{bookings.map((b) => <BookingRow key={b.number} b={b} tz={tz} />)}</div>;
}

const STATE_STYLE: Record<string, string> = {
  requested: s.stRequested, pending: s.stPending, confirmed: s.stConfirmed, completed: s.stCompleted,
};

function BookingRow({ b, tz }: { b: Booking; tz: string }) {
  const meta = [`#${b.number}`];
  if (b.service) meta.push(b.service);
  meta.push(`until ${fmtTime(b.ends_at, tz)}`);
  return (
    <div className={cx(s.booking, (b.state === "cancelled" || b.state === "no_show") && s.faded)}>
      <div className={s.bookingTime}>{fmtTime(b.starts_at, tz)}</div>
      <div className={s.body}>
        <div className={s.name}>{b.customer_name || "Customer"}</div>
        <div className={s.meta}>{meta.join(" · ")}</div>
        {b.proposed_starts_at && <div className={s.meta}>Proposed new time: {fmtWhen(b.proposed_starts_at, tz)}</div>}
      </div>
      <span className={cx(s.state, STATE_STYLE[b.state])}>{BOOKING_STATES[b.state] || b.state}</span>
    </div>
  );
}

// ------------------------------------------------------------ the chart

type ChartKind = "bookings" | "messages";

function WeeksChart({ weeks, tz }: { weeks: DashboardData["weeks"]; tz: string }) {
  const [kind, setKind] = useState<ChartKind>("bookings");
  const values = weeks.map((w) => (kind === "bookings" ? w.bookings.booked : w.messages.received));
  const max = Math.max(1, ...values);
  return (
    <Section title="Last 8 weeks"
             extra={<Segmented options={[["bookings", "Bookings"], ["messages", "Messages"]] as const}
                               value={kind} onChange={setKind} />}>
      <div className={s.chart} role="img"
           aria-label={`${kind === "bookings" ? "Bookings" : "Messages received"} per week, oldest first: ${values.join(", ")}`}>
        {weeks.map((w, i) => (
          <div key={w.since} className={s.barCol}>
            <span className={s.barVal}>{values[i]}</span>
            <div className={cx(s.bar, i === weeks.length - 1 && s.barCurrent)}
                 style={{ height: `${Math.round((values[i] / max) * 80)}%` }} />
          </div>
        ))}
      </div>
      <div className={s.barLabels}>
        {weeks.map((w, i) => <span key={w.since}>{i === weeks.length - 1 ? "now" : fmtShortDay(w.since, tz)}</span>)}
      </div>
    </Section>
  );
}

// ------------------------------------------------- unanswered messages

type Filter = "open" | "reviewed" | "all";

function QueueSection({ tenantId, tz, openCount }: { tenantId: number; tz: string; openCount: number }) {
  const [filter, setFilter] = useState<Filter>("open");
  return (
    <Section title="Unanswered messages"
             extra={<Segmented options={[["open", "To review"], ["reviewed", "Reviewed"], ["all", "All"]] as const}
                               value={filter} onChange={setFilter} />}>
      <p className={s.hint}>{openCount ? `${openCount} waiting for a look.`
        : "Nothing waiting. Messages the bot did not answer show up here."}</p>
      <QueueList key={filter} tenantId={tenantId} tz={tz} filter={filter} />
    </Section>
  );
}

function QueueList({ tenantId, tz, filter }: { tenantId: number; tz: string; filter: Filter }) {
  const load = useCallback(
    async () => (await ownerApi<{ items: QueueItem[] }>(
      "GET", `/api/owner/unanswered?tenant_id=${encodeURIComponent(tenantId)}&status=${encodeURIComponent(filter)}`)).items,
    [tenantId, filter]);
  const { data, error, reload } = useOwnerData(load);
  if (error) return <div className={s.errorBox}>{error.message}</div>;
  if (!data) return <div className={s.loading}>Loading…</div>;
  if (!data.length) return <div className={s.empty}>{filter === "open" ? "Nothing to review." : "Nothing here."}</div>;
  return <div className={s.list}>{data.map((item) => <QueueCard key={item.id} item={item} tz={tz} onChanged={reload} />)}</div>;
}

function QueueCard({ item, tz, onChanged }: { item: QueueItem; tz: string; onChanged: () => Promise<unknown> }) {
  const { toast, authProblem } = useOwner();
  const [busy, setBusy] = useState(false);
  const open = item.status === "open";
  const by = item.reviewed_by ? item.reviewed_by.replace(/^owner:/, "") : "";

  async function markReviewed() {
    setBusy(true);
    try {
      await ownerApi("POST", `/api/owner/unanswered/${encodeURIComponent(item.id)}/reviewed`);
      toast("Marked reviewed.", "info");
      await onChanged();
    } catch (err) {
      if (!authProblem(err)) toast(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className={cx(s.qItem, !open && s.done)}>
      <div className={s.top}>
        <span className={s.who2}>{item.customer || `Chat ${item.chat_id}`}</span>
        <span className={s.when}>{fmtWhen(item.created_at, tz)}</span>
      </div>
      <div className={s.text}>{item.text || "(no text: a photo, a sticker or a deleted message)"}</div>
      <div className={s.why}>{QUEUE_REASONS[item.reason] || item.reason}</div>
      <div className={s.actions}>
        {open ? (
          <button type="button" className={`${s.btn} ${s.btnSmall}`} disabled={busy} onClick={() => void markReviewed()}>
            Mark reviewed</button>
        ) : (
          <span className={cx(s.muted, s.small)}>
            {"Reviewed" + (by ? " by " + by : "") + (item.reviewed_at ? ", " + fmtWhen(item.reviewed_at, tz) : "")}
          </span>
        )}
      </div>
    </div>
  );
}
