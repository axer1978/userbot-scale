"""Reply timing from the tenant config: the delay before a reply, the gaps
inside a burst, and quiet hours. Pure functions, so the rules are testable
without a clock or a Telegram client; session_runtime.py does the waiting.

Nothing here asks the LLM anything: timing is config, applied in code.
"""

from __future__ import annotations

import math
import random
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Any, Optional

MAX_LOGNORMAL_TRIES = 20


def sample_reply_delay(delay: dict[str, Any], rng: Optional[random.Random] = None) -> float:
    """Seconds to wait before writing a reply.

    uniform: anywhere in [min_s, max_s].
    lognormal: most replies near the lower third of the range, a few near
    the top, the way a person who is usually quick sometimes isn't. Drawn
    until a value lands in range (falls back to uniform if it never does).
    """
    rng = rng or random  # type: ignore[assignment]
    low, high = float(delay["min_s"]), float(delay["max_s"])
    if high <= low:
        return low
    if delay.get("distribution") == "lognormal":
        median = low + (high - low) * 0.35
        mu, sigma = math.log(max(median, 0.1)), 0.5
        for _ in range(MAX_LOGNORMAL_TRIES):
            value = rng.lognormvariate(mu, sigma)
            if low <= value <= high:
                return value
    return rng.uniform(low, high)


def burst_gap_seconds(burst: dict[str, Any], rng: Optional[random.Random] = None) -> float:
    rng = rng or random  # type: ignore[assignment]
    gap = burst["gap_ms"]
    return rng.uniform(gap["min"], gap["max"]) / 1000.0


def _hhmm(value: str) -> dtime:
    hour, minute = value.split(":")
    return dtime(int(hour), int(minute))


def in_quiet_hours(now_local: datetime, quiet: dict[str, Any]) -> bool:
    """Inside the quiet window? start == end means no window at all."""
    if not quiet.get("enabled"):
        return False
    start, end, now = _hhmm(quiet["start"]), _hhmm(quiet["end"]), now_local.time()
    if start == end:
        return False
    if start < end:
        return start <= now < end
    return now >= start or now < end


def seconds_until_quiet_ends(now_local: datetime, quiet: dict[str, Any]) -> float:
    """0 outside quiet hours; otherwise how long until the window closes.
    `now_local` must be timezone-aware (the tenant's zone), so the answer is
    right across a DST change."""
    if not in_quiet_hours(now_local, quiet):
        return 0.0
    end = _hhmm(quiet["end"])
    day = now_local.date()
    if now_local.time() >= end:
        day += timedelta(days=1)
    ends_at = datetime.combine(day, end, tzinfo=now_local.tzinfo)
    # In UTC: subtracting two datetimes that share a tzinfo counts wall-clock
    # time, which is an hour off on the night the clocks change.
    return max(0.0, (ends_at.astimezone(timezone.utc) - now_local.astimezone(timezone.utc)).total_seconds())


def later(now_local: datetime, seconds: float) -> datetime:
    """`now_local` plus real elapsed seconds, in the same zone. Plain `+`
    on an aware datetime adds wall-clock time, wrong across a DST change."""
    return (now_local.astimezone(timezone.utc) + timedelta(seconds=seconds)).astimezone(now_local.tzinfo)
