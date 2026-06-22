"""Tests for src.render: the pure formatting + Pillow rendering layer.

These pin the locked format contract from 02-CONTEXT.md (icon text, Dutch
tooltip) and the 64x64 RGBA icon image properties. No pystray, no network --
rendering is verified by asserting on format strings and the produced PIL image.
"""

import os

from src.render import (
    ICON_SIZE,
    icon_text,
    is_active_print,
    render_for_display_state,
    render_for_state,
    render_icon,
    tooltip_for_display_state,
    tooltip_text,
)
from src.state import PrintState
from src.status import DisplayState


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


# --- render_icon / render_for_state ---------------------------------------


def _opaque_coords(img):
    """Return the set of (x, y) coords whose alpha channel is fully opaque (255)."""
    px = img.load()
    w, h = img.size
    return {(x, y) for x in range(w) for y in range(h) if px[x, y][3] == 255}


def test_render_icon_is_64x64_rgba():
    """render_icon returns a 64x64 RGBA PIL image."""
    img = render_icon("1:23")
    assert img.mode == "RGBA"
    assert img.size == (ICON_SIZE, ICON_SIZE) == (64, 64)


def test_render_icon_draws_centered_text():
    """Active text is drawn: opaque pixels exist in the central 16..48 band."""
    img = render_icon("1:23")
    opaque = _opaque_coords(img)
    assert opaque, "expected at least one fully-opaque pixel"
    central = [c for c in opaque if 16 <= c[0] <= 48 and 16 <= c[1] <= 48]
    assert central, "expected opaque pixels in the central region (text not centered)"


def test_render_icon_longest_string_fits_64px():
    """The widest expected strings ('12:34', '10u') stay within the 64px canvas."""
    for text in ("12:34", "10u"):
        img = render_icon(text)
        bbox = img.getbbox()
        assert bbox is not None
        assert bbox[2] <= ICON_SIZE and bbox[3] <= ICON_SIZE, f"{text} overflows: {bbox}"


def test_render_icon_idle_is_neutral_glyph():
    """render_icon(None) is a 64x64 RGBA neutral icon with opaque pixels and no digits."""
    img = render_icon(None)
    assert img.mode == "RGBA"
    assert img.size == (64, 64)
    assert _opaque_coords(img), "neutral icon must have a visible (opaque) glyph"
    # Differs from an active time icon (no digits drawn).
    assert _opaque_coords(img) != _opaque_coords(render_icon("1:23"))


def test_render_for_state_matches_icon_text():
    """render_for_state(state) draws the same pixels as render_icon(icon_text(state))."""
    active = _state(mc_percent=47, mc_remaining_time=83)
    assert _opaque_coords(render_for_state(active)) == _opaque_coords(
        render_icon(icon_text(active))
    )
    idle = _state(gcode_state="IDLE")
    assert _opaque_coords(render_for_state(idle)) == _opaque_coords(render_icon(None))


def test_bundled_font_exists():
    """The bundled font ships in assets/ (no system font path dependency)."""
    font_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "assets", "DejaVuSans.ttf"
    )
    assert os.path.exists(font_path)


# --- render_for_display_state (5-state glyphs) -----------------------------


def _running_state():
    return _state(gcode_state="RUNNING", mc_percent=47, mc_remaining_time=83)


def test_render_for_display_state_is_64x64_rgba_for_all_states():
    """Every display state renders a 64x64 RGBA image with visible opaque pixels."""
    st = _running_state()
    for ds in DisplayState:
        img = render_for_display_state(ds, st)
        assert img.mode == "RGBA"
        assert img.size == (ICON_SIZE, ICON_SIZE) == (64, 64)
        assert _opaque_coords(img), f"{ds} produced no opaque glyph"


def test_render_for_display_state_active_and_idle_delegate():
    """ACTIVE_PRINT reuses the time-digit render; NO_ACTIVE_PRINT reuses the neutral dot."""
    st = _running_state()
    assert _opaque_coords(
        render_for_display_state(DisplayState.ACTIVE_PRINT, st)
    ) == _opaque_coords(render_for_state(st))
    assert _opaque_coords(
        render_for_display_state(DisplayState.NO_ACTIVE_PRINT, st)
    ) == _opaque_coords(render_icon(None))


def test_display_states_are_visually_distinct():
    """All five states produce pairwise-distinct opaque-pixel sets (legibility)."""
    st = _running_state()
    coords = {ds: _opaque_coords(render_for_display_state(ds, st)) for ds in DisplayState}
    states = list(DisplayState)
    for i in range(len(states)):
        for j in range(i + 1, len(states)):
            a, b = states[i], states[j]
            assert coords[a] != coords[b], f"{a} and {b} render identically"


def test_render_for_display_state_glyphs_fit_canvas():
    """Each non-active glyph stays within the 64x64 canvas (getbbox in bounds)."""
    st = _running_state()
    for ds in DisplayState:
        img = render_for_display_state(ds, st)
        bbox = img.getbbox()
        assert bbox is not None
        assert bbox[2] <= ICON_SIZE and bbox[3] <= ICON_SIZE, f"{ds} overflows: {bbox}"
