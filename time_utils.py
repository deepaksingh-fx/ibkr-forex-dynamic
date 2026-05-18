"""
NY-anchored time math. FX day boundaries, trading-zone gate, prior-FX-day
window for daily CPR.

The forex day starts at 17:00 NY local. We use `America/New_York` zoneinfo
so DST transitions ride along automatically - "17:00 NY" stays 17:00 year
round; the UTC offset shifts itself between -5 (winter) and -4 (summer).
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Tuple
from zoneinfo import ZoneInfo


NY = ZoneInfo("America/New_York")
ROLLOVER_HOUR: int = 17       # 17:00 NY = FX day boundary


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
    Market closed Fri 17:00 -> Sun 17:00.
    """
    now = now_ny if now_ny is not None else ny_now()
    if now.tzinfo is None:
        raise ValueError("expected tz-aware datetime")
    weekday = now.weekday()  # Mon=0 ... Sun=6

    if weekday == 6:  # Sunday
        return now.hour >= ROLLOVER_HOUR
    if weekday == 5:  # Saturday - closed
        return False
    if weekday == 4:  # Friday - open until 17:00
        return now.hour < ROLLOVER_HOUR
    # Mon-Thu (0..3) - fully open
    return True


def current_fx_day_anchor(now_ny: datetime | None = None) -> Tuple[datetime, datetime]:
    """
    Return [start, end) of the FX day that contains `now_ny`, both NY-tz aware.

    FX day = [prior calendar day 17:00 NY, today 17:00 NY) for bar OPEN times.
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

    Calendar-mechanical: subtracts 24 hours. May land on a weekend window
    (Sat 17:00 -> Sun 17:00) on Mondays - callers needing only trading
    sessions should use `prior_trading_fx_day_window` instead.
    """
    cur_start, _ = current_fx_day_anchor(now_ny)
    return cur_start - timedelta(days=1), cur_start


def prior_trading_fx_day_window(now_ny: datetime | None = None) -> Tuple[datetime, datetime]:
    """
    Return [start, end) of the most-recent COMPLETED trading FX day strictly
    before the current FX day. Skips weekend windows.

    Mapping by current FX day:
      Mon FX day -> Fri FX day [Thu 17:00 -> Fri 17:00 NY]
      Tue FX day -> Mon FX day [Sun 17:00 -> Mon 17:00 NY]
      Wed FX day -> Tue FX day
      Thu FX day -> Wed FX day
      Fri FX day -> Thu FX day

    The FX day's `start` weekday determines this:
      Mon FX day starts Sun (weekday=6) -> walk back 2 days to Fri
      Tue FX day starts Mon (weekday=0) -> walk back 1 day
      Wed FX day starts Tue (weekday=1) -> walk back 1 day
      Thu FX day starts Wed (weekday=2) -> walk back 1 day
      Fri FX day starts Thu (weekday=3) -> walk back 1 day
    """
    cur_start, _ = current_fx_day_anchor(now_ny)
    # cur_start.weekday(): Mon=0 ... Sun=6. For valid in-zone moments, cur_start
    # is always one of {Sun, Mon, Tue, Wed, Thu} = weekdays {6, 0, 1, 2, 3}.
    if cur_start.weekday() == 6:        # Mon FX day starts Sunday -> skip weekend
        end = cur_start - timedelta(days=2)   # Fri 17:00 NY
    else:                                # Tue-Fri FX day -> yesterday's session
        end = cur_start
    start = end - timedelta(days=1)
    return start, end


def next_rollover_after(now_ny: datetime | None = None) -> datetime:
    """
    Return the next 17:00 NY FX-day boundary strictly after `now_ny`.

    This is always exactly the `end` of the current FX day (which is the
    start of the next one). Used by the main loop to sleep until the next
    selection.
    """
    _, end = current_fx_day_anchor(now_ny)
    return end
