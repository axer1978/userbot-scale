"""Reply timing from config: delays, burst gaps, quiet hours."""

from __future__ import annotations

import random
import statistics
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import humanlike

RIGA = ZoneInfo("Europe/Riga")
NIGHT = {"enabled": True, "start": "21:00", "end": "09:00"}


def test_uniform_delay_stays_in_range():
    rng = random.Random(1)
    values = [humanlike.sample_reply_delay({"min_s": 10, "max_s": 30, "distribution": "uniform"}, rng)
              for _ in range(500)]
    assert min(values) >= 10 and max(values) <= 30


def test_lognormal_delay_stays_in_range_and_leans_early():
    rng = random.Random(2)
    values = [humanlike.sample_reply_delay({"min_s": 10, "max_s": 110, "distribution": "lognormal"}, rng)
              for _ in range(2000)]
    assert min(values) >= 10 and max(values) <= 110
    # Clustered below the middle of the range, unlike uniform.
    assert statistics.median(values) < 60


def test_equal_bounds_mean_exactly_that_delay():
    assert humanlike.sample_reply_delay({"min_s": 7, "max_s": 7, "distribution": "lognormal"}) == 7


def test_burst_gap_is_in_milliseconds_range():
    rng = random.Random(3)
    gaps = [humanlike.burst_gap_seconds({"gap_ms": {"min": 500, "max": 1500}}, rng) for _ in range(200)]
    assert min(gaps) >= 0.5 and max(gaps) <= 1.5


@pytest.mark.parametrize("hhmm, quiet", [
    ("20:59", False), ("21:00", True), ("23:30", True), ("03:00", True), ("08:59", True), ("09:00", False),
])
def test_quiet_window_across_midnight(hhmm, quiet):
    hour, minute = map(int, hhmm.split(":"))
    assert humanlike.in_quiet_hours(datetime(2026, 9, 30, hour, minute, tzinfo=RIGA), NIGHT) is quiet


def test_same_day_window_and_disabled_and_empty_windows():
    lunch = {"enabled": True, "start": "12:00", "end": "13:00"}
    assert humanlike.in_quiet_hours(datetime(2026, 9, 30, 12, 30, tzinfo=RIGA), lunch)
    assert not humanlike.in_quiet_hours(datetime(2026, 9, 30, 13, 30, tzinfo=RIGA), lunch)
    assert not humanlike.in_quiet_hours(datetime(2026, 9, 30, 23, 0, tzinfo=RIGA), {**NIGHT, "enabled": False})
    assert not humanlike.in_quiet_hours(datetime(2026, 9, 30, 23, 0, tzinfo=RIGA),
                                        {"enabled": True, "start": "10:00", "end": "10:00"})


def test_seconds_until_quiet_ends():
    assert humanlike.seconds_until_quiet_ends(datetime(2026, 9, 30, 23, 0, tzinfo=RIGA), NIGHT) == 10 * 3600
    assert humanlike.seconds_until_quiet_ends(datetime(2026, 9, 30, 8, 30, tzinfo=RIGA), NIGHT) == 30 * 60
    assert humanlike.seconds_until_quiet_ends(datetime(2026, 9, 30, 12, 0, tzinfo=RIGA), NIGHT) == 0


def test_quiet_hours_are_measured_in_real_time_across_a_dst_change():
    # Riga goes from UTC+3 to UTC+2 at 04:00 on 25 Oct 2026: the night from
    # 01:00 to 09:00 is eight hours on the wall clock but nine in reality.
    at = datetime(2026, 10, 25, 1, 0, tzinfo=RIGA)
    assert humanlike.seconds_until_quiet_ends(at, NIGHT) == 9 * 3600
