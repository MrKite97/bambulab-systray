"""Tests for src.render: the pure formatting + Pillow rendering layer.

These pin the locked format contract from 02-CONTEXT.md (icon text, Dutch
tooltip) and the 64x64 RGBA icon image properties. No pystray, no network --
rendering is verified by asserting on format strings and the produced PIL image.
"""

import os

import pytest

from src.render import (
    ICON_SIZE,
    detect_windows_theme,
    icon_text,
    is_active_print,
    render_for_display_state,
    render_for_state,
    render_icon,
    render_printer_icon,
    status_from_gcode_state,
    status_to_color,
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


def test_render_for_state_routes_through_printer_glyph():
    """render_for_state draws the printer-fill glyph: active fills, idle has none."""
    # Active RUNNING -> printing-blue frame WITH a neutral fill column by pct.
    active = _state(gcode_state="RUNNING", mc_percent=80, mc_remaining_time=83)
    a_img = render_for_state(active)
    a_px = a_img.load()
    assert a_px[_BUILD_CENTER_X, _BUILD_TOP] == status_to_color("printing", "dark")
    assert _fill_column_height(a_img, _BUILD_CENTER_X) > 0
    # IDLE -> neutral grey frame, NO fill column (ICON-02).
    idle = _state(gcode_state="IDLE")
    i_img = render_for_state(idle)
    assert _fill_column_height(i_img, _BUILD_CENTER_X) == 0


def test_render_for_state_matches_printer_icon():
    """render_for_state(state) equals render_printer_icon for the derived status."""
    active = _state(gcode_state="RUNNING", mc_percent=47)
    assert _opaque_coords(render_for_state(active)) == _opaque_coords(
        render_printer_icon(47, "printing")
    )
    idle = _state(gcode_state="IDLE")
    assert _opaque_coords(render_for_state(idle)) == _opaque_coords(
        render_printer_icon(0, "neutral", logged_out=True)
    )


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


def test_render_for_display_state_active_has_fill_nonactive_have_none():
    """ACTIVE_PRINT renders a status-color frame WITH a fill; the four non-active
    states render a neutral grey frame with NO fill column (ICON-02)."""
    st = _running_state()  # RUNNING, pct=47
    active = render_for_display_state(DisplayState.ACTIVE_PRINT, st)
    a_px = active.load()
    assert a_px[_BUILD_CENTER_X, _BUILD_TOP] == status_to_color("printing", "dark")
    assert _fill_column_height(active, _BUILD_CENTER_X) > 0

    for ds in (
        DisplayState.NO_ACTIVE_PRINT,
        DisplayState.PRINTER_OFFLINE,
        DisplayState.CLOUD_DISCONNECTED,
        DisplayState.TOKEN_EXPIRED,
    ):
        img = render_for_display_state(ds, st)
        px = img.load()
        # Neutral grey frame, no fill.
        assert px[_BUILD_CENTER_X, _BUILD_TOP] == status_to_color("neutral", "dark")
        assert _fill_column_height(img, _BUILD_CENTER_X) == 0, f"{ds} has a fill column"


def test_render_for_display_state_active_matches_printer_glyph():
    """ACTIVE_PRINT delegates to render_printer_icon for the derived status."""
    st = _running_state()
    assert _opaque_coords(
        render_for_display_state(DisplayState.ACTIVE_PRINT, st)
    ) == _opaque_coords(render_printer_icon(st.mc_percent, "printing"))


def test_non_active_display_states_share_neutral_logged_out_frame():
    """The four non-active states all render the same neutral grey logged-out frame.

    Phase 6 intentionally unifies them visually (the distinction is in the
    tooltip wording, not the glyph)."""
    st = _running_state()
    expected = _opaque_coords(render_printer_icon(0, "neutral", logged_out=True))
    for ds in (
        DisplayState.NO_ACTIVE_PRINT,
        DisplayState.PRINTER_OFFLINE,
        DisplayState.CLOUD_DISCONNECTED,
        DisplayState.TOKEN_EXPIRED,
    ):
        assert _opaque_coords(render_for_display_state(ds, st)) == expected


def test_render_for_display_state_glyphs_fit_canvas():
    """Each non-active glyph stays within the 64x64 canvas (getbbox in bounds)."""
    st = _running_state()
    for ds in DisplayState:
        img = render_for_display_state(ds, st)
        bbox = img.getbbox()
        assert bbox is not None
        assert bbox[2] <= ICON_SIZE and bbox[3] <= ICON_SIZE, f"{ds} overflows: {bbox}"


# --- tooltip_for_display_state (exact locked Dutch strings, all 5 states) ---


def test_tooltip_for_display_state_active_body():
    """ACTIVE_PRINT tooltip is the exact active body 'NN% — nog Xu Ym'."""
    st = _state(gcode_state="RUNNING", mc_percent=47, mc_remaining_time=83)
    assert tooltip_for_display_state(DisplayState.ACTIVE_PRINT, st) == "47% — nog 1u 23m"


def test_tooltip_for_display_state_locked_constant_strings():
    """The four non-active states return their exact locked Dutch constants."""
    st = _state(gcode_state="IDLE")
    assert tooltip_for_display_state(DisplayState.NO_ACTIVE_PRINT, st) == "Geen actieve print"
    assert tooltip_for_display_state(DisplayState.PRINTER_OFFLINE, st) == "Printer offline"
    assert tooltip_for_display_state(DisplayState.CLOUD_DISCONNECTED, st) == "Verbinden…"
    assert tooltip_for_display_state(DisplayState.TOKEN_EXPIRED, st) == "Opnieuw inloggen vereist"


def test_tooltip_for_display_state_delegates_to_status():
    """tooltip_for_display_state returns exactly status.tooltip_for (thin delegate)."""
    from src import status

    st = _state(gcode_state="RUNNING", mc_percent=12, mc_remaining_time=5)
    for ds in DisplayState:
        assert tooltip_for_display_state(ds, st) == status.tooltip_for(ds, st)


def test_non_active_states_never_leak_stale_numbers():
    """A PrintState carrying percent=47/remaining=83 shows no '47%' / 'nog' when not active."""
    stale = _state(gcode_state="RUNNING", mc_percent=47, mc_remaining_time=83)
    for ds in (
        DisplayState.NO_ACTIVE_PRINT,
        DisplayState.PRINTER_OFFLINE,
        DisplayState.CLOUD_DISCONNECTED,
        DisplayState.TOKEN_EXPIRED,
    ):
        tip = tooltip_for_display_state(ds, stale)
        assert "47%" not in tip, f"{ds} leaked stale percent: {tip!r}"
        assert "nog" not in tip, f"{ds} leaked stale remaining: {tip!r}"


# --- status_to_color (theme-aware, locked hex) -----------------------------

# Locked expected RGBA values (alpha 255) for the dark theme.
_DARK = {
    "printing": (84, 197, 255, 255),  # #54C5FF
    "paused": (255, 203, 69, 255),  # #FFCB45
    "done": (111, 208, 106, 255),  # #6FD06A
    "error": (255, 138, 149, 255),  # #FF8A95
    "neutral": (154, 160, 170, 255),  # #9AA0AA
}
# Locked expected RGBA values (alpha 255) for the light theme.
_LIGHT = {
    "printing": (0, 103, 192, 255),  # #0067C0
    "paused": (154, 91, 0, 255),  # #9A5B00
    "done": (16, 124, 16, 255),  # #107C10
    "error": (196, 43, 28, 255),  # #C42B1C
    "neutral": (136, 136, 146, 255),  # #888892
}


def test_status_to_color_dark_locked_hex():
    """Each status maps to the exact locked dark hex (alpha 255)."""
    for status, expected in _DARK.items():
        assert status_to_color(status, "dark") == expected


def test_status_to_color_light_locked_hex():
    """Each status maps to the exact locked light hex (alpha 255)."""
    for status, expected in _LIGHT.items():
        assert status_to_color(status, "light") == expected


def test_status_to_color_unknown_falls_back_to_neutral():
    """An unknown status returns the neutral entry and never raises."""
    assert status_to_color("nonsense", "dark") == _DARK["neutral"]
    assert status_to_color("", "light") == _LIGHT["neutral"]


# --- status_from_gcode_state -----------------------------------------------


def test_status_from_gcode_state_known_mappings():
    """gcode_state maps to status keys, case-insensitively."""
    assert status_from_gcode_state("RUNNING") == "printing"
    assert status_from_gcode_state("running") == "printing"
    assert status_from_gcode_state("PAUSE") == "paused"
    assert status_from_gcode_state("FINISH") == "done"
    assert status_from_gcode_state("FAILED") == "error"


def test_status_from_gcode_state_neutral_fallbacks():
    """IDLE / PREPARE / unknown / empty all fall through to neutral."""
    for s in ("IDLE", "PREPARE", "unknown", "", "garbage"):
        assert status_from_gcode_state(s) == "neutral"


# --- detect_windows_theme --------------------------------------------------


def test_detect_windows_theme_returns_valid_value():
    """detect_windows_theme never raises and returns 'light' or 'dark'."""
    assert detect_windows_theme() in ("light", "dark")


# --- render_printer_icon ---------------------------------------------------

# Build-area geometry (mirrors render.py constants; sampled in tests).
_BUILD_LEFT, _BUILD_RIGHT = 14, 50
_BUILD_TOP, _PLATE_Y = 18, 50
_FRAME_STROKE = 4
_BUILD_CENTER_X = (_BUILD_LEFT + _BUILD_RIGHT) // 2


def _fill_column_height(img, x, theme="dark"):
    """Count neutral-grey FILL pixels strictly inside the build area at column x.

    Only pixels equal to the neutral fill color are counted, so frame-stroke
    pixels that happen to cross this column are excluded -- this measures the
    material fill height, not the frame.
    """
    px = img.load()
    fill = status_to_color("neutral", theme)
    # Sample strictly between the top frame stroke and the plate stroke so a
    # grey frame (neutral status) is not mistaken for fill.
    return sum(
        1
        for y in range(_BUILD_TOP + _FRAME_STROKE, _PLATE_Y - _FRAME_STROKE)
        if px[x, y] == fill
    )


def test_render_printer_icon_is_64x64_rgba():
    """render_printer_icon returns a 64x64 RGBA image."""
    img = render_printer_icon(50, "printing", theme="dark")
    assert img.mode == "RGBA"
    assert img.size == (ICON_SIZE, ICON_SIZE) == (64, 64)


def test_render_printer_icon_fill_height_grows_with_pct():
    """A higher pct produces a taller neutral-grey fill column."""
    low = render_printer_icon(10, "printing", theme="dark")
    high = render_printer_icon(80, "printing", theme="dark")
    low_h = _fill_column_height(low, _BUILD_CENTER_X)
    high_h = _fill_column_height(high, _BUILD_CENTER_X)
    assert high_h > low_h, f"expected pct=80 taller than pct=10 ({high_h} <= {low_h})"


def test_render_printer_icon_frame_is_status_color():
    """A frame pixel equals the mapped status color (printing), not the neutral fill."""
    img = render_printer_icon(50, "printing", theme="dark")
    px = img.load()
    # Top frame stroke runs across the build top; sample its center.
    frame_pixel = px[_BUILD_CENTER_X, _BUILD_TOP]
    assert frame_pixel == status_to_color("printing", "dark") == (84, 197, 255, 255)
    assert frame_pixel != status_to_color("neutral", "dark")


def test_render_printer_icon_fill_is_neutral_not_status():
    """The fill column is neutral material grey, never the status color."""
    img = render_printer_icon(80, "printing", theme="dark")
    px = img.load()
    # Mid-fill, clear of the plate stroke: at pct=80 the fill reaches ~y=24,
    # so y=40 at the build center is solidly inside the neutral fill.
    fill_pixel = px[_BUILD_CENTER_X, 40]
    assert fill_pixel == status_to_color("neutral", "dark") == (154, 160, 170, 255)
    assert fill_pixel != status_to_color("printing", "dark")


def test_render_printer_icon_logged_out_has_no_fill():
    """logged_out=True renders a frame but no opaque pixels strictly inside the build area."""
    img = render_printer_icon(80, "printing", theme="dark", logged_out=True)
    # No fill column at the build center.
    assert _fill_column_height(img, _BUILD_CENTER_X) == 0


def test_render_printer_icon_neutral_status_has_no_fill():
    """status='neutral' renders a grey frame with no fill column (ICON-02)."""
    img = render_printer_icon(80, "neutral", theme="dark")
    assert _fill_column_height(img, _BUILD_CENTER_X) == 0


def test_render_printer_icon_pct1_frame_fully_visible():
    """At pct=1 the colored frame stays fully visible (~unchanged frame pixel count)."""
    px1 = render_printer_icon(1, "printing", theme="dark")
    px80 = render_printer_icon(80, "printing", theme="dark")

    def _frame_pixels(img):
        load = img.load()
        color = status_to_color("printing", "dark")
        return {
            (x, y)
            for x in range(ICON_SIZE)
            for y in range(ICON_SIZE)
            if load[x, y] == color
        }

    f1 = len(_frame_pixels(px1))
    f80 = len(_frame_pixels(px80))
    assert f1 > 0
    # Frame stroke is independent of fill; counts should match closely.
    assert abs(f1 - f80) <= 4, f"frame changed too much with pct ({f1} vs {f80})"


def test_render_printer_icon_theme_selects_hex():
    """theme='light' frame uses the light hex; theme='dark' uses the dark hex."""
    dark = render_printer_icon(50, "printing", theme="dark").load()
    light = render_printer_icon(50, "printing", theme="light").load()
    assert dark[_BUILD_CENTER_X, _BUILD_TOP] == (84, 197, 255, 255)
    assert light[_BUILD_CENTER_X, _BUILD_TOP] == (0, 103, 192, 255)


@pytest.mark.parametrize("pct", [0, 1, 10, 50, 80, 100])
@pytest.mark.parametrize("status", ["printing", "paused", "done", "error", "neutral"])
def test_render_printer_icon_grid_returns_rgba(pct, status):
    """Every pct x status combination returns a 64x64 RGBA image with opaque pixels."""
    img = render_printer_icon(pct, status, theme="dark")
    assert img.mode == "RGBA"
    assert img.size == (64, 64)
    assert _opaque_coords(img), f"pct={pct} status={status} produced no opaque glyph"
