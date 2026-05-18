"""
Tests for time_utils.

Covers:
  - is_in_trading_zone:    boundaries, weekdays, weekend
  - current_fx_day_anchor: pre/post 17:00, midnight, exact boundaries
  - prior_fx_day_window:   per-weekday correctness
  - next_rollover_after:   matches the FX-day end
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
    is_in_trading_zone,
    next_rollover_after,
    ny_now,
    prior_fx_day_window,
    prior_trading_fx_day_window,
    to_ny,
)


# ------------------------------------------------------------------------
# ny_now / to_ny
# ------------------------------------------------------------------------
def test_ny_now_is_aware_and_in_ny():
    n = ny_now()
    assert n.tzinfo is not None
    assert str(n.tzinfo) == "America/New_York"


def test_to_ny_naive_is_treated_as_utc():
    naive = datetime(2025, 6, 15, 21, 0)  # naive - assumed UTC
    out = to_ny(naive)
    # June: NY is UTC-4 (EDT). 21:00 UTC -> 17:00 NY.
    assert out.hour == 17 and out.minute == 0
    assert str(out.tzinfo) == "America/New_York"


def test_to_ny_aware_utc_converted():
    utc = datetime(2025, 12, 15, 22, 0, tzinfo=timezone.utc)  # winter
    out = to_ny(utc)
    # December: NY is UTC-5 (EST). 22:00 UTC -> 17:00 NY.
    assert out.hour == 17 and out.minute == 0


def test_to_ny_aware_non_utc_tz_converted():
    london = datetime(2025, 6, 15, 22, 0, tzinfo=ZoneInfo("Europe/London"))
    out = to_ny(london)
    # June: London BST UTC+1, NY EDT UTC-4 -> NY = London - 5h = 17:00.
    assert out.hour == 17 and out.minute == 0


# ------------------------------------------------------------------------
# is_in_trading_zone
# ------------------------------------------------------------------------
class TestTradingZone:
    @pytest.mark.parametrize("dt,expected", [
        # Sunday boundaries
        (datetime(2025, 11, 16, 16, 59, tzinfo=NY), False),  # Sun before 17:00
        (datetime(2025, 11, 16, 17, 0, tzinfo=NY), True),    # Sun 17:00 (boundary inclusive)
        (datetime(2025, 11, 16, 17, 1, tzinfo=NY), True),    # Sun after 17:00
        # Monday - fully open
        (datetime(2025, 11, 17, 0, 0, tzinfo=NY), True),
        (datetime(2025, 11, 17, 14, 0, tzinfo=NY), True),
        (datetime(2025, 11, 17, 23, 59, tzinfo=NY), True),
        # Tuesday/Wednesday/Thursday - fully open
        (datetime(2025, 11, 18, 12, 0, tzinfo=NY), True),
        (datetime(2025, 11, 19, 12, 0, tzinfo=NY), True),
        (datetime(2025, 11, 20, 12, 0, tzinfo=NY), True),
        # Friday boundaries
        (datetime(2025, 11, 21, 16, 59, tzinfo=NY), True),   # Fri before 17:00
        (datetime(2025, 11, 21, 17, 0, tzinfo=NY), False),   # Fri 17:00 (boundary exclusive)
        (datetime(2025, 11, 21, 17, 1, tzinfo=NY), False),   # Fri after close
        # Saturday - fully closed
        (datetime(2025, 11, 22, 0, 0, tzinfo=NY), False),
        (datetime(2025, 11, 22, 12, 0, tzinfo=NY), False),
        (datetime(2025, 11, 22, 23, 59, tzinfo=NY), False),
    ])
    def test_zone(self, dt, expected):
        assert is_in_trading_zone(dt) is expected

    def test_naive_datetime_raises(self):
        with pytest.raises(ValueError):
            is_in_trading_zone(datetime(2025, 11, 17, 12, 0))


# ------------------------------------------------------------------------
# current_fx_day_anchor
# ------------------------------------------------------------------------
class TestFXDayAnchor:
    def test_monday_morning_is_in_monday_fx_day(self):
        s, e = current_fx_day_anchor(datetime(2025, 11, 17, 9, 0, tzinfo=NY))
        assert s == datetime(2025, 11, 16, 17, 0, tzinfo=NY)
        assert e == datetime(2025, 11, 17, 17, 0, tzinfo=NY)

    def test_sunday_evening_is_in_monday_fx_day(self):
        s, e = current_fx_day_anchor(datetime(2025, 11, 16, 18, 30, tzinfo=NY))
        assert s == datetime(2025, 11, 16, 17, 0, tzinfo=NY)
        assert e == datetime(2025, 11, 17, 17, 0, tzinfo=NY)

    def test_at_1700_boundary_starts_new_fx_day(self):
        s, e = current_fx_day_anchor(datetime(2025, 11, 17, 17, 0, tzinfo=NY))
        assert s == datetime(2025, 11, 17, 17, 0, tzinfo=NY)
        assert e == datetime(2025, 11, 18, 17, 0, tzinfo=NY)

    def test_just_before_rollover_still_old_fx_day(self):
        s, _ = current_fx_day_anchor(datetime(2025, 11, 17, 16, 59, tzinfo=NY))
        assert s == datetime(2025, 11, 16, 17, 0, tzinfo=NY)

    def test_window_length_is_24h(self):
        for hour in (0, 5, 12, 16, 17, 18, 23):
            s, e = current_fx_day_anchor(datetime(2025, 11, 17, hour, 30, tzinfo=NY))
            assert e - s == timedelta(days=1)


# ------------------------------------------------------------------------
# prior_fx_day_window
# ------------------------------------------------------------------------
class TestPriorFXDayWindow:
    @pytest.mark.parametrize("now_ny,exp_start,exp_end", [
        # Tuesday -> Mon FX day
        (
            datetime(2025, 11, 18, 9, 0, tzinfo=NY),
            datetime(2025, 11, 16, 17, 0, tzinfo=NY),
            datetime(2025, 11, 17, 17, 0, tzinfo=NY),
        ),
        # Wednesday -> Tue FX day
        (
            datetime(2025, 11, 19, 9, 0, tzinfo=NY),
            datetime(2025, 11, 17, 17, 0, tzinfo=NY),
            datetime(2025, 11, 18, 17, 0, tzinfo=NY),
        ),
        # Thursday -> Wed FX day
        (
            datetime(2025, 11, 20, 9, 0, tzinfo=NY),
            datetime(2025, 11, 18, 17, 0, tzinfo=NY),
            datetime(2025, 11, 19, 17, 0, tzinfo=NY),
        ),
        # Friday -> Thu FX day
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


# ------------------------------------------------------------------------
# prior_trading_fx_day_window (skips weekends)
# ------------------------------------------------------------------------
class TestPriorTradingFXDayWindow:
    @pytest.mark.parametrize("now_ny,exp_start,exp_end", [
        # Mon FX day (Sun 17:00 -> Mon 17:00) -> Fri FX day [Thu 17:00 -> Fri 17:00]
        (
            datetime(2025, 11, 17, 9, 0, tzinfo=NY),
            datetime(2025, 11, 13, 17, 0, tzinfo=NY),
            datetime(2025, 11, 14, 17, 0, tzinfo=NY),
        ),
        # Sunday 18:30 NY is also in Mon FX day -> also returns Fri
        (
            datetime(2025, 11, 16, 18, 30, tzinfo=NY),
            datetime(2025, 11, 13, 17, 0, tzinfo=NY),
            datetime(2025, 11, 14, 17, 0, tzinfo=NY),
        ),
        # Tue -> Mon FX day [Sun 17:00 -> Mon 17:00]
        (
            datetime(2025, 11, 18, 9, 0, tzinfo=NY),
            datetime(2025, 11, 16, 17, 0, tzinfo=NY),
            datetime(2025, 11, 17, 17, 0, tzinfo=NY),
        ),
        # Wed -> Tue FX day
        (
            datetime(2025, 11, 19, 9, 0, tzinfo=NY),
            datetime(2025, 11, 17, 17, 0, tzinfo=NY),
            datetime(2025, 11, 18, 17, 0, tzinfo=NY),
        ),
        # Fri -> Thu FX day
        (
            datetime(2025, 11, 21, 9, 0, tzinfo=NY),
            datetime(2025, 11, 19, 17, 0, tzinfo=NY),
            datetime(2025, 11, 20, 17, 0, tzinfo=NY),
        ),
    ])
    def test_per_weekday(self, now_ny, exp_start, exp_end):
        s, e = prior_trading_fx_day_window(now_ny)
        assert s == exp_start
        assert e == exp_end

    def test_returned_window_is_a_weekday_session(self):
        # Whatever Monday we test, the returned window must span
        # [Thu 17:00 -> Fri 17:00].
        s, e = prior_trading_fx_day_window(datetime(2026, 5, 18, 9, 0, tzinfo=NY))
        assert s.weekday() == 3   # Thursday
        assert e.weekday() == 4   # Friday
        assert s.hour == 17 and e.hour == 17


# ------------------------------------------------------------------------
# next_rollover_after
# ------------------------------------------------------------------------
class TestNextRollover:
    def test_mid_fx_day_returns_today_1700(self):
        # Tue 09:00 NY -> in Tue FX day [Mon 17:00, Tue 17:00) -> next rollover Tue 17:00.
        assert next_rollover_after(datetime(2025, 11, 18, 9, 0, tzinfo=NY)) == \
            datetime(2025, 11, 18, 17, 0, tzinfo=NY)

    def test_at_1700_returns_next_day_1700(self):
        # Tue 17:00 exact -> we're now in Wed FX day -> next rollover Wed 17:00.
        assert next_rollover_after(datetime(2025, 11, 18, 17, 0, tzinfo=NY)) == \
            datetime(2025, 11, 19, 17, 0, tzinfo=NY)

    def test_evening_returns_tomorrow_1700(self):
        # Mon 20:00 NY -> in Tue FX day [Mon 17:00, Tue 17:00) -> next rollover Tue 17:00.
        assert next_rollover_after(datetime(2025, 11, 17, 20, 0, tzinfo=NY)) == \
            datetime(2025, 11, 18, 17, 0, tzinfo=NY)


# ------------------------------------------------------------------------
# DST transitions
# ------------------------------------------------------------------------
class TestDST:
    """The 17:00 NY anchor is local-time. DST shifts the UTC offset but
       not the local hour, so gates work cleanly across the transitions."""

    def test_spring_forward_sunday_evening_in_zone(self):
        dt = datetime(2025, 3, 9, 17, 0, tzinfo=NY)
        assert is_in_trading_zone(dt) is True

    def test_spring_forward_monday_in_zone(self):
        dt = datetime(2025, 3, 10, 12, 0, tzinfo=NY)
        assert is_in_trading_zone(dt) is True

    def test_fall_back_sunday_evening_in_zone(self):
        dt = datetime(2025, 11, 2, 17, 0, tzinfo=NY)
        assert is_in_trading_zone(dt) is True

    def test_fall_back_monday_in_zone(self):
        dt = datetime(2025, 11, 3, 12, 0, tzinfo=NY)
        assert is_in_trading_zone(dt) is True

    def test_fx_day_anchor_in_summer_and_winter(self):
        # Summer (EDT)
        s, _ = current_fx_day_anchor(datetime(2025, 6, 15, 14, 0, tzinfo=NY))
        assert s.hour == 17 and s.day == 14
        # Winter (EST)
        s2, _ = current_fx_day_anchor(datetime(2025, 12, 15, 14, 0, tzinfo=NY))
        assert s2.hour == 17 and s2.day == 14
