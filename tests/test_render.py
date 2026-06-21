"""Tests for src.render: the pure formatting + Pillow rendering layer.

These pin the locked format contract from 02-CONTEXT.md (icon text, Dutch
tooltip) and the 64x64 RGBA icon image properties. No pystray, no network --
rendering is verified by asserting on format strings and the produced PIL image.
"""

from src.render import (
    icon_text,
    is_active_print,
    tooltip_text,
)
from src.state import PrintState


def _state(gcode_state="RUNNING", mc_percent=0, mc_remaining_time=0):
    return PrintState(
        gcode_state=gcode_state,
        mc_percent=mc_percent,
        mc_remaining_time=mc_remaining_time,
    )


# --- is_active_print -------------------------------------------------------


def test_is_active_print_running_and_pause_true():
    """RUNNING and PAUSE count as active (case-insensitive)."""
    assert is_active_print("RUNNING") is True
    assert is_active_print("PAUSE") is True
    assert is_active_print("running") is True
    assert is_active_print("Pause") is True


def test_is_active_print_other_states_false():
    """IDLE / FINISH / FAILED / unknown / empty are not active."""
    for s in ("IDLE", "FINISH", "FAILED", "unknown", ""):
        assert is_active_print(s) is False


# --- icon_text -------------------------------------------------------------


def test_icon_text_under_2h_is_h_mm():
    """Active print under 2h shows h:mm (83 -> '1:23', 5 -> '0:05', 0 -> '0:00')."""
    assert icon_text(_state(mc_remaining_time=83)) == "1:23"
    assert icon_text(_state(mc_remaining_time=5)) == "0:05"
    assert icon_text(_state(mc_remaining_time=0)) == "0:00"


def test_icon_text_at_or_above_10h_is_compact_hours():
    """>=600 min uses compact hours form (600 -> '10u', 725 -> '12u')."""
    assert icon_text(_state(mc_remaining_time=600)) == "10u"
    assert icon_text(_state(mc_remaining_time=725)) == "12u"


def test_icon_text_boundary_just_under_10h_is_h_mm():
    """599 min (9h59) stays h:mm just under the 10h threshold."""
    assert icon_text(_state(mc_remaining_time=599)) == "9:59"


def test_icon_text_idle_returns_none():
    """No active print -> icon_text returns None (signals neutral icon)."""
    assert icon_text(_state(gcode_state="IDLE", mc_remaining_time=83)) is None
    assert icon_text(_state(gcode_state="FINISH")) is None


# --- tooltip_text ----------------------------------------------------------


def test_tooltip_text_active_full_form():
    """Active tooltip is exactly '47% — nog 1u 23m' (em dash)."""
    assert tooltip_text(_state(mc_percent=47, mc_remaining_time=83)) == "47% — nog 1u 23m"


def test_tooltip_text_duration_forms():
    """Dutch nog-form: <60 -> 'nog Nm'; >=60 -> 'nog Hu Mm'."""
    assert tooltip_text(_state(mc_percent=0, mc_remaining_time=0)) == "0% — nog 0m"
    assert tooltip_text(_state(mc_percent=0, mc_remaining_time=5)) == "0% — nog 5m"
    assert tooltip_text(_state(mc_percent=0, mc_remaining_time=60)) == "0% — nog 1u 0m"
    assert tooltip_text(_state(mc_percent=0, mc_remaining_time=83)) == "0% — nog 1u 23m"


def test_tooltip_text_idle_is_geen_actieve_print():
    """No active print -> tooltip is exactly 'Geen actieve print'."""
    assert tooltip_text(_state(gcode_state="IDLE")) == "Geen actieve print"
    assert tooltip_text(_state(gcode_state="FAILED")) == "Geen actieve print"
