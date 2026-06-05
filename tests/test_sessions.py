"""
Tests for IST session windows, including the midnight-crossing NY session.
"""
from __future__ import annotations

from datetime import date, datetime, time

from sessions import (
    IST,
    SESSIONS,
    active_session,
    first_candle_close,
    session_window,
)


def _ist(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=IST)


class TestSessionDefs:
    def test_four_sessions_no_overlap_or_duplicates(self):
        assert len(SESSIONS) == 4
        names = [s.name for s in SESSIONS]
        assert names == ["Asian", "London", "LDN-NY Overlap", "New York"]

    def test_only_ny_crosses_midnight(self):
        crossing = [s.name for s in SESSIONS if s.crosses_midnight]
        assert crossing == ["New York"]

    def test_pairs_as_specified(self):
        by_name = {s.name: s.pairs for s in SESSIONS}
        assert by_name["Asian"] == ("AUDJPY", "NZDJPY", "AUDUSD", "NZDUSD", "USDJPY")
        assert by_name["London"] == ("GBPJPY", "EURJPY", "CHFJPY", "USDCHF")
        assert by_name["LDN-NY Overlap"] == ()  # intentionally emptied -> no trade
        assert by_name["New York"] == ()         # intentionally emptied -> no trade


class TestSessionWindow:
    def test_same_day_window(self):
        asian = SESSIONS[0]
        start, end = session_window(asian, date(2026, 6, 3))
        assert start == _ist(2026, 6, 3, 4, 30)
        assert end == _ist(2026, 6, 3, 11, 30)

    def test_ny_window_ends_next_day(self):
        ny = SESSIONS[3]
        start, end = session_window(ny, date(2026, 6, 3))
        assert start == _ist(2026, 6, 3, 21, 30)
        assert end == _ist(2026, 6, 4, 2, 30)        # crosses midnight

    def test_first_candle_close_is_start_plus_5m(self):
        asian = SESSIONS[0]
        assert first_candle_close(asian, date(2026, 6, 3)) == _ist(2026, 6, 3, 4, 35)


class TestActiveSession:
    def test_asian_active(self):
        s, d = active_session(_ist(2026, 6, 3, 6, 0))
        assert s.name == "Asian" and d == date(2026, 6, 3)

    def test_gap_between_sessions_is_none(self):
        # 11:30-12:30 IST is a gap (no session)
        assert active_session(_ist(2026, 6, 3, 12, 0)) is None
        # 02:30-04:30 IST is a gap
        assert active_session(_ist(2026, 6, 3, 3, 0)) is None

    def test_ny_before_midnight_is_today(self):
        s, d = active_session(_ist(2026, 6, 3, 23, 0))
        assert s.name == "New York" and d == date(2026, 6, 3)

    def test_ny_after_midnight_belongs_to_yesterday(self):
        s, d = active_session(_ist(2026, 6, 4, 1, 0))
        assert s.name == "New York" and d == date(2026, 6, 3)   # session started prev day

    def test_boundaries_inclusive_start_exclusive_end(self):
        asian = SESSIONS[0]
        assert active_session(_ist(2026, 6, 3, 4, 30))[0].name == "Asian"   # inclusive start
        assert active_session(_ist(2026, 6, 3, 11, 30)) is None             # exclusive end
