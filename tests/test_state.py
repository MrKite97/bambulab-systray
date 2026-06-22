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
    # Windows time.monotonic() resolution is ~15.6ms; sleep comfortably past it
    # so the second merge lands on a strictly later tick.
    time.sleep(0.05)
    s.merge({"mc_percent": 2})
    assert s.last_update_monotonic > first


def test_hmm_formats_minutes_as_h_mm():
    """hmm() renders an integer count of MINUTES as h:mm (83 -> '1:23')."""
    assert hmm(83) == "1:23"
    assert hmm(0) == "0:00"
    assert hmm(5) == "0:05"
    assert hmm(60) == "1:00"
    assert hmm(125) == "2:05"


# --- Phase 5 Task 1: layer + file fields (v2 panel telemetry) ---


def test_layer_fields_merge():
    """A report carrying layer_num/total_layer_num exposes both ints."""
    s = PrintState()
    s.merge({"layer_num": 132, "total_layer_num": 198})
    assert s.layer_num == 132
    assert s.total_layer_num == 198


def test_layer_fields_default_zero():
    """Fresh PrintState has layer_num/total_layer_num == 0."""
    s = PrintState()
    assert s.layer_num == 0
    assert s.total_layer_num == 0


def test_partial_delta_leaves_total_layer_unchanged():
    """A partial delta {layer_num} leaves total_layer_num at its prior value."""
    s = PrintState()
    s.merge({"layer_num": 132, "total_layer_num": 198})
    s.merge({"layer_num": 7})
    assert s.layer_num == 7  # updated
    assert s.total_layer_num == 198  # unchanged, NOT reset to 0


def test_none_layer_num_does_not_clobber():
    """An explicit None layer_num must not clobber a prior good value."""
    s = PrintState()
    s.merge({"layer_num": 132})
    s.merge({"layer_num": None})
    assert s.layer_num == 132


def test_gcode_file_stores_basename():
    """gcode_file with a path stores only the basename (path stripped)."""
    s = PrintState()
    s.merge({"gcode_file": "Metadata/plate_1.gcode"})
    assert s.gcode_file == "plate_1.gcode"


def test_gcode_file_strips_deep_path():
    """gcode_file with a deep path stores only the basename."""
    s = PrintState()
    s.merge({"gcode_file": "/cache/sub/3DBenchy.gcode"})
    assert s.gcode_file == "3DBenchy.gcode"


def test_subtask_name_basename_unchanged_when_already_bare():
    """subtask_name already a basename is stored unchanged (extension kept)."""
    s = PrintState()
    s.merge({"subtask_name": "3DBenchy.gcode"})
    assert s.subtask_name == "3DBenchy.gcode"


def test_file_fields_default_empty():
    """Fresh PrintState has gcode_file/subtask_name == ''."""
    s = PrintState()
    assert s.gcode_file == ""
    assert s.subtask_name == ""


def test_partial_delta_leaves_gcode_file_unchanged():
    """Missing gcode_file leaves prior value; None does not clobber."""
    s = PrintState()
    s.merge({"gcode_file": "plate_1.gcode"})
    s.merge({"layer_num": 5})  # gcode_file absent
    assert s.gcode_file == "plate_1.gcode"  # unchanged
    s.merge({"gcode_file": None})  # explicit None
    assert s.gcode_file == "plate_1.gcode"  # still not clobbered
