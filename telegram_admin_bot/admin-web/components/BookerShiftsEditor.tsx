"use client";

// booking.shifts as rows (booker_shifts.py): from, to, who books then, and
// the days the shift starts on. Every change is written into the field as
// JSON (the box above stays the source); Save config saves it. A row's
// booker may be a group, picked like booking.provider's.

import { BookerGroupPicker } from "@/components/BookerGroupPicker";

type Shift = { start: string; end: string; provider: string; days: string[] };

const DAYS: [string, string][] = [
  ["mon", "Mo"], ["tue", "Tu"], ["wed", "We"], ["thu", "Th"], ["fri", "Fr"], ["sat", "Sa"], ["sun", "Su"],
];
const MAX_SHIFTS = 12;
const HHMM = /^([01]\d|2[0-3]):[0-5]\d$/;

function parse(text: string): Shift[] | null {
  if (!text.trim()) return [];
  try {
    const value: unknown = JSON.parse(text);
    if (!Array.isArray(value)) return null;
    return value.map((item) => {
      const s = (item || {}) as Record<string, unknown>;
      return {
        start: String(s.start ?? ""), end: String(s.end ?? ""), provider: String(s.provider ?? ""),
        days: Array.isArray(s.days) ? s.days.map(String) : [],
      };
    });
  } catch {
    return null;
  }
}

function json(list: Shift[]): string {
  // No days: every day (left out, as the schema's default).
  return JSON.stringify(list.map(({ days, ...rest }) => (days.length ? { ...rest, days } : rest)), null, 2);
}

function badTime(value: string, end: boolean): boolean {
  return !(HHMM.test(value) || (end && value === "24:00"));
}

function span(s: Shift): string {
  if (badTime(s.start, false) || badTime(s.end, true)) return "";
  if (s.start === s.end || (s.start === "00:00" && s.end === "24:00")) return "the whole day";
  return s.end !== "24:00" && s.end < s.start ? "runs past midnight" : "";
}

export function BookerShiftsEditor({ tenantId, value, onChange }: {
  tenantId: number | null; value: string; onChange: (json: string) => void;
}) {
  const shifts = parse(value);
  if (shifts === null) {
    return (
      <div className="cfg-helper">
        <span className="err">The shifts above are not a JSON list: fix it, or empty the box to edit them here.</span>
      </div>
    );
  }
  const write = (list: Shift[]) => onChange(json(list));
  const edit = (i: number, patch: Partial<Shift>) => write(shifts.map((s, n) => (n === i ? { ...s, ...patch } : s)));
  const toggleDay = (i: number, day: string) => {
    const days = shifts[i].days.includes(day) ? shifts[i].days.filter((d) => d !== day) : [...shifts[i].days, day];
    edit(i, { days: DAYS.map(([d]) => d).filter((d) => days.includes(d)) });
  };

  return (
    <div className="cfg-shifts">
      {shifts.length === 0 && (
        <p className="muted">No shifts: booking.provider gets every request at any hour.</p>
      )}
      {shifts.map((s, i) => (
        <div key={i} className="cfg-shift">
          <div className="cfg-shift-line">
            <span className="muted">#{i + 1}</span>
            <input className={badTime(s.start, false) ? "bad" : undefined} value={s.start} placeholder="16:00"
                   maxLength={5} inputMode="numeric" aria-label={`Shift ${i + 1} starts`}
                   onChange={(ev) => edit(i, { start: ev.target.value.trim() })} />
            <span>–</span>
            <input className={badTime(s.end, true) ? "bad" : undefined} value={s.end} placeholder="24:00"
                   maxLength={5} inputMode="numeric" aria-label={`Shift ${i + 1} ends`}
                   onChange={(ev) => edit(i, { end: ev.target.value.trim() })} />
            <input className="who" value={s.provider} maxLength={128}
                   placeholder="who books: phone, @username or group"
                   aria-label={`Shift ${i + 1}: who books`}
                   onChange={(ev) => edit(i, { provider: ev.target.value })} />
            <button type="button" className="btn small" title="Remove this shift"
                    onClick={() => write(shifts.filter((_, n) => n !== i))}>Remove</button>
          </div>
          <div className="cfg-shift-line">
            {DAYS.map(([day, label]) => (
              <label key={day} className="day" title="The days this shift starts on">
                <input type="checkbox" checked={s.days.includes(day)} onChange={() => toggleDay(i, day)} />
                {label}
              </label>
            ))}
            <span className="muted">{s.days.length ? "" : "every day"}{span(s) ? ` · ${span(s)}` : ""}</span>
          </div>
          {tenantId !== null && (
            <BookerGroupPicker tenantId={tenantId} onPick={(id) => edit(i, { provider: id })} />
          )}
        </div>
      ))}
      <div className="cfg-shift-line">
        <button type="button" className="btn small" disabled={shifts.length >= MAX_SHIFTS}
                onClick={() => write([...shifts, { start: "", end: "", provider: "", days: [] }])}>Add a shift</button>
        <span className="muted">In the account&apos;s time zone. An end before the start runs past midnight; 24:00 is
          midnight. The first shift that matches is on duty; outside them all, booking.provider.</span>
      </div>
    </div>
  );
}
