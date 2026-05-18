"""Tests for state_store.StateStore."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from state_store import PersistedState, StateStore, StateStoreError, CURRENT_VERSION


def test_save_load_roundtrip(tmp_path: Path):
    store = StateStore(tmp_path / "state.json")
    s = PersistedState(
        cfd_account="U25265693",
        selected_pair="EURUSD",
        position=1,
        entry_price=1.0900,
        entry_timestamp="2026-05-18T13:00:00-04:00",
        last_processed_open_ny="2026-05-18T13:55:00-04:00",
    )
    store.save(s)
    assert store.exists()
    loaded = store.load()
    assert loaded.cfd_account == "U25265693"
    assert loaded.selected_pair == "EURUSD"
    assert loaded.position == 1
    assert loaded.entry_price == 1.0900
    assert loaded.entry_timestamp == "2026-05-18T13:00:00-04:00"
    assert loaded.last_processed_open_ny == "2026-05-18T13:55:00-04:00"
    assert loaded.saved_at != ""   # populated by save()


def test_save_is_atomic(tmp_path: Path):
    """The .tmp file should not persist after save."""
    store = StateStore(tmp_path / "state.json")
    store.save(PersistedState(position=0))
    # No stray .tmp files in the directory.
    tmps = list(tmp_path.glob(".strategy_state.*"))
    assert tmps == []


def test_load_missing_raises(tmp_path: Path):
    store = StateStore(tmp_path / "absent.json")
    with pytest.raises(StateStoreError, match="not found"):
        store.load()


def test_load_malformed_raises(tmp_path: Path):
    p = tmp_path / "bad.json"
    p.write_text("not valid json {")
    store = StateStore(p)
    with pytest.raises(StateStoreError, match="malformed"):
        store.load()


def test_load_version_mismatch_raises(tmp_path: Path):
    p = tmp_path / "v999.json"
    p.write_text(json.dumps({"version": 999, "position": 0}))
    store = StateStore(p)
    with pytest.raises(StateStoreError, match="Unsupported state version"):
        store.load()


def test_delete(tmp_path: Path):
    store = StateStore(tmp_path / "state.json")
    store.save(PersistedState(position=0))
    assert store.exists()
    store.delete()
    assert not store.exists()
    # Idempotent - delete on missing file does not raise.
    store.delete()


def test_save_overwrites_previous(tmp_path: Path):
    store = StateStore(tmp_path / "state.json")
    store.save(PersistedState(position=1, entry_price=1.05))
    store.save(PersistedState(position=-1, entry_price=1.15))
    loaded = store.load()
    assert loaded.position == -1
    assert loaded.entry_price == 1.15


def test_position_normalization(tmp_path: Path):
    """Position values should round-trip as ints, even from JSON strings."""
    p = tmp_path / "state.json"
    p.write_text(json.dumps({
        "version": CURRENT_VERSION,
        "saved_at": "x",
        "cfd_account": "U25265693",
        "selected_pair": "EURUSD",
        "position": "1",   # string, should be coerced
        "entry_price": None,
        "entry_timestamp": None,
        "last_processed_open_ny": None,
    }))
    store = StateStore(p)
    loaded = store.load()
    assert loaded.position == 1
    assert isinstance(loaded.position, int)
