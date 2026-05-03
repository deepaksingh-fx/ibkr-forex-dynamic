"""Tests for pair_utils."""
from __future__ import annotations

import pytest

from pair_utils import (
    HIERARCHY,
    UnknownCurrency,
    construct_pair,
    pair_contains_any,
    split_pair,
)


# ────────────────────────────────────────────────────────────────────────
# split_pair
# ────────────────────────────────────────────────────────────────────────
class TestSplitPair:
    @pytest.mark.parametrize("input_,expected", [
        ("EURUSD", ("EUR", "USD")),
        ("USDJPY", ("USD", "JPY")),
        ("GBPCAD", ("GBP", "CAD")),
        ("eurusd", ("EUR", "USD")),     # lowercase normalized
        ("EurUsd", ("EUR", "USD")),     # mixed case normalized
        ("  EURUSD  ", ("EUR", "USD")), # whitespace stripped
    ])
    def test_valid(self, input_, expected):
        assert split_pair(input_) == expected

    @pytest.mark.parametrize("bad", [
        "",          # empty
        "EUR",       # too short
        "EURUSDX",   # too long
        "EUR USD",   # has space (length 7 after strip — actually 7 incl space → wait, after strip is 'EUR USD' = 7)
        "123456",    # not alpha
        "EURUS1",    # mixed alpha+digit
        "EUR/USD",   # has slash
    ])
    def test_invalid_raises(self, bad):
        with pytest.raises(ValueError):
            split_pair(bad)


# ────────────────────────────────────────────────────────────────────────
# construct_pair
# ────────────────────────────────────────────────────────────────────────
class TestConstructPair:
    """Hierarchy: EUR > GBP > AUD > NZD > USD > CAD > CHF > JPY"""

    @pytest.mark.parametrize("c1,c2,expected", [
        # First arg ranks higher
        ("EUR", "USD", "EURUSD"),
        ("EUR", "JPY", "EURJPY"),
        ("EUR", "GBP", "EURGBP"),
        ("GBP", "USD", "GBPUSD"),
        ("GBP", "JPY", "GBPJPY"),
        ("USD", "JPY", "USDJPY"),
        ("USD", "CHF", "USDCHF"),
        ("USD", "CAD", "USDCAD"),
        ("CAD", "JPY", "CADJPY"),
        ("CAD", "CHF", "CADCHF"),
        ("CHF", "JPY", "CHFJPY"),
        # Order flipped — should be order-agnostic
        ("USD", "EUR", "EURUSD"),
        ("JPY", "USD", "USDJPY"),
        ("CHF", "USD", "USDCHF"),
        # Lowercase and mixed
        ("eur", "usd", "EURUSD"),
        ("Usd", "Jpy", "USDJPY"),
    ])
    def test_valid_combinations(self, c1, c2, expected):
        assert construct_pair(c1, c2) == expected

    def test_same_currency_raises(self):
        with pytest.raises(ValueError):
            construct_pair("USD", "USD")

    def test_unknown_currency_raises(self):
        with pytest.raises(UnknownCurrency):
            construct_pair("XYZ", "USD")

    def test_unknown_currency_is_value_error(self):
        # UnknownCurrency must inherit from ValueError so generic ValueError
        # catches in selection.py work.
        with pytest.raises(ValueError):
            construct_pair("USD", "XYZ")

    def test_full_hierarchy_consistency(self):
        """Pairs of all distinct hierarchy currencies — base must be higher rank."""
        for i, c1 in enumerate(HIERARCHY):
            for c2 in HIERARCHY[i + 1:]:
                pair = construct_pair(c1, c2)
                assert pair == c1 + c2
                # Order swap returns same result
                assert construct_pair(c2, c1) == pair


# ────────────────────────────────────────────────────────────────────────
# pair_contains_any
# ────────────────────────────────────────────────────────────────────────
class TestPairContainsAny:
    @pytest.mark.parametrize("symbol,currencies,expected", [
        ("EURUSD", ["USD"], True),     # quote matches
        ("EURUSD", ["EUR"], True),     # base matches
        ("EURUSD", ["EUR", "USD"], True),  # both match
        ("EURUSD", ["JPY"], False),
        ("EURGBP", ["USD", "JPY"], False),
        ("USDJPY", ["JPY"], True),
        ("EURUSD", ["usd"], True),     # lowercase normalized
        ("EURUSD", ["UsD"], True),     # mixed case
        ("EURUSD", [], False),         # empty list
    ])
    def test_match(self, symbol, currencies, expected):
        assert pair_contains_any(symbol, currencies) is expected

    def test_works_with_tuple(self):
        assert pair_contains_any("EURUSD", ("USD",)) is True
