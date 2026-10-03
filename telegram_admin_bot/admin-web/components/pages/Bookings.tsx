"use client";

// The open account's bookings (booking_api.py): a week at a time, what
// waits for the owner's answer, the waitlist, opening hours, and the
// calendar feed and AI usage. Every action goes to the running account,
// which tells the customer and the owner; nothing here confirms anything
// by itself.

import { useCallback, useEffect, useState } from "react";
import { useDialogs, useToast } from "@/components/feedback";
import { Fact, PageShell, Tabs } from "@/components/ui";
import { errorText } from "@/lib/api";
import { cx, fmtTime } from "@/lib/format";
import { usePanel, useSocketEvent } from "@/lib/panel";
import type { AiUsage, AvailabilityRule, Booking, BookingEvent, BookingsWeek, WaitlistEntry } from "@/lib/types";
import { useLoader } from "@/lib/useLoader";

type Tab = "calendar" | "waiting" | "waitlist" | "hours" | "links";

const BK_STATE: Record<string, string> = {
  requested: "not sent to the owner yet", pending: "waiting for the owner", confirmed: "confirmed",
  cancelled: "cancelled", no_show: "missed", completed: "done",
};
const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
const SHORT: Intl.DateTimeFormatOptions = { weekday: "short", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" };

function when(iso: string, tz: string | undefined, opts: Intl.DateTimeFormatOptions): string {
  return new Date(iso).toLocaleString([], { timeZone: tz, ...opts });
}

/* ------------------------------------------------------------- one booking */

// Every transition of one booking, newest state included.
function History({ bookingId }: { bookingId: number }) {
  const { sApi } = usePanel();
  const toast = useToast();
  const [events, setEvents] = useState<BookingEvent[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    sApi<{ events: BookingEvent[] }>("GET", `/bookings/${bookingId}`)
      .then((d) => { if (!cancelled) setEvents(d.events); }, (err) => toast(errorText(err)));
    return () => { cancelled = true; };
  }, [bookingId, sApi, toast]);

  if (!events) return null;
  return (
    <div className="bk-events">
      {events.map((e, i) => (
        <div key={i}>
          {fmtTime(e.created_at)} · {e.action} by {e.actor}
          {e.from_state ? ` (${e.from_state} → ${e.to_state})` : ` (${e.to_state})`}
          {e.reason ? ` — ${e.reason}` : ""}
        </div>
      ))}
    </div>
  );
}

