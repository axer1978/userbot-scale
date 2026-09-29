"""Is this time bookable, and if not, what is free nearby? Decided in code
from the tenant's weekly opening hours, its existing bookings and its
booking limits; the LLM only proposes a time and phrases the answer.

Opening hours are local wall time in the tenant's IANA zone; bookings and
`now` are aware datetimes (stored in UTC). All comparisons happen in UTC
after localizing each window on its own date with zoneinfo, so a rule like
09:00-17:00 means 09:00-17:00 on the clock on the days the clocks change,
too. Pure functions: no database, no clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

REASONS = ("past", "too_soon", "too_far", "closed_day", "outside_hours", "conflict", "invalid")

# Fixed English names: strftime("%a")/("%b") follow the process locale.
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


@dataclass(frozen=True)
class Rule:
    weekday: int          # 0 = Monday
    start: dtime          # local wall time
    end: dtime            # local wall time, > start (a window never crosses midnight)
    slot_minutes: int = 60
    buffer_minutes: int = 0

    def __post_init__(self) -> None:
        if not 0 <= self.weekday <= 6:
            raise ValueError(f"weekday must be 0..6, got {self.weekday}")
        if self.end <= self.start:
            raise ValueError(f"window end {self.end} must be after start {self.start}")
        if self.slot_minutes <= 0:
            raise ValueError("slot_minutes must be positive")
        if self.buffer_minutes < 0:
            raise ValueError("buffer_minutes must not be negative")


@dataclass(frozen=True)
class Busy:
    start: datetime  # aware
    end: datetime    # aware; already includes that booking's own buffer (its blocked_until)


@dataclass(frozen=True)
class Limits:
    min_notice_minutes: int = 0     # a booking must start at least this long after now
    max_days_ahead: int = 365       # and not later than this many days after now
    closed_dates: frozenset[date] = frozenset()   # local dates with no bookings at all


@dataclass(frozen=True)
class SlotCheck:
    ok: bool
    reason: Optional[str]      # None when ok; else one of REASONS
    buffer_minutes: int        # the buffer that applies to this booking (from the matching rule, 0 if no rules)


def _as_time(value: Any) -> dtime:
    if isinstance(value, dtime):
        return value
    # fromisoformat takes both "09:00" and the "09:00:00" Postgres returns as text.
    return dtime.fromisoformat(str(value).strip())


def rule_from_row(row: Mapping) -> Rule:
    """A Rule from a DB row or dict; times may be time objects or "HH:MM"."""
    slot = row.get("slot_minutes")
    buffer = row.get("buffer_minutes")
    return Rule(
        weekday=int(row["weekday"]),
        start=_as_time(row["start_time"]),
        end=_as_time(row["end_time"]),
        slot_minutes=60 if slot is None else int(slot),
        buffer_minutes=0 if buffer is None else int(buffer),
    )


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def _localize(day: date, wall: dtime, zone: ZoneInfo) -> datetime:
    """`wall` on `day` in `zone`, as UTC. fold=0: a time in the spring-forward
    gap lands where the pre-change offset puts it (i.e. an hour later on the
    new clock), and an ambiguous fall-back time means its first occurrence."""
    return datetime.combine(day, wall, tzinfo=zone).replace(fold=0).astimezone(timezone.utc)


def _window(rule: Rule, day: date, zone: ZoneInfo) -> tuple[datetime, datetime]:
    return _localize(day, rule.start, zone), _localize(day, rule.end, zone)


def _matching_rule(start: datetime, end: datetime, rules: Sequence[Rule], zone: ZoneInfo) -> Optional[Rule]:
    day = start.astimezone(zone).date()
    for rule in sorted(rules, key=lambda r: r.start):
        if rule.weekday != day.weekday():
            continue
        win_start, win_end = _window(rule, day, zone)
        if win_start <= start and end <= win_end:
            return rule
    return None


def check_slot(
    start: datetime,
    end: datetime,
    *,
    rules: Sequence[Rule],
    busy: Sequence[Busy],
    tz: str,
    now: datetime,
    limits: Limits = Limits(),
) -> SlotCheck:
    """Can [start, end) be booked? The first failing check wins, in the
    order of REASONS after "invalid". Slots need not be aligned to the
    rule's slot grid: a customer may ask for 14:10."""
    _require_aware(start, "start")
    _require_aware(end, "end")
    _require_aware(now, "now")
    for item in busy:
        _require_aware(item.start, "busy.start")
        _require_aware(item.end, "busy.end")
    zone = ZoneInfo(tz)
    start, end, now = _utc(start), _utc(end), _utc(now)

    if end <= start:
        return SlotCheck(False, "invalid", 0)
    if start <= now:
        return SlotCheck(False, "past", 0)
    if start < now + timedelta(minutes=limits.min_notice_minutes):
        return SlotCheck(False, "too_soon", 0)
    if start > now + timedelta(days=limits.max_days_ahead):
        return SlotCheck(False, "too_far", 0)
    if start.astimezone(zone).date() in limits.closed_dates:
        return SlotCheck(False, "closed_day", 0)

    buffer_minutes = 0
    if rules:  # no rules configured = hours not enforced, only limits and conflicts
        rule = _matching_rule(start, end, rules, zone)
        if rule is None:
            return SlotCheck(False, "outside_hours", 0)
        buffer_minutes = rule.buffer_minutes

    # This booking blocks until end + its buffer; existing ones already carry
    # theirs in Busy.end. Half-open, so back-to-back bookings don't collide.
    blocked_until = end + timedelta(minutes=buffer_minutes)
    for item in busy:
        if start < _utc(item.end) and _utc(item.start) < blocked_until:
            return SlotCheck(False, "conflict", buffer_minutes)
    return SlotCheck(True, None, buffer_minutes)


