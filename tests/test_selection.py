"""
Tests for selection (asset selection algorithm).

Covers all 5 SPEC examples + edge cases (Edge 1: candidate not in list,
Edge 2: empty side, ties, missing CPRs, empty inputs).
"""
from __future__ import annotations

import pytest

from cpr import compute_cpr_from_hlc
from selection import SelectionError, select_shortlist


def _cpr_with_width_pct(target_pct: float):
    """
    Build a real CPR object with a specific width %.
    Math: pick BC = 1 - width/2 with pivot = 1, so width_pct = width / pivot * 100 = target_pct.
    """
    width = target_pct / 100.0
    BC = 1.0 - width / 2.0
    H = BC + 0.001
    L = BC - 0.001
    C = 3.0 - 2 * BC   # makes pivot = 1.0
    return compute_cpr_from_hlc(H, L, C)


# ────────────────────────────────────────────────────────────────────────
# Standard universe used across tests
# ────────────────────────────────────────────────────────────────────────
DEFAULT_SYMBOLS = [
    "EURUSD", "USDJPY", "GBPUSD", "USDCHF", "USDCAD",
    "EURJPY", "EURGBP", "EURCHF", "EURCAD",
    "GBPJPY", "GBPCHF", "GBPCAD",
    "CHFJPY", "CADCHF", "CADJPY",
]


def cprs_with_primary(primary: str, others: dict[str, float] | None = None):
    """Build CPRs where `primary` has the lowest width % among the default symbols."""
    cprs = {s: _cpr_with_width_pct(0.30) for s in DEFAULT_SYMBOLS}  # baseline
    cprs[primary] = _cpr_with_width_pct(0.05)                       # narrowest
    if others:
        for sym, pct in others.items():
            cprs[sym] = _cpr_with_width_pct(pct)
    return cprs


# ────────────────────────────────────────────────────────────────────────
# SPEC's 5 worked examples
# ────────────────────────────────────────────────────────────────────────
class TestSPECExamples:
    def test_example_1_cross_pair_expansion(self):
        """allowed=[USD,JPY], primary=EURGBP → narrowest of {EURUSD,EURJPY} + {GBPUSD,GBPJPY}."""
        cprs = cprs_with_primary("EURGBP", {
            "EURUSD": 0.10, "EURJPY": 0.08,
            "GBPUSD": 0.07, "GBPJPY": 0.09,
        })
        r = select_shortlist(DEFAULT_SYMBOLS, ["USD", "JPY"], cprs)
        assert r.primary == "EURGBP"
        assert r.expanded is True
        assert set(r.shortlist) == {"EURJPY", "GBPUSD"}  # narrowest per side

    def test_example_2_no_expansion_via_base(self):
        """allowed=[EUR], primary=EURGBP → trade EURGBP only."""
        cprs = cprs_with_primary("EURGBP")
        r = select_shortlist(DEFAULT_SYMBOLS, ["EUR"], cprs)
        assert r.primary == "EURGBP"
        assert r.expanded is False
        assert r.shortlist == ("EURGBP",)

    def test_example_3_no_expansion_via_quote(self):
        """allowed=[JPY], primary=USDJPY → trade USDJPY only."""
        cprs = cprs_with_primary("USDJPY")
        r = select_shortlist(DEFAULT_SYMBOLS, ["JPY"], cprs)
        assert r.primary == "USDJPY"
        assert r.expanded is False
        assert r.shortlist == ("USDJPY",)

    def test_example_4_single_allowed_currency_expansion(self):
        """allowed=[USD], primary=EURGBP → trade EURUSD + GBPUSD."""
        cprs = cprs_with_primary("EURGBP", {"EURUSD": 0.10, "GBPUSD": 0.10})
        r = select_shortlist(DEFAULT_SYMBOLS, ["USD"], cprs)
        assert r.primary == "EURGBP"
        assert r.expanded is True
        assert set(r.shortlist) == {"EURUSD", "GBPUSD"}

    def test_example_5_three_allowed_expansion(self):
        """allowed=[USD,JPY,CHF], primary=EURGBP → narrowest from each side."""
        cprs = cprs_with_primary("EURGBP", {
            "EURUSD": 0.10, "EURJPY": 0.08, "EURCHF": 0.12,
            "GBPUSD": 0.07, "GBPJPY": 0.09, "GBPCHF": 0.11,
        })
        r = select_shortlist(DEFAULT_SYMBOLS, ["USD", "JPY", "CHF"], cprs)
        assert r.primary == "EURGBP"
        assert r.expanded is True
        assert set(r.shortlist) == {"EURJPY", "GBPUSD"}


