"""
Tests for the daily-narrowest pair selector.

The selector picks the symbol with the lowest CPR width %, with ties
broken by first appearance in symbols_list.
"""
from __future__ import annotations

import pytest

from cpr import compute_cpr_from_hlc
from selection import SelectionError, narrowest_pair


def _cpr_with_width_pct(target_pct: float):
    """Build a real CPR with a specific width % (pivot=1.0)."""
    width = target_pct / 100.0
    BC = 1.0 - width / 2.0
    H = BC + 0.001
    L = BC - 0.001
    C = 3.0 - 2 * BC   # makes pivot = 1.0
    return compute_cpr_from_hlc(H, L, C)


DEFAULT_SYMBOLS = [
    "EURUSD", "USDJPY", "GBPUSD", "USDCHF", "USDCAD",
    "EURJPY", "EURGBP", "EURCHF", "EURCAD",
    "GBPJPY", "GBPCHF", "GBPCAD",
    "CHFJPY", "CADCHF", "CADJPY",
]


class TestNarrowestPair:
    def test_picks_lowest_width_pct(self):
        cprs = {s: _cpr_with_width_pct(0.30) for s in DEFAULT_SYMBOLS}
        cprs["EURGBP"] = _cpr_with_width_pct(0.05)   # narrowest
        assert narrowest_pair(DEFAULT_SYMBOLS, cprs) == "EURGBP"

    def test_unique_winner_in_middle_of_list(self):
        cprs = {s: _cpr_with_width_pct(0.20) for s in DEFAULT_SYMBOLS}
        cprs["EURCHF"] = _cpr_with_width_pct(0.01)
        assert narrowest_pair(DEFAULT_SYMBOLS, cprs) == "EURCHF"


class TestTieBreaker:
    def test_ties_resolve_to_first_in_list(self):
        cprs = {s: _cpr_with_width_pct(0.20) for s in DEFAULT_SYMBOLS}
        cprs["USDJPY"] = _cpr_with_width_pct(0.05)   # tied
        cprs["GBPUSD"] = _cpr_with_width_pct(0.05)   # tied
        # USDJPY appears earlier in DEFAULT_SYMBOLS → wins.
        assert narrowest_pair(DEFAULT_SYMBOLS, cprs) == "USDJPY"

    def test_all_equal_returns_first(self):
        cprs = {s: _cpr_with_width_pct(0.10) for s in DEFAULT_SYMBOLS}
        assert narrowest_pair(DEFAULT_SYMBOLS, cprs) == DEFAULT_SYMBOLS[0]


class TestInputValidation:
    def test_empty_symbols_raises(self):
        with pytest.raises(SelectionError):
            narrowest_pair([], {})

    def test_missing_cpr_raises(self):
        partial = {s: _cpr_with_width_pct(0.10) for s in DEFAULT_SYMBOLS[:5]}
        with pytest.raises(SelectionError, match="missing"):
            narrowest_pair(DEFAULT_SYMBOLS, partial)


class TestSingleSymbol:
    def test_single_symbol_returns_itself(self):
        cprs = {"EURUSD": _cpr_with_width_pct(0.10)}
        assert narrowest_pair(["EURUSD"], cprs) == "EURUSD"