def free_slots(
    *,
    day_from: date,
    days: int,
    duration_minutes: int,
    rules: Sequence[Rule],
    busy: Sequence[Busy],
    tz: str,
    now: datetime,
    limits: Limits = Limits(),
    limit: Optional[int] = None,
) -> list[datetime]:
    """Bookable starts on the rules' slot grid, local dates day_from ..
    day_from + days - 1, ascending, as aware datetimes in the tenant zone.
    No rules means no grid to offer, so nothing is suggested."""
    _require_aware(now, "now")
    if not rules or days <= 0 or (limit is not None and limit <= 0):
        return []
    zone = ZoneInfo(tz)
    duration = timedelta(minutes=duration_minutes)
    ordered = sorted(rules, key=lambda r: r.start)
    found: list[datetime] = []
    seen: set[datetime] = set()
    for offset in range(days):
        day = day_from + timedelta(days=offset)
        if day in limits.closed_dates:
            continue
        for rule in ordered:
            if rule.weekday != day.weekday():
                continue
            _, win_end = _window(rule, day, zone)
            step = timedelta(minutes=rule.slot_minutes)
            wall = datetime.combine(day, rule.start)
            # Step on the wall clock so slots stay on :00/:30 across a DST
            # change; the fit against the window end is measured in UTC.
            while wall.date() == day:
                cand = _localize(day, wall.time(), zone)
                if cand + duration > win_end:
                    break
                # Two wall times in a spring-forward gap can map to one instant.
                if cand not in seen:
                    seen.add(cand)
                    if check_slot(cand, cand + duration, rules=rules, busy=busy,
                                  tz=tz, now=now, limits=limits).ok:
                        found.append(cand)
                wall += step
    found.sort()
    if limit is not None:
        found = found[:limit]
    return [slot.astimezone(zone) for slot in found]


def suggest_near(
    requested: datetime,
    duration_minutes: int,
    *,
    rules: Sequence[Rule],
    busy: Sequence[Busy],
    tz: str,
    now: datetime,
    limits: Limits = Limits(),
    count: int = 3,
    search_days: int = 7,
) -> list[datetime]:
    """Up to `count` free slots closest to `requested` (either side), from
    its local date over `search_days` days, closest first, ties earlier."""
    _require_aware(requested, "requested")
    zone = ZoneInfo(tz)
    target = _utc(requested)
    slots = free_slots(
        day_from=requested.astimezone(zone).date(), days=search_days,
        duration_minutes=duration_minutes, rules=rules, busy=busy, tz=tz,
        now=now, limits=limits,
    )
    slots = [slot for slot in slots if _utc(slot) != target]
    slots.sort(key=lambda slot: (abs(_utc(slot) - target), _utc(slot)))
    return slots[:max(count, 0)]


def describe(dt: datetime, tz: str) -> str:
    """Human text in the tenant zone, e.g. "Fri 03 Oct 14:00"."""
    _require_aware(dt, "dt")
    local = dt.astimezone(ZoneInfo(tz))
    return f"{_DAYS[local.weekday()]} {local.day:02d} {_MONTHS[local.month - 1]} {local:%H:%M}"
