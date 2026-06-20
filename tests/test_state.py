"""Tests for src.state.PrintState delta-merge contract.

These pin the network<->UI seam Phase 2 reuses: partial report deltas merge
over a retained state (missing field == unchanged, NOT zero), and the
mc_remaining_time minutes value is None-guarded.
"""

import time

from src.state import PrintState, hmm


def test_defaults():
    """Test 1: fresh PrintState has the documented defaults."""
    s = PrintState()
    assert s.gcode_state == "unknown"
    assert s.mc_percent == 0
    assert s.mc_remaining_time == 0
    assert s.last_update_monotonic == 0.0


def test_full_merge_sets_all_fields():
    """Test 2: a report carrying all three fields sets them."""
    s = PrintState()
    s.merge({"gcode_state": "running", "mc_percent": 42, "mc_remaining_time": 83})
    assert s.gcode_state == "running"
    assert s.mc_percent == 42
    assert s.mc_remaining_time == 83


def test_partial_delta_leaves_missing_fields_unchanged():
    """Test 3: missing field == unchanged, NOT reset to zero (delta semantics)."""
    s = PrintState()
    s.merge({"gcode_state": "running", "mc_percent": 42, "mc_remaining_time": 83})
    s.merge({"mc_percent": 43})
    assert s.mc_percent == 43  # updated
    assert s.gcode_state == "running"  # unchanged, not "unknown"
    assert s.mc_remaining_time == 83  # unchanged, not 0


def test_none_remaining_time_does_not_overwrite():
    """Test 4: an explicit None mc_remaining_time must not clobber a prior value."""
    s = PrintState()
    s.merge({"mc_remaining_time": 83})
    s.merge({"mc_remaining_time": None})
    assert s.mc_remaining_time == 83


def test_merge_advances_last_update_monotonic():
    """Test 5: each merge refreshes last_update_monotonic to a strictly later time."""
    s = PrintState()
    s.merge({"mc_percent": 1})
    first = s.last_update_monotonic
    assert first > 0.0
    time.sleep(0.01)
    s.merge({"mc_percent": 2})
    assert s.last_update_monotonic > first


def test_hmm_formats_minutes_as_h_mm():
    """hmm() renders an integer count of MINUTES as h:mm (83 -> '1:23')."""
    assert hmm(83) == "1:23"
    assert hmm(0) == "0:00"
    assert hmm(5) == "0:05"
    assert hmm(60) == "1:00"
    assert hmm(125) == "2:05"
