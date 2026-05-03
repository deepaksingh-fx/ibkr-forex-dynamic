"""Tests for state_store — atomic JSON persistence."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from state_store import (
    CURRENT_VERSION,
    PersistedPosition,
    PersistedState,
    StateStore,
    StateStoreError,
)


@pytest.fixture
def tmp_state_path(tmp_path: Path) -> Path:
    return tmp_path / "strategy_state.json"


# ────────────────────────────────────────────────────────────
class TestRoundTrip:
    def test_save_load_empty(self, tmp_state_path):
        store = StateStore(tmp_state_path)
        store.save(PersistedState())
        loaded = store.load()
        assert loaded.version == CURRENT_VERSION
        assert loaded.positions == {}
        assert loaded.day_realized == {}
        assert loaded.halted == {}

    def test_save_load_with_positions(self, tmp_state_path):
        store = StateStore(tmp_state_path)
        s = PersistedState(
            fx_day_start="2026-05-04T17:00:00-04:00",
            shortlist=["EURUSD", "USDJPY"],
            positions={
                "EURUSD": {
                    "U25272450": PersistedPosition(
                        side="LONG", entry_price=1.17338,
                        entry_time="2026-05-04T11:05:00-04:00",
                        trail_armed=True,
                    ),
                },
                "USDJPY": {},
            },
            day_realized={"U25272450": -23.40, "U25265693": 0.0},
            halted={"U25272450": False, "U25265693": True},
        )
        store.save(s)
        loaded = store.load()
        assert loaded.shortlist == ["EURUSD", "USDJPY"]
        assert "U25272450" in loaded.positions["EURUSD"]
        p = loaded.positions["EURUSD"]["U25272450"]
        assert p.side == "LONG"
        assert p.entry_price == 1.17338
        assert p.trail_armed is True
        assert loaded.day_realized["U25272450"] == -23.40
        assert loaded.halted["U25265693"] is True

    def test_saved_at_is_populated(self, tmp_state_path):
        store = StateStore(tmp_state_path)
        s = PersistedState()
        assert s.saved_at == ""
        store.save(s)
        loaded = store.load()
        assert loaded.saved_at != ""
        # Plausibly looks like an ISO timestamp
        assert "T" in loaded.saved_at and ("+" in loaded.saved_at or "Z" in loaded.saved_at or loaded.saved_at.endswith("00:00"))


class TestErrors:
    def test_load_missing_file_raises(self, tmp_state_path):
        store = StateStore(tmp_state_path)
        assert not store.exists()
        with pytest.raises(StateStoreError, match="not found"):
            store.load()

    def test_load_malformed_json_raises(self, tmp_state_path):
        tmp_state_path.write_text("{not valid json")
        store = StateStore(tmp_state_path)
        with pytest.raises(StateStoreError, match="malformed"):
            store.load()

    def test_load_wrong_version_raises(self, tmp_state_path):
        tmp_state_path.write_text(json.dumps({"version": 999, "positions": {}}))
        store = StateStore(tmp_state_path)
        with pytest.raises(StateStoreError, match="version"):
            store.load()


class TestExistsAndDelete:
    def test_exists_false_initially(self, tmp_state_path):
        store = StateStore(tmp_state_path)
        assert store.exists() is False

    def test_exists_after_save(self, tmp_state_path):
        store = StateStore(tmp_state_path)
        store.save(PersistedState())
        assert store.exists() is True

    def test_delete(self, tmp_state_path):
        store = StateStore(tmp_state_path)
        store.save(PersistedState())
        assert store.exists()
        store.delete()
        assert not store.exists()

    def test_delete_when_missing_is_noop(self, tmp_state_path):
        store = StateStore(tmp_state_path)
        store.delete()  # should not raise


class TestAtomicWrite:
    def test_no_temp_files_left_behind(self, tmp_state_path):
        store = StateStore(tmp_state_path)
        store.save(PersistedState())
        store.save(PersistedState())
        store.save(PersistedState())
        # Only the final file should remain.
        siblings = list(tmp_state_path.parent.iterdir())
        suffixes = {p.suffix for p in siblings}
        assert ".tmp" not in suffixes
        assert tmp_state_path.exists()


class TestConsistency:
    def test_overwrite_preserves_only_latest(self, tmp_state_path):
        store = StateStore(tmp_state_path)
        # First save
        s1 = PersistedState(shortlist=["EURUSD"])
        store.save(s1)
        # Second save with different data
        s2 = PersistedState(shortlist=["GBPUSD", "USDJPY"])
        store.save(s2)
        loaded = store.load()
        assert loaded.shortlist == ["GBPUSD", "USDJPY"]
