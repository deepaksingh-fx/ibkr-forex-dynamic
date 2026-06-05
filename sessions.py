"""
Session windows for the coil-index strategy. All times are IST (Asia/Kolkata).
Pure, no I/O.

Four non-overlapping sessions per day; each has its own candidate pairs. The
New York session crosses midnight IST (21:30 -> 02:30 the next calendar day).

  Asian          04:30-11:30  AUDJPY NZDJPY AUDUSD NZDUSD USDJPY
  London         12:30-17:30  GBPJPY EURJPY CHFJPY USDCHF
  LDN-NY Overlap  17:30-21:30  EURUSD GBPUSD GBPJPY USDCHF
  New York        21:30-02:30  USDCAD USDJPY CADJPY

The strategy waits for the first 5-min candle of a session to CLOSE (session
start + 5 min) before selecting; `first_candle_close()` returns that instant.

Pair order within a session matters: it is the tie-breaker for coil selection
(first appearance wins), mirroring `selection.narrowest_pair`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
FIRST_CANDLE_MINUTES = 5


@dataclass(frozen=True, slots=True)
class Session:
    name: str
    start: time          # IST time-of-day, inclusive
    end: time            # IST time-of-day, exclusive
    pairs: Tuple[str, ...]

    @property
    def crosses_midnight(self) -> bool:
        """True if the session spans the IST midnight (end <= start)."""
        return self.end <= self.start


SESSIONS: Tuple[Session, ...] = (
    Session("Asian", time(4, 30), time(11, 30),
            ("AUDJPY", "NZDJPY", "AUDUSD", "NZDUSD", "USDJPY")),
    Session("London", time(12, 30), time(17, 30),
            ("GBPJPY", "EURJPY", "CHFJPY", "USDCHF")),
    # LDN-NY Overlap intentionally emptied -> no selection, no trade this session.
    Session("LDN-NY Overlap", time(17, 30), time(21, 30), ()),
    # New York intentionally has NO pairs -> no selection, no trade this session.
    Session("New York", time(21, 30), time(2, 30), ()),
)


def parse_hhmm(s: str) -> time:
    """Parse 'HH:MM' (IST) into a time. Used when building sessions from config."""
    hh, mm = s.split(":")
    return time(int(hh), int(mm))


def build_sessions(specs) -> Tuple[Session, ...]:
    """
    Build a session tuple from an iterable of dict specs, e.g.:
        {"name": "Asian", "start": "04:30", "end": "11:30",
         "pairs": ["AUDJPY", "NZDJPY", ...]}
    Lets windows AND pairs be fully customised (config/CLI) without touching code.
    """
    out = []
    for s in specs:
        out.append(Session(
            name=s["name"],
            start=parse_hhmm(s["start"]),
            end=parse_hhmm(s["end"]),
            pairs=tuple(s["pairs"]),
        ))
    if not out:
        raise ValueError("no sessions defined")
    return tuple(out)


def session_window(session: Session, session_date: date) -> Tuple[datetime, datetime]:
    """
    Return [start, end) of `session` on `session_date` as IST-aware datetimes.
    `session_date` is the calendar date of the session START; for the midnight-
    crossing New York session the end lands on the next calendar day.
    """
    start = datetime.combine(session_date, session.start, tzinfo=IST)
    end_date = session_date + timedelta(days=1) if session.crosses_midnight else session_date
    end = datetime.combine(end_date, session.end, tzinfo=IST)
    return start, end


def first_candle_close(session: Session, session_date: date) -> datetime:
    """IST instant at which the session's first 5-min candle closes (start + 5m)."""
    start, _ = session_window(session, session_date)
    return start + timedelta(minutes=FIRST_CANDLE_MINUTES)


def _in_window(t: time, start: time, end: time) -> bool:
    if start <= end:
        return start <= t < end
    return t >= start or t < end          # crosses midnight


def active_session(now_ist: datetime,
                   sessions: Tuple[Session, ...] = SESSIONS) -> Optional[Tuple[Session, date]]:
    """
    Return (session, session_start_date) active at `now_ist`, else None.

    `sessions` defaults to the built-in SESSIONS but callers running a custom
    config (e.g. the live runtime) MUST pass their own tuple so scheduling
    respects the configured windows/pairs.

    For the New York session, an early-morning `now_ist` (before 02:30) belongs
    to the session that STARTED on the previous calendar day, so the returned
    date is yesterday's.
    """
    if now_ist.tzinfo is None:
        raise ValueError("expected tz-aware datetime")
    tod = now_ist.timetz().replace(tzinfo=None)
    for s in sessions:
        if _in_window(tod, s.start, s.end):
            if s.crosses_midnight and tod < s.end:
                return s, now_ist.date() - timedelta(days=1)
            return s, now_ist.date()
    return None
