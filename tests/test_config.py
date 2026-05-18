"""Tests for config.StrategyConfig validation."""
from __future__ import annotations

import pytest

from config import (
    DEFAULT_SYMBOLS,
    LOT_SIZE,
    MIN_ACCOUNT_BALANCE_USD,
    IBKRConnection,
    StrategyConfig,
)


class TestConstants:
    def test_lot_size(self):
        # 0.25 lot = 25,000 units. Above IDEALPRO threshold so positions
        # surface cleanly in reqPositionsAsync; reserved for future trading.
        assert LOT_SIZE == 0.25

    def test_min_account_balance(self):
        assert MIN_ACCOUNT_BALANCE_USD == 1000

    def test_default_symbols_count(self):
        assert len(DEFAULT_SYMBOLS) == 13

    def test_default_symbols_unique(self):
        assert len(set(DEFAULT_SYMBOLS)) == len(DEFAULT_SYMBOLS)

    def test_default_symbols_includes_aud_nzd(self):
        assert "AUDUSD" in DEFAULT_SYMBOLS
        assert "AUDJPY" in DEFAULT_SYMBOLS
        assert "NZDUSD" in DEFAULT_SYMBOLS
        assert "NZDJPY" in DEFAULT_SYMBOLS


class TestValidation:
    def test_minimum_valid_config(self):
        cfg = StrategyConfig()
        assert cfg.symbols_list == tuple(DEFAULT_SYMBOLS)

    def test_empty_symbols_raises(self):
        with pytest.raises(ValueError, match="symbols"):
            StrategyConfig(symbols_list=())


class TestSafetyFlags:
    def test_dry_run_is_default(self):
        cfg = StrategyConfig()
        assert cfg.LIVE_TRADING is False

    def test_live_with_read_only_raises(self):
        with pytest.raises(ValueError, match="read_only"):
            StrategyConfig(
                LIVE_TRADING=True,
                ibkr=IBKRConnection(read_only=True),
            )

    def test_live_with_read_only_false_ok(self):
        cfg = StrategyConfig(
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
