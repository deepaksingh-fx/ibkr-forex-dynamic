"""Tests for config.StrategyConfig validation."""
from __future__ import annotations

import pytest

from config import (
    DEFAULT_SYMBOLS,
    EMA_PERIOD,
    LOT_SIZE,
    MIN_ACCOUNT_BALANCE_USD,
    TRAIL_ARM_PCT,
    IBKRConnection,
    StrategyConfig,
)


class TestConstants:
    def test_lot_size(self):
        # 0.05 lot = 5,000 units. Routes as odd-lot (below IDEALPRO 20k min)
        # and stays within USD cash buying power on both FA sub-accounts so
        # no leveraged-FX permission is required.
        assert LOT_SIZE == 0.05

    def test_min_account_balance(self):
        assert MIN_ACCOUNT_BALANCE_USD == 1000

    def test_trail_arm_pct(self):
        assert TRAIL_ARM_PCT == 0.5

    def test_ema_period(self):
        assert EMA_PERIOD == 50

    def test_default_symbols_count(self):
        assert len(DEFAULT_SYMBOLS) == 15

    def test_default_symbols_unique(self):
        assert len(set(DEFAULT_SYMBOLS)) == len(DEFAULT_SYMBOLS)


class TestValidation:
    def test_minimum_valid_config(self):
        cfg = StrategyConfig(allowed_currencies=("USD",))
        assert cfg.allowed_currencies == ("USD",)

    def test_empty_symbols_raises(self):
        with pytest.raises(ValueError, match="symbols"):
            StrategyConfig(symbols_list=(), allowed_currencies=("USD",))

    def test_empty_allowed_currencies_raises(self):
        with pytest.raises(ValueError, match="allowed_currencies"):
            StrategyConfig(allowed_currencies=())

    def test_zero_entry_trigger_raises(self):
        with pytest.raises(ValueError, match="entry_trigger"):
            StrategyConfig(allowed_currencies=("USD",), entry_trigger_range_pct=0)

    def test_negative_entry_trigger_raises(self):
        with pytest.raises(ValueError, match="entry_trigger"):
            StrategyConfig(allowed_currencies=("USD",), entry_trigger_range_pct=-0.05)

    def test_zero_per_trade_loss_raises(self):
        with pytest.raises(ValueError, match="per_trade_loss"):
            StrategyConfig(allowed_currencies=("USD",), per_trade_loss_pct=0)

    def test_zero_per_day_loss_raises(self):
        with pytest.raises(ValueError, match="per_day_loss"):
            StrategyConfig(allowed_currencies=("USD",), per_day_loss_pct=0)


class TestNormalization:
    def test_lowercase_currencies_normalized(self):
        cfg = StrategyConfig(allowed_currencies=("usd", "jpy"))
        assert cfg.allowed_currencies == ("USD", "JPY")

    def test_mixed_case_normalized(self):
        cfg = StrategyConfig(allowed_currencies=("UsD", "jPy", "EuR"))
        assert cfg.allowed_currencies == ("USD", "JPY", "EUR")

    def test_whitespace_stripped(self):
        cfg = StrategyConfig(allowed_currencies=("  usd  ",))
        assert cfg.allowed_currencies == ("USD",)


class TestSafetyFlags:
    def test_dry_run_is_default(self):
        cfg = StrategyConfig(allowed_currencies=("USD",))
        assert cfg.LIVE_TRADING is False

    def test_live_with_read_only_raises(self):
        # LIVE_TRADING=True + read_only=True is contradictory.
        with pytest.raises(ValueError, match="read_only"):
            StrategyConfig(
                allowed_currencies=("USD",),
                LIVE_TRADING=True,
                ibkr=IBKRConnection(read_only=True),
            )

    def test_live_with_read_only_false_ok(self):
        cfg = StrategyConfig(
            allowed_currencies=("USD",),
            LIVE_TRADING=True,
            ibkr=IBKRConnection(read_only=False),
        )
        assert cfg.LIVE_TRADING is True
        assert cfg.ibkr.read_only is False


class TestIBKRConnectionDefaults:
    def test_defaults_to_live_gateway_port(self):
        c = IBKRConnection()
        assert c.port == 4001       # live gateway
        assert c.host == "127.0.0.1"
        assert c.read_only is True   # safety default
