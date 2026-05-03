"""
NY-anchored time math. All FX day boundaries, trading-zone gates, and
first-hour / EOD checks live here.

The forex day starts at 17:00 NY local. We use `America/New_York` zoneinfo
so DST transitions ride along automatically — "17:00 NY" stays 17:00 year
round; the UTC offset shifts itself between -5 (winter) and -4 (summer).
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Tuple
from zoneinfo import ZoneInfo


NY = ZoneInfo("America/New_York")
ROLLOVER_HOUR: int = 17       # 17:00 NY = FX day boundary
EOD_MINUTE: time = time(16, 55)   # second-last 5-min candle close of the FX day
FIRST_HOUR_END: time = time(18, 0)  # exclusive — close < 18:00 is first hour


def ny_now() -> datetime:
    """Current wall-clock in NY timezone."""
    return datetime.now(NY)


def to_ny(dt: datetime) -> datetime:
    """Coerce a datetime into NY tz. Naive datetimes are assumed UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(NY)


def is_in_trading_zone(now_ny: datetime | None = None) -> bool:
    """
    True iff `now_ny` is in [Sun 17:00 NY, Fri 17:00 NY).
    Market closed Fri 17:00 → Sun 17:00.
    """
    now = now_ny if now_ny is not None else ny_now()
    if now.tzinfo is None:
        raise ValueError("expected tz-aware datetime")
    weekday = now.weekday()  # Mon=0 ... Sun=6

    if weekday == 6:  # Sunday
        return now.hour >= ROLLOVER_HOUR
    if weekday == 5:  # Saturday — closed
        return False
    if weekday == 4:  # Friday — open until 17:00
        return now.hour < ROLLOVER_HOUR
    # Mon-Thu (0..3) — fully open
    return True


def current_fx_day_anchor(now_ny: datetime | None = None) -> Tuple[datetime, datetime]:
    """
    Return [start, end) of the FX day that contains `now_ny`, both NY-tz aware.

    FX day = [prior calendar day 17:00 NY, today 17:00 NY) for bar OPEN times.
    Examples:
      Mon 09:00 NY → [Sun 17:00, Mon 17:00)
      Sun 18:30 NY → [Sun 17:00, Mon 17:00)   # already in Mon FX day
      Tue 02:00 NY → [Mon 17:00, Tue 17:00)
    """
    now = now_ny if now_ny is not None else ny_now()
    if now.hour >= ROLLOVER_HOUR:
        start_date = now.date()
    else:
        start_date = (now - timedelta(days=1)).date()
    start = datetime.combine(start_date, time(ROLLOVER_HOUR, 0), tzinfo=NY)
    end = start + timedelta(days=1)
    return start, end


def prior_fx_day_window(now_ny: datetime | None = None) -> Tuple[datetime, datetime]:
    """
    Return [start, end) of the FX day immediately PRIOR to the current FX day.

    Used for daily CPR (Tue–Fri). The output is a bar-open-time window:
    bars whose open ∈ [start, end) belong to the prior FX day.
    """
    cur_start, _ = current_fx_day_anchor(now_ny)
    return cur_start - timedelta(days=1), cur_start


def prior_week_window(now_ny: datetime | None = None) -> Tuple[datetime, datetime]:
    """
    Return [start, end) covering the FULL previous trading week (5 FX days).

    Used for weekly CPR (Monday + asset selection).
    For any moment in the current trading week, this returns:
        Sun 17:00 NY (last week) → Fri 17:00 NY (last week)
    Equivalently: "the Sunday before last" 17:00 → "last Friday" 17:00.
    """
    now = now_ny if now_ny is not None else ny_now()
    cur_start, _ = current_fx_day_anchor(now)
    # cur_start is the 17:00 NY anchor of the *current* FX day.
    # Walk back to the most recent Friday 17:00 NY (= end of last week).
    # If today's FX day is Mon: cur_start is Sun 17:00 → last Friday is 3 days back.
    # If today's FX day is Tue: cur_start is Mon 17:00 → last Friday is 4 days back.
    # ... Friday FX day: cur_start is Thu 17:00 → last Friday is 7 days back.
    weekday = cur_start.weekday()  # Mon=0 ... Sun=6 (cur_start is the FX day's 17:00 anchor)
    # cur_start.weekday(): for Mon FX day, anchor is Sunday 17:00 → weekday == 6
    #                     for Tue FX day, anchor is Monday 17:00 → weekday == 0
    #                     ...
    #                     for Fri FX day, anchor is Thursday 17:00 → weekday == 3
    # We want the most recent Friday 17:00 NY that is <= cur_start.
    # weekday(): Mon=0, Tue=1, Wed=2, Thu=3, Fri=4, Sat=5, Sun=6
    # Fri = 4. Days back to most recent Friday from `cur_start` (which is one of Sun/Mon/Tue/Wed/Thu):
    days_back = (weekday - 4) % 7
    # Sun(6) → 2; Mon(0) → 3; Tue(1) → 4; Wed(2) → 5; Thu(3) → 6
    end = cur_start - timedelta(days=days_back)
    # `end` is now Friday 17:00 NY of last week.
    # Start = Sunday 17:00 NY of last week = end - 5 days (Fri to Sun-prev = 5 days).
    start = end - timedelta(days=5)
    return start, end


def is_first_hour(bar_close_ny: datetime) -> bool:
    """
    True iff `bar_close_ny` is the close of a 5-min candle in the first
    hour of an FX day. Definition (SPEC §10.7, §12.1):

        close strictly before 18:00 NY on its calendar day,
        AND the close is on/after the FX-day anchor 17:00.

    First-hour bar closes: 17:05, 17:10, 17:15, ..., 17:55. (11 bars.)
    The bar closing at 18:00 is NOT first hour (boundary excluded).
    """
    if bar_close_ny.tzinfo is None:
        raise ValueError("expected tz-aware datetime")
    fx_start, _ = current_fx_day_anchor(bar_close_ny)
    # First hour ends at fx_start + 1 hour, on the SAME calendar date as fx_start.
    first_hour_end = datetime.combine(fx_start.date(), FIRST_HOUR_END, tzinfo=NY)
    return fx_start < bar_close_ny < first_hour_end


def is_eod_candle_close(bar_close_ny: datetime) -> bool:
    """
    True iff this is the second-last 5-min candle close of an FX day, i.e.
    bar opens 16:50 → closes 16:55 NY.
    """
    if bar_close_ny.tzinfo is None:
        raise ValueError("expected tz-aware datetime")
    return bar_close_ny.time() == EOD_MINUTE