function BookingRow({ b, tz, open, onToggle, onChanged }: {
  b: Booking; tz?: string; open: boolean; onToggle: () => void; onChanged: () => Promise<void>;
}) {
  const { sApi } = usePanel();
  const toast = useToast();
  const { prompt } = useDialogs();

  const act = async (action: string, needsTime?: boolean) => {
    const body: Record<string, string> = { action };
    if (needsTime) {
      const now = when(b.starts_at, tz, { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit",
                                         minute: "2-digit", hour12: false });
      const answer = await prompt(`New start time in ${tz} (YYYY-MM-DD HH:MM). Now: ${now}`, "");
      if (!answer) return;
      const m = answer.trim().match(/^(\d{4}-\d{2}-\d{2})[ T](\d{1,2}):(\d{2})$/);
      if (!m) { toast("Use YYYY-MM-DD HH:MM"); return; }
      body.starts_at = `${m[1]}T${m[2].padStart(2, "0")}:${m[3]}`;
    }
    if (action === "cancel") {
      const reason = await prompt("Why? (goes into the history; the customer is told it is cancelled)", "");
      if (reason === null) return;
      body.reason = reason;
    }
    try {
      await sApi("POST", `/bookings/${b.id}/action`, body);
      toast("Done. The customer and the owner are told.", "info");
    } catch (err) { toast(errorText(err)); }
    await onChanged();
  };

  const future = new Date(b.starts_at) > new Date();
  const live = ["requested", "pending", "confirmed"].includes(b.state);
  const arrived = b.arrived_at ? when(b.arrived_at, tz, { hour: "2-digit", minute: "2-digit" }) +
    (b.arrival_photo_match === true ? " (photo matches the entrance)"
      : b.arrival_photo_match === false ? " (photo did not match)" : "") : "";

  return (
    <div className={cx("bk-row", `st-${b.state}`, open && "open")}>
      <div className="bk-head" onClick={onToggle}>
        <span className="bk-time">{when(b.starts_at, tz, { hour: "2-digit", minute: "2-digit" })}</span>
        <span className="bk-num">#{b.number}</span>
        <span className="bk-who">{b.customer_name || "customer"}</span>
        {b.service && <span className="muted">{b.service}</span>}
        <span className="spacer" />
        <span className="bk-state">{BK_STATE[b.state] || b.state}</span>
      </div>
      {b.proposed_starts_at && (
        <div className="bk-proposal">
          {b.proposed_by === "owner" ? "Proposed to the customer" : "The customer asks to move it to"}:{" "}
          {when(b.proposed_starts_at, tz, SHORT)}
        </div>
      )}
      {open && (
        <div className="bk-detail">
          <Fact label="When" value={when(b.starts_at, tz, SHORT) + "–" + when(b.ends_at, tz, { hour: "2-digit", minute: "2-digit" })} />
          <Fact label="Customer" value={(b.customer_name || "") + (b.customer_username ? ` (@${b.customer_username})` : "")} />
          <Fact label="Notes" value={b.notes} />
          <Fact label="Cancelled" value={b.cancel_reason} />
          <Fact label="Coming" value={b.attendance_confirmed_at ? "confirmed by the customer" : ""} />
          <Fact label="Arrived" value={arrived} />
          <div className="bk-actions">
            {b.state === "requested" && <button type="button" className="btn small" onClick={() => act("resend")}>Send to the owner again</button>}
            {(b.state === "requested" || b.state === "pending") && <>
              <button type="button" className="btn small primary" onClick={() => act("confirm")}>Confirm</button>
              <button type="button" className="btn small warn" onClick={() => act("decline")}>Decline</button>
            </>}
            {b.state === "confirmed" && b.proposed_by === "customer" && <>
              <button type="button" className="btn small primary" onClick={() => act("confirm")}>Accept the move</button>
              <button type="button" className="btn small" onClick={() => act("decline")}>Keep the old time</button>
            </>}
            {live && future && <>
              <button type="button" className="btn small" onClick={() => act("propose", true)}>Propose another time</button>
              {b.state === "confirmed" && <button type="button" className="btn small" onClick={() => act("reschedule", true)}>Move it now</button>}
              <button type="button" className="btn small warn" onClick={() => act("cancel")}>Cancel</button>
            </>}
            {b.state === "confirmed" && !future && <>
              <button type="button" className="btn small primary" onClick={() => act("complete")}>Done</button>
              <button type="button" className="btn small warn" onClick={() => act("no_show")}>Did not come</button>
            </>}
          </div>
          <History key={b.state} bookingId={b.id} />
        </div>
      )}
    </div>
  );
}

/* --------------------------------------------------------------- the tabs */

function Calendar({ data, start, setStart, rowProps }: {
  data: BookingsWeek; start: string; setStart: (start: string | null) => void;
  rowProps: (b: Booking) => React.ComponentProps<typeof BookingRow>;
}) {
  const shift = (days: number) => {
    const d = new Date(start + "T00:00:00Z");
    d.setUTCDate(d.getUTCDate() + days);
    setStart(d.toISOString().slice(0, 10));
  };
  const byDay = new Map<string, Booking[]>();
  for (const b of data.bookings) {
    const day = when(b.starts_at, data.timezone, { weekday: "long", day: "numeric", month: "long" });
    byDay.set(day, [...(byDay.get(day) || []), b]);
  }
  return (
    <>
      <div className="bk-nav">
        <button type="button" className="btn small" onClick={() => shift(-7)}>◀ Previous week</button>
        <button type="button" className="btn small" onClick={() => setStart(null)}>This week</button>
        <button type="button" className="btn small" onClick={() => shift(7)}>Next week ▶</button>
        <span className="muted">Week of {start} · times in {data.timezone}</span>
      </div>
      {!byDay.size && <div className="empty">No bookings this week.</div>}
      {[...byDay].map(([day, list]) => (
        <div key={day}>
          <h4 className="bk-day">{day}</h4>
          {list.map((b) => <BookingRow key={b.id} {...rowProps(b)} />)}
        </div>
      ))}
    </>
  );
}