# ────────────────────────────────────────────────────────────────────────
# Tie-breaker
# ────────────────────────────────────────────────────────────────────────
class TestTieBreaker:
    def test_tie_breaker_uses_first_in_list(self):
        # Two pairs tied at the lowest width.
        cprs = {s: _cpr_with_width_pct(0.20) for s in DEFAULT_SYMBOLS}
        cprs["GBPUSD"] = _cpr_with_width_pct(0.05)  # tied
        cprs["USDJPY"] = _cpr_with_width_pct(0.05)  # tied
        # USDJPY appears earlier in DEFAULT_SYMBOLS than GBPUSD → primary=USDJPY
        r = select_shortlist(DEFAULT_SYMBOLS, ["JPY"], cprs)
        assert r.primary == "USDJPY"

    def test_tie_breaker_in_expansion(self):
        # Force expansion: PRIMARY = EURGBP (narrowest, no allowed-currency match).
        # On EUR side: EURUSD and EURJPY tied at 0.10 → EURUSD wins (earlier in list).
        # On GBP side: GBPJPY narrowest at 0.06 → wins.
        # Note: must keep all expansion candidates ABOVE EURGBP's width to avoid
        # accidentally promoting one of them to PRIMARY.
        cprs = cprs_with_primary("EURGBP", {  # EURGBP = 0.05 (narrowest)
            "EURUSD": 0.10, "EURJPY": 0.10,    # tied — EURUSD wins by list-order
            "GBPUSD": 0.08, "GBPJPY": 0.06,    # GBPJPY narrowest on GBP side
        })
        r = select_shortlist(DEFAULT_SYMBOLS, ["USD", "JPY"], cprs)
        assert r.primary == "EURGBP"
        assert r.expanded is True
        assert set(r.shortlist) == {"EURUSD", "GBPJPY"}


# ────────────────────────────────────────────────────────────────────────
# Edge 1 — candidate not in symbols_list (drop silently)
# ────────────────────────────────────────────────────────────────────────
class TestEdge1MissingCandidates:
    def test_drops_missing_candidate(self):
        # Use a SUBSET of pairs. EURJPY is not in subset → must be dropped.
        # primary should still expand using remaining candidate (EURUSD).
        subset = ["EURGBP", "EURUSD", "GBPUSD", "GBPJPY"]
        cprs = {
            "EURGBP": _cpr_with_width_pct(0.05),
            "EURUSD": _cpr_with_width_pct(0.10),
            "GBPUSD": _cpr_with_width_pct(0.07),
            "GBPJPY": _cpr_with_width_pct(0.09),
        }
        r = select_shortlist(subset, ["USD", "JPY"], cprs)
        # EUR side: only EURUSD (EURJPY not in subset) → winner = EURUSD
        # GBP side: GBPUSD vs GBPJPY → narrowest = GBPUSD
        assert r.expanded is True
        assert set(r.shortlist) == {"EURUSD", "GBPUSD"}


# ────────────────────────────────────────────────────────────────────────
# Edge 2 — side empty after Edge 1 filtering → SelectionError
# ────────────────────────────────────────────────────────────────────────
class TestEdge2EmptySide:
    def test_empty_side_raises(self):
        # primary=EURGBP, allowed=[CHF]. Need EURCHF and GBPCHF in symbols_list.
        # We exclude both → both sides empty → raise.
        subset = ["EURGBP", "EURUSD", "GBPUSD"]
        cprs = {
            "EURGBP": _cpr_with_width_pct(0.05),
            "EURUSD": _cpr_with_width_pct(0.10),
            "GBPUSD": _cpr_with_width_pct(0.07),
        }
        with pytest.raises(SelectionError):
            select_shortlist(subset, ["CHF"], cprs)

    def test_one_side_empty_raises(self):
        # primary=EURGBP, allowed=[USD,JPY]. EUR side has EURUSD/EURJPY.
        # GBP side has GBPUSD/GBPJPY. Drop GBP-side ones → still must raise.
        subset = ["EURGBP", "EURUSD", "EURJPY"]   # no GBP-anything
        cprs = {
            "EURGBP": _cpr_with_width_pct(0.05),
            "EURUSD": _cpr_with_width_pct(0.10),
            "EURJPY": _cpr_with_width_pct(0.08),
        }
        with pytest.raises(SelectionError, match="GBP"):
            select_shortlist(subset, ["USD", "JPY"], cprs)


# ────────────────────────────────────────────────────────────────────────
# Input validation
# ────────────────────────────────────────────────────────────────────────
class TestInputValidation:
    def test_empty_symbols_raises(self):
        with pytest.raises(SelectionError):
            select_shortlist([], ["USD"], {})

    def test_empty_allowed_raises(self):
        cprs = {s: _cpr_with_width_pct(0.10) for s in DEFAULT_SYMBOLS}
        with pytest.raises(SelectionError):
            select_shortlist(DEFAULT_SYMBOLS, [], cprs)

    def test_missing_cpr_raises(self):
        partial = {s: _cpr_with_width_pct(0.10) for s in DEFAULT_SYMBOLS[:5]}
        with pytest.raises(SelectionError, match="missing"):
            select_shortlist(DEFAULT_SYMBOLS, ["USD"], partial)


# ────────────────────────────────────────────────────────────────────────
# Single-pair universe
# ────────────────────────────────────────────────────────────────────────
class TestSinglePair:
    def test_single_pair_qualifies_no_expansion(self):
        cprs = {"EURUSD": _cpr_with_width_pct(0.10)}
        r = select_shortlist(["EURUSD"], ["USD"], cprs)
        assert r.primary == "EURUSD"
        assert r.expanded is False
        assert r.shortlist == ("EURUSD",)

    def test_single_pair_disqualified_raises(self):
        # primary=EURUSD, allowed=[JPY] → expansion needed but only 1 pair available
        cprs = {"EURUSD": _cpr_with_width_pct(0.10)}
        with pytest.raises(SelectionError):
            select_shortlist(["EURUSD"], ["JPY"], cprs)
