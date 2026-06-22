"""Tests for src.status: the single pure 5-state display derivation seam.

These pin the locked 5-state machine from 03-CONTEXT.md: each
(PrintState + ConnectionStatus + injected monotonic clock) maps to exactly
one DisplayState, and each DisplayState maps to its exact locked Dutch
tooltip. No network, no threads, no real waits -- the freshness clock is
injected as a plain float, mirroring tests/test_render.py's style.
"""

from src.state import PrintState
from src.status import (
    FRESHNESS_TIMEOUT_SECONDS,
    ConnectionStatus,
    DisplayState,
    derive_display_state,
)

# A fixed "now" on the injected monotonic clock; all timestamps are relative.
NOW = 1000.0


def _state(
    gcode_state="RUNNING",
    mc_percent=0,
    mc_remaining_time=0,
    last_update_monotonic=NOW,
):
    """Build a PrintState; last_update defaults to fresh (== NOW)."""
    return PrintState(
        gcode_state=gcode_state,
        mc_percent=mc_percent,
        mc_remaining_time=mc_remaining_time,
        last_update_monotonic=last_update_monotonic,
    )


# --- derive_display_state: the full 5-state table --------------------------


def test_active_connected_is_active_print():
    """CONNECTED + active gcode_state -> ACTIVE_PRINT."""
    for g in ("RUNNING", "running", "PAUSE", "Pause"):
        st = _state(gcode_state=g)
        assert (
            derive_display_state(st, ConnectionStatus.CONNECTED, NOW)
            is DisplayState.ACTIVE_PRINT
        )


def test_idle_fresh_connected_is_no_active_print():
    """CONNECTED + idle gcode_state + fresh report -> NO_ACTIVE_PRINT."""
    for g in ("IDLE", "FINISH", "FAILED", "idle", "finish"):
        st = _state(gcode_state=g, last_update_monotonic=NOW)
        assert (
            derive_display_state(st, ConnectionStatus.CONNECTED, NOW)
            is DisplayState.NO_ACTIVE_PRINT
        )


def test_idle_stale_connected_is_printer_offline():
    """CONNECTED + idle gcode_state but NO fresh report -> PRINTER_OFFLINE."""
    stale = NOW - (FRESHNESS_TIMEOUT_SECONDS + 1)
    st = _state(gcode_state="IDLE", last_update_monotonic=stale)
    assert (
        derive_display_state(st, ConnectionStatus.CONNECTED, NOW)
        is DisplayState.PRINTER_OFFLINE
    )


def test_disconnected_is_cloud_disconnected():
    """DISCONNECTED beats any gcode_state -> CLOUD_DISCONNECTED."""
    st = _state(gcode_state="RUNNING")
    assert (
        derive_display_state(st, ConnectionStatus.DISCONNECTED, NOW)
        is DisplayState.CLOUD_DISCONNECTED
    )


def test_token_expired_is_token_expired():
    """TOKEN_EXPIRED is the most-severe state and wins over everything."""
    st = _state(gcode_state="RUNNING")
    assert (
        derive_display_state(st, ConnectionStatus.TOKEN_EXPIRED, NOW)
        is DisplayState.TOKEN_EXPIRED
    )


# --- freshness boundary ----------------------------------------------------


def test_freshness_boundary_exactly_at_timeout_is_fresh():
    """A report exactly FRESHNESS_TIMEOUT_SECONDS old is still fresh (<=)."""
    boundary = NOW - FRESHNESS_TIMEOUT_SECONDS
    st = _state(gcode_state="IDLE", last_update_monotonic=boundary)
    assert (
        derive_display_state(st, ConnectionStatus.CONNECTED, NOW)
        is DisplayState.NO_ACTIVE_PRINT
    )


def test_active_print_shows_even_if_slightly_stale():
    """Active gcode_state stays ACTIVE_PRINT even when slightly stale.

    Active prints stream reports continuously; staleness there is the
    disconnect path, not the offline branch. Offline only applies to idle.
    """
    stale = NOW - (FRESHNESS_TIMEOUT_SECONDS + 5)
    st = _state(gcode_state="RUNNING", last_update_monotonic=stale)
    assert (
        derive_display_state(st, ConnectionStatus.CONNECTED, NOW)
        is DisplayState.ACTIVE_PRINT
    )


# --- guard tests -----------------------------------------------------------


def test_idle_vs_offline_are_distinct():
    """Same CONNECTED + IDLE: fresh -> NO_ACTIVE_PRINT, stale -> PRINTER_OFFLINE."""
    fresh = _state(gcode_state="IDLE", last_update_monotonic=NOW)
    stale = _state(
        gcode_state="IDLE",
        last_update_monotonic=NOW - (FRESHNESS_TIMEOUT_SECONDS + 1),
    )
    fresh_state = derive_display_state(fresh, ConnectionStatus.CONNECTED, NOW)
    stale_state = derive_display_state(stale, ConnectionStatus.CONNECTED, NOW)
    assert fresh_state is DisplayState.NO_ACTIVE_PRINT
    assert stale_state is DisplayState.PRINTER_OFFLINE
    assert fresh_state is not stale_state


def test_never_received_report_is_offline_not_idle():
    """last_update_monotonic=0.0 (no report) while CONNECTED -> PRINTER_OFFLINE."""
    st = _state(gcode_state="IDLE", last_update_monotonic=0.0)
    assert (
        derive_display_state(st, ConnectionStatus.CONNECTED, NOW)
        is DisplayState.PRINTER_OFFLINE
    )


def test_garbage_gcode_state_does_not_raise_and_is_safe():
    """An unknown/garbage gcode_state falls through to a safe non-active branch."""
    st = _state(gcode_state="!!garbage!!", last_update_monotonic=NOW)
    # Fresh garbage is not active and not idle-listed -> safe NO_ACTIVE_PRINT.
    assert (
        derive_display_state(st, ConnectionStatus.CONNECTED, NOW)
        is DisplayState.NO_ACTIVE_PRINT
    )


# --- enum shape ------------------------------------------------------------


def test_display_state_has_exactly_five_members():
    assert len(DisplayState) == 5
    assert {m.name for m in DisplayState} == {
        "ACTIVE_PRINT",
        "NO_ACTIVE_PRINT",
        "PRINTER_OFFLINE",
        "CLOUD_DISCONNECTED",
        "TOKEN_EXPIRED",
    }


def test_connection_status_has_exactly_three_members():
    assert len(ConnectionStatus) == 3
    assert {m.name for m in ConnectionStatus} == {
        "CONNECTED",
        "DISCONNECTED",
        "TOKEN_EXPIRED",
    }


def test_freshness_timeout_is_thirty():
    assert FRESHNESS_TIMEOUT_SECONDS == 30
