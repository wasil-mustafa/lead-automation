"""Watermark/state tests.

File I/O is exercised against pytest's tmp_path; the `since` math is made deterministic by
injecting `now`, so no test depends on the wall clock.
"""
from datetime import datetime, timezone

from leadauto.state import State, advance, compute_since, load_state, save_state


def test_load_missing_file_returns_empty_state(tmp_path):
    state = load_state(tmp_path / "nope.json")
    assert isinstance(state, State)
    assert state.last_task_created is None
    assert state.processed_task_ids == []


def test_save_then_load_roundtrips(tmp_path):
    path = tmp_path / "watermark.json"
    save_state(State("2026-06-19T10:00:00.000+0000", ["T1", "T2"]), path)
    loaded = load_state(path)
    assert loaded.last_task_created == "2026-06-19T10:00:00.000+0000"
    assert loaded.processed_task_ids == ["T1", "T2"]


def test_compute_since_subtracts_lookback_from_watermark():
    state = State("2026-06-19T10:00:00.000+0000", [])
    assert compute_since(state, lookback_days=35) == "2026-05-15T10:00:00Z"


def test_compute_since_no_watermark_uses_now():
    state = State(None, [])
    now = datetime(2026, 6, 21, 0, 0, 0, tzinfo=timezone.utc)
    assert compute_since(state, lookback_days=35, now=now) == "2026-05-17T00:00:00Z"


def test_advance_sets_latest_created_and_unions_ids():
    state = State("2026-06-01T00:00:00.000+0000", ["T0"])
    tasks = [
        {"Id": "T1", "CreatedDate": "2026-06-10T09:00:00.000+0000"},
        {"Id": "T2", "CreatedDate": "2026-06-12T09:00:00.000+0000"},
    ]
    new = advance(state, tasks)
    assert new.last_task_created == "2026-06-12T09:00:00.000+0000"
    assert set(new.processed_task_ids) == {"T0", "T1", "T2"}


def test_advance_no_tasks_keeps_state():
    state = State("2026-06-01T00:00:00.000+0000", ["T0"])
    new = advance(state, [])
    assert new.last_task_created == "2026-06-01T00:00:00.000+0000"
    assert new.processed_task_ids == ["T0"]


def test_advance_never_moves_watermark_backwards():
    state = State("2026-06-20T00:00:00.000+0000", [])
    tasks = [{"Id": "T1", "CreatedDate": "2026-06-10T00:00:00.000+0000"}]
    new = advance(state, tasks)
    assert new.last_task_created == "2026-06-20T00:00:00.000+0000"  # kept the later one
    assert new.processed_task_ids == ["T1"]
