"""
Comprehensive tests for time_utils.

Covers:
  - is_in_trading_zone:    boundaries, weekdays, weekend
  - current_fx_day_anchor: pre/post 17:00, midnight, exact boundaries
  - prior_week_window:     called from each weekday returns the same window
  - prior_fx_day_window:   per-weekday correctness
  - is_first_hour:         every 5-min slot 17:00-18:10
  - is_eod_candle_close:   exact 16:55, off-by-one
  - DST transitions:       spring-forward (Mar) and fall-back (Nov)
  - to_ny:                 naive UTC, aware UTC, aware non-UTC
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from time_utils import (
    NY,
    current_fx_day_anchor,
    is_eod_candle_close,
    is_first_hour,
    is_in_trading_zone,
    ny_now,
    prior_fx_day_window,
    prior_week_window,
    to_ny,
)


# ────────────────────────────────────────────────────────────────────────
# ny_now / to_ny
# ────────────────────────────────────────────────────────────────────────
def test_ny_now_is_aware_and_in_ny():
    n = ny_now()
    assert n.tzinfo is not None
    assert str(n.tzinfo) == "America/New_York"


def test_to_ny_naive_is_treated_as_utc():
    naive = datetime(2025, 6, 15, 21, 0)  # naive — assumed UTC
    out = to_ny(naive)
    # June: NY is UTC-4 (EDT). 21:00 UTC → 17:00 NY.
    assert out.hour == 17 and out.minute == 0
    assert str(out.tzinfo) == "America/New_York"


def test_to_ny_aware_utc_converted():
    utc = datetime(2025, 12, 15, 22, 0, tzinfo=timezone.utc)  # winter
    out = to_ny(utc)
    # December: NY is UTC-5 (EST). 22:00 UTC → 17:00 NY.
    assert out.hour == 17 and out.minute == 0


def test_to_ny_aware_non_utc_tz_converted():
    london = datetime(2025, 6, 15, 22, 0, tzinfo=ZoneInfo("Europe/London"))
    out = to_ny(london)
    # June: London BST UTC+1, NY EDT UTC-4 → NY = London - 5h = 17:00.
    assert out.hour == 17 and out.minute == 0


# ────────────────────────────────────────────────────────────────────────
# is_in_trading_zone
# ────────────────────────────────────────────────────────────────────────
class TestTradingZone:
    @pytest.mark.parametrize("dt,expected", [
        # Sunday boundaries
        (datetime(2025, 11, 16, 16, 59, tzinfo=NY), False),  # Sun before 17:00
        (datetime(2025, 11, 16, 17, 0, tzinfo=NY), True),    # Sun 17:00 (boundary inclusive)
        (datetime(2025, 11, 16, 17, 1, tzinfo=NY), True),    # Sun after 17:00
        # Monday — fully open
        (datetime(2025, 11, 17, 0, 0, tzinfo=NY), True),
        (datetime(2025, 11, 17, 14, 0, tzinfo=NY), True),
        (datetime(2025, 11, 17, 23, 59, tzinfo=NY), True),
        # Tuesday/Wednesday/Thursday — fully open
        (datetime(2025, 11, 18, 12, 0, tzinfo=NY), True),
        (datetime(2025, 11, 19, 12, 0, tzinfo=NY), True),
        (datetime(2025, 11, 20, 12, 0, tzinfo=NY), True),
        # Friday boundaries
        (datetime(2025, 11, 21, 16, 59, tzinfo=NY), True),   # Fri before 17:00
        (datetime(2025, 11, 21, 17, 0, tzinfo=NY), False),   # Fri 17:00 (boundary exclusive)
        (datetime(2025, 11, 21, 17, 1, tzinfo=NY), False),   # Fri after close
        # Saturday — fully closed
        (datetime(2025, 11, 22, 0, 0, tzinfo=NY), False),
        (datetime(2025, 11, 22, 12, 0, tzinfo=NY), False),
        (datetime(2025, 11, 22, 23, 59, tzinfo=NY), False),
    ])
    def test_zone(self, dt, expected):
        assert is_in_trading_zone(dt) is expected

    def test_naive_datetime_raises(self):
        with pytest.raises(ValueError):
            is_in_trading_zone(datetime(2025, 11, 17, 12, 0))


# ────────────────────────────────────────────────────────────────────────
# current_fx_day_anchor
# ────────────────────────────────────────────────────────────────────────
class TestFXDayAnchor:
    def test_monday_morning_is_in_monday_fx_day(self):
        # Mon Nov 17 09:00 NY → Mon FX day = [Sun 17:00, Mon 17:00)
        s, e = current_fx_day_anchor(datetime(2025, 11, 17, 9, 0, tzinfo=NY))
        assert s == datetime(2025, 11, 16, 17, 0, tzinfo=NY)
        assert e == datetime(2025, 11, 17, 17, 0, tzinfo=NY)

    def test_sunday_evening_is_in_monday_fx_day(self):
        # Sun Nov 16 18:30 NY → Mon FX day [Sun 17:00, Mon 17:00)
        s, e = current_fx_day_anchor(datetime(2025, 11, 16, 18, 30, tzinfo=NY))
        assert s == datetime(2025, 11, 16, 17, 0, tzinfo=NY)
        assert e == datetime(2025, 11, 17, 17, 0, tzinfo=NY)

    def test_at_1700_boundary_starts_new_fx_day(self):
        # Mon 17:00 NY exactly → Tue FX day [Mon 17:00, Tue 17:00)
        s, e = current_fx_day_anchor(datetime(2025, 11, 17, 17, 0, tzinfo=NY))
        assert s == datetime(2025, 11, 17, 17, 0, tzinfo=NY)
        assert e == datetime(2025, 11, 18, 17, 0, tzinfo=NY)

    def test_just_before_rollover_still_old_fx_day(self):
        # Mon 16:59 NY → still Mon FX day
        s, e = current_fx_day_anchor(datetime(2025, 11, 17, 16, 59, tzinfo=NY))
        assert s == datetime(2025, 11, 16, 17, 0, tzinfo=NY)

    def test_window_length_is_24h(self):
        for hour in (0, 5, 12, 16, 17, 18, 23):
            s, e = current_fx_day_anchor(datetime(2025, 11, 17, hour, 30, tzinfo=NY))
            assert e - s == timedelta(days=1)


# ────────────────────────────────────────────────────────────────────────
# prior_week_window
# ────────────────────────────────────────────────────────────────────────
class TestPriorWeekWindow:
    """All weekdays of the same trading week must return the same window."""

    EXPECTED_START = datetime(2025, 11, 9, 17, 0, tzinfo=NY)   # Sun Nov 9
    EXPECTED_END = datetime(2025, 11, 14, 17, 0, tzinfo=NY)    # Fri Nov 14

    @pytest.mark.parametrize("now_ny", [
        datetime(2025, 11, 16, 18, 0, tzinfo=NY),  # Sun evening = Mon FX day
        datetime(2025, 11, 17, 9, 0, tzinfo=NY),   # Mon morning
        datetime(2025, 11, 17, 23, 59, tzinfo=NY), # Mon late
        datetime(2025, 11, 18, 9, 0, tzinfo=NY),   # Tue
        datetime(2025, 11, 19, 9, 0, tzinfo=NY),   # Wed
        datetime(2025, 11, 20, 9, 0, tzinfo=NY),   # Thu
        datetime(2025, 11, 21, 9, 0, tzinfo=NY),   # Fri morning
        datetime(2025, 11, 21, 16, 59, tzinfo=NY), # Fri just before close
    ])
    def test_consistent_across_week(self, now_ny):
        s, e = prior_week_window(now_ny)
        assert s == self.EXPECTED_START, f"start at {now_ny}"
        assert e == self.EXPECTED_END, f"end at {now_ny}"

    def test_window_spans_5_days(self):
        s, e = prior_week_window(datetime(2025, 11, 17, 14, 0, tzinfo=NY))
        assert e - s == timedelta(days=5)

    def test_starts_on_sunday(self):
        s, _ = prior_week_window(datetime(2025, 11, 17, 14, 0, tzinfo=NY))
        assert s.weekday() == 6  # Sunday

    def test_ends_on_friday(self):
        _, e = prior_week_window(datetime(2025, 11, 17, 14, 0, tzinfo=NY))
        assert e.weekday() == 4  # Friday


# ────────────────────────────────────────────────────────────────────────
# prior_fx_day_window
# ────────────────────────────────────────────────────────────────────────
class TestPriorFXDayWindow:
    @pytest.mark.parametrize("now_ny,exp_start,exp_end", [
        # Tuesday → Mon FX day
        (
            datetime(2025, 11, 18, 9, 0, tzinfo=NY),
            datetime(2025, 11, 16, 17, 0, tzinfo=NY),
            datetime(2025, 11, 17, 17, 0, tzinfo=NY),
        ),
        # Wednesday → Tue FX day
        (
            datetime(2025, 11, 19, 9, 0, tzinfo=NY),
            datetime(2025, 11, 17, 17, 0, tzinfo=NY),
            datetime(2025, 11, 18, 17, 0, tzinfo=NY),
        ),
        # Thursday → Wed FX day
        (
            datetime(2025, 11, 20, 9, 0, tzinfo=NY),
            datetime(2025, 11, 18, 17, 0, tzinfo=NY),
            datetime(2025, 11, 19, 17, 0, tzinfo=NY),
        ),
        # Friday → Thu FX day
        (
            datetime(2025, 11, 21, 9, 0, tzinfo=NY),
            datetime(2025, 11, 19, 17, 0, tzinfo=NY),
            datetime(2025, 11, 20, 17, 0, tzinfo=NY),
        ),
    ])
    def test_per_weekday(self, now_ny, exp_start, exp_end):
        s, e = prior_fx_day_window(now_ny)
        assert s == exp_start
        assert e == exp_end


# ────────────────────────────────────────────────────────────────────────
# is_first_hour — every 5-min slot 17:00 → 18:10
# ────────────────────────────────────────────────────────────────────────
class TestFirstHour:
    """First-hour bars: closes 17:05, 17:10, ..., 17:55. (11 bars)
       Bar closing 18:00 (opens 17:55) is NOT first-hour."""

    @pytest.mark.parametrize("close_minute,expected", [
        # All first-hour closes
        (5, True), (10, True), (15, True), (20, True), (25, True),
        (30, True), (35, True), (40, True), (45, True), (50, True), (55, True),
    ])
    def test_first_hour_5min_closes(self, close_minute, expected):
        # Mon 17:XX NY → Tue FX day, first hour [Mon 17:00, Mon 18:00)
        dt = datetime(2025, 11, 17, 17, close_minute, tzinfo=NY)
        assert is_first_hour(dt) is expected

    def test_18_00_close_not_first_hour(self):
        # Bar opens 17:55, closes 18:00 → NOT first hour (boundary excluded)
        assert is_first_hour(datetime(2025, 11, 17, 18, 0, tzinfo=NY)) is False

    def test_18_05_close_not_first_hour(self):
        assert is_first_hour(datetime(2025, 11, 17, 18, 5, tzinfo=NY)) is False

    def test_17_00_exact_not_first_hour(self):
        # 17:00 is fx_start, not strictly after — exclusive
        assert is_first_hour(datetime(2025, 11, 17, 17, 0, tzinfo=NY)) is False

    def test_mid_day_not_first_hour(self):
        assert is_first_hour(datetime(2025, 11, 17, 12, 0, tzinfo=NY)) is False
        assert is_first_hour(datetime(2025, 11, 17, 22, 0, tzinfo=NY)) is False

    def test_eod_close_not_first_hour(self):
        assert is_first_hour(datetime(2025, 11, 17, 16, 55, tzinfo=NY)) is False

    def test_first_hour_per_fx_day(self):
        # First hour of Mon FX day starts Sun 17:00 NY
        assert is_first_hour(datetime(2025, 11, 16, 17, 30, tzinfo=NY)) is True
        # First hour of Tue FX day = Mon 17:00–18:00
        assert is_first_hour(datetime(2025, 11, 17, 17, 30, tzinfo=NY)) is True
        # First hour of Fri FX day = Thu 17:00–18:00
        assert is_first_hour(datetime(2025, 11, 20, 17, 30, tzinfo=NY)) is True

    def test_naive_raises(self):
        with pytest.raises(ValueError):
            is_first_hour(datetime(2025, 11, 17, 17, 30))


# ────────────────────────────────────────────────────────────────────────
# is_eod_candle_close
# ────────────────────────────────────────────────────────────────────────
class TestEODCandleClose:
    def test_16_55_is_eod(self):
        assert is_eod_candle_close(datetime(2025, 11, 17, 16, 55, tzinfo=NY)) is True

    def test_16_50_is_not_eod(self):
        assert is_eod_candle_close(datetime(2025, 11, 17, 16, 50, tzinfo=NY)) is False

    def test_17_00_is_not_eod(self):
        # 17:00 is the rollover boundary, not the EOD candle
        assert is_eod_candle_close(datetime(2025, 11, 17, 17, 0, tzinfo=NY)) is False

    def test_naive_raises(self):
        with pytest.raises(ValueError):
            is_eod_candle_close(datetime(2025, 11, 17, 16, 55))


# ────────────────────────────────────────────────────────────────────────
# DST transitions
# ────────────────────────────────────────────────────────────────────────
class TestDST:
    """The 17:00 NY anchor is local-time. DST shifts the UTC offset but
       not the local hour, so all our gates must continue to work cleanly
       across spring-forward / fall-back."""

    def test_spring_forward_sunday_evening_in_zone(self):
        # Sun Mar 9 2025: clocks jump 02:00 → 03:00 EST→EDT
        # Sun 17:00 EDT (= 21:00 UTC) is the FX week open.
        dt = datetime(2025, 3, 9, 17, 0, tzinfo=NY)
        assert is_in_trading_zone(dt) is True

    def test_spring_forward_monday_in_zone(self):
        dt = datetime(2025, 3, 10, 12, 0, tzinfo=NY)
        assert is_in_trading_zone(dt) is True

    def test_fall_back_sunday_evening_in_zone(self):
        # Sun Nov 2 2025: clocks fall 02:00 → 01:00 EDT→EST
        # Sun 17:00 EST (= 22:00 UTC) is the FX week open.
        dt = datetime(2025, 11, 2, 17, 0, tzinfo=NY)
        assert is_in_trading_zone(dt) is True

    def test_fall_back_monday_in_zone(self):
        dt = datetime(2025, 11, 3, 12, 0, tzinfo=NY)
        assert is_in_trading_zone(dt) is True

    def test_prior_week_window_spans_dst_boundary(self):
        # Trading week of Mon Mar 10 2025 (post-DST): last week was Mar 3-7,
        # which was entirely pre-DST. The window must still anchor cleanly to
        # 17:00 local NY on each end.
        s, e = prior_week_window(datetime(2025, 3, 10, 9, 0, tzinfo=NY))
        assert s == datetime(2025, 3, 2, 17, 0, tzinfo=NY)   # Sun before last week
        assert e == datetime(2025, 3, 7, 17, 0, tzinfo=NY)   # last Friday
        # Window length should be 5 days regardless of DST.
        assert e - s == timedelta(days=5)

    def test_first_hour_works_on_dst_day(self):
        # Sun Mar 9 2025 17:30 NY (DST already shifted that morning)
        assert is_first_hour(datetime(2025, 3, 9, 17, 30, tzinfo=NY)) is True
        # Sun Mar 9 2025 18:00 NY → not first hour
        assert is_first_hour(datetime(2025, 3, 9, 18, 0, tzinfo=NY)) is False

    def test_fx_day_anchor_in_summer_and_winter(self):
        # Summer (EDT): June 15 14:00 NY → FX day [Jun 14 17:00, Jun 15 17:00)
        s, e = current_fx_day_anchor(datetime(2025, 6, 15, 14, 0, tzinfo=NY))
        assert s.hour == 17 and s.day == 14
        # Winter (EST): Dec 15 14:00 NY → FX day [Dec 14 17:00, Dec 15 17:00)
        s2, e2 = current_fx_day_anchor(datetime(2025, 12, 15, 14, 0, tzinfo=NY))
        assert s2.hour == 17 and s2.day == 14