function Waitlist({ tz, reloadKey }: { tz?: string; reloadKey: number }) {
  const { sApi } = usePanel();
  const toast = useToast();
  const [entries, setEntries] = useState<WaitlistEntry[] | null>(null);
  const [bump, setBump] = useState(0);

  useEffect(() => {
    let cancelled = false;
    sApi<WaitlistEntry[]>("GET", "/waitlist")
      .then((list) => { if (!cancelled) setEntries(list); })
      .catch((err) => toast(errorText(err)));
    return () => { cancelled = true; };
  }, [sApi, toast, reloadKey, bump]);

  const fmt: Intl.DateTimeFormatOptions = { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" };
  return (
    <>
      <p className="pf-note">People waiting for a time to free up, first in line first. When a booking is cancelled or
        moved, the first person whose wish covers the freed time is offered it.</p>
      {entries && !entries.length && <div className="empty">Nobody is waiting.</div>}
      {(entries || []).map((e) => (
        <div key={e.id} className="bk-row">
          <div className="bk-head">
            <span className="bk-who">{e.customer_name || "customer"}</span>
            <span className="muted">{when(e.wanted_from, tz, fmt)} – {when(e.wanted_to, tz, fmt)}</span>
            <span className="spacer" />
            <span className="bk-state">{e.state === "offered" ? "offered a time" : "waiting"}</span>
            <button type="button" className="btn small warn" onClick={async () => {
              try { await sApi("DELETE", `/waitlist/${e.id}`); setBump((n) => n + 1); }
              catch (err) { toast(errorText(err)); }
            }}>Remove</button>
          </div>
        </div>
      ))}
    </>
  );
}

function OpeningHours() {
  const { sApi } = usePanel();
  const toast = useToast();
  const [rules, setRules] = useState<AvailabilityRule[] | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    sApi<{ rules: AvailabilityRule[] }>("GET", "/availability")
      .then((d) => setRules(d.rules.map((r) => ({ ...r }))))
      .catch((err) => toast(errorText(err)));
  }, [sApi, toast]);

  const update = (i: number, patch: Partial<AvailabilityRule>) =>
    setRules((list) => (list || []).map((r, n) => (n === i ? { ...r, ...patch } : r)));

  return (
    <>
      <p className="pf-note">When bookings can be made, in the client&apos;s time zone. Several rows on one day make
        breaks. With no rows at all, any time is accepted as long as it does not overlap another booking. Closed days,
        notice and how far ahead are in Settings → Config under booking.</p>
      {rules && (
        <table className="cfg-table">
          <tbody>
            <tr>{["Day", "From", "To", "Slot (min)", "Gap after (min)", ""].map((h) => <td key={h} className="muted">{h}</td>)}</tr>
            {rules.map((r, i) => (
              <tr key={i}>
                <td><select value={r.weekday} onChange={(ev) => update(i, { weekday: Number(ev.target.value) })}>
                  {WEEKDAYS.map((name, n) => <option key={n} value={n}>{name}</option>)}
                </select></td>
                <td><input type="time" value={r.start_time} onChange={(ev) => update(i, { start_time: ev.target.value })} /></td>
                <td><input type="time" value={r.end_time} onChange={(ev) => update(i, { end_time: ev.target.value })} /></td>
                <td><input type="number" value={r.slot_minutes} onChange={(ev) => update(i, { slot_minutes: Number(ev.target.value) })} /></td>
                <td><input type="number" value={r.buffer_minutes} onChange={(ev) => update(i, { buffer_minutes: Number(ev.target.value) })} /></td>
                <td><button type="button" className="btn small warn"
                            onClick={() => setRules((list) => (list || []).filter((_, n) => n !== i))}>Remove</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <div className="pf-actions">
        <button type="button" className="btn small" onClick={() => setRules((list) => {
          const last = list?.[list.length - 1];
          return [...(list || []), { weekday: last ? (last.weekday + 1) % 7 : 0, start_time: "09:00", end_time: "17:00",
                                     slot_minutes: 60, buffer_minutes: 0 }];
        })}>+ Add hours</button>
        <button type="button" className="btn primary" disabled={busy || !rules} onClick={async () => {
          setBusy(true);
          try { await sApi("PUT", "/availability", { rules }); toast("Opening hours saved.", "info"); }
          catch (err) { toast(errorText(err)); }
          finally { setBusy(false); }
        }}>Save opening hours</button>
      </div>
    </>
  );
}

function FeedAndUsage() {
  const { sApi } = usePanel();
  const toast = useToast();
  const { confirm } = useDialogs();
  const [feed, setFeed] = useState<{ url?: string; public_base_url_set: boolean } | null>(null);
  const [feedError, setFeedError] = useState<string | null>(null);
  const [usage, setUsage] = useState<AiUsage | null>(null);
  const [usageError, setUsageError] = useState<string | null>(null);
  const [bump, setBump] = useState(0);

  useEffect(() => {
    sApi<{ url?: string; public_base_url_set: boolean }>("GET", "/calendar-feed")
      .then(setFeed).catch((err) => setFeedError(errorText(err)));
  }, [sApi, bump]);
  useEffect(() => {
    sApi<AiUsage>("GET", "/ai-usage").then(setUsage).catch((err) => setUsageError(errorText(err)));
  }, [sApi]);

  const lim = (v: number | null | undefined, unit: string) => (v ? `${unit}${v}` : "no limit");

  return (
    <>
      <div className="pf-section">
        <div className="title">Calendar feed for the owner</div>
        {feedError && <div className="pf-errors">{feedError}</div>}
        {feed && !feed.public_base_url_set && (
          <p className="pf-note">Not reachable yet: set PUBLIC_BASE_URL in .env and start the public service (see the
            README). The link stays the same once it is.</p>
        )}
        {feed?.public_base_url_set && <>
          <input readOnly value={feed.url || ""} onFocus={(ev) => ev.target.select()} />
          <p className="pf-note">Subscribe to it in Google Calendar, Apple Calendar or Outlook. Anyone with the link can
            read the bookings, so share it only with the owner.</p>
        </>}
        {feed && (
          <button type="button" className="btn small warn" onClick={async () => {
            if (!(await confirm("Make a new link? The old one stops working at once."))) return;
            try { await sApi("POST", "/calendar-feed/regenerate"); setBump((n) => n + 1); }
            catch (err) { toast(errorText(err)); }
          }}>Replace the link</button>
        )}
      </div>

      <div className="pf-section">
        <div className="title">AI usage</div>
        {usageError && <div className="pf-errors">{usageError}</div>}
        {usage && <>
          <div>Today: {usage.today.tokens.toLocaleString()} tokens, €{usage.today.eur.toFixed(2)}{" "}
            (limits: {lim(usage.limits.daily_tokens, "")} tokens, {lim(usage.limits.daily_spend_eur, "€")})</div>
          <div>This month: {usage.month.tokens.toLocaleString()} tokens, €{usage.month.eur.toFixed(2)}{" "}
            (limits: {lim(usage.limits.monthly_tokens, "")} tokens, {lim(usage.limits.monthly_spend_eur, "€")})</div>
          {usage.reached && <div className="pf-errors">Stopped: {usage.reached}. Messages are still received.</div>}
          <p className="pf-note">The limits are in Settings → Config: limits.* and api_spend_cap_eur.</p>
        </>}
      </div>
    </>
  );
}

/* -------------------------------------------------------------------- page */

const TABS: [Tab, string][] = [["calendar", "Calendar"], ["waiting", "Waiting for an answer"], ["waitlist", "Waitlist"],
                               ["hours", "Opening hours"], ["links", "Calendar link & AI usage"]];

export function Bookings() {
  const { state } = usePanel();
  return (
    <PageShell title="Bookings" width="w-980">
      <div className="bk-scroll">
        {state.sessionId ? <AccountBookings key={state.sessionId} />
          : <div className="empty" style={{ padding: 40 }}>Pick an account first.</div>}
      </div>
    </PageShell>
  );
}

function AccountBookings() {
  const { sApi } = usePanel();
  const [tab, setTab] = useState<Tab>("calendar");
  // The week picked with the arrows; null = this week.
  const [start, setStart] = useState<string | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [waitlistKey, setWaitlistKey] = useState(0);

  const fetchWeek = useCallback(
    () => sApi<BookingsWeek>("GET", `/bookings${start ? `?start=${start}&days=7` : "?days=7"}`), [start, sApi]);
  const { data, error, reload: load } = useLoader(fetchWeek);
  const onChanged = useCallback(async () => { await load(); }, [load]);

  useSocketEvent(["booking"], () => { void load(); });
  useSocketEvent(["waitlist"], () => setWaitlistKey((n) => n + 1));

  const rowProps = (b: Booking) => ({
    b, tz: data?.timezone, open: selected === b.id,
    onToggle: () => setSelected((s) => (s === b.id ? null : b.id)),
    onChanged,
  });

  const tabs = TABS.map(([key, label]) =>
    [key, key === "waiting" && data ? `${label} (${data.awaiting.length})` : label] as const);

  return (
    <>
      <Tabs tabs={tabs} value={tab} onChange={(key) => { setTab(key); setSelected(null); }} />
      {error && <div className="pf-errors">{error}</div>}
      {data && !data.enabled && (
        <p className="pf-note warn-note">Bookings are off for this client (booking.enabled in Settings → Config).
          Nothing new is detected until they are turned on.</p>
      )}
      {tab === "calendar" && data &&
        <Calendar data={data} start={data.start} setStart={setStart} rowProps={rowProps} />}
      {tab === "waiting" && data && <>
        <p className="pf-note">Requests the owner has not answered yet, and moves customers asked for. The owner can
          answer by text (YES 7, NO 7, 7 15:30) or you can answer here.</p>
        {!data.awaiting.length && <div className="empty">Nothing is waiting.</div>}
        {data.awaiting.map((b) => <BookingRow key={b.id} {...rowProps(b)} />)}
      </>}
      {tab === "waitlist" && <Waitlist tz={data?.timezone} reloadKey={waitlistKey} />}
      {tab === "hours" && <OpeningHours />}
      {tab === "links" && <FeedAndUsage />}
    </>
  );
}
