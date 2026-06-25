"""Pure formatting + rendering layer for the tray (no pystray, no network, no threads).

This module turns a :class:`~src.state.PrintState` into the three user-visible
artifacts of the tray:

1. a compact icon-text string (remaining time, e.g. ``1:23`` / ``10u``),
2. a Dutch hover tooltip string (``47% — nog 1u 23m`` / ``Geen actieve print``),
3. a 64x64 RGBA :class:`PIL.Image.Image` with that text drawn on it.

The exact format strings are LOCKED by ``02-CONTEXT.md`` and asserted verbatim in
``tests/test_render.py``. This layer reads ONLY ``gcode_state`` / ``mc_percent`` /
``mc_remaining_time`` from PrintState -- PrintState carries no token, so no secret
can ever reach the tooltip/icon, and nothing here logs.

"Active print" detection here is intentionally minimal (this phase only): the full
5-state machine (offline / disconnected / token-expired) is Phase 3.
"""

from PIL import Image, ImageDraw, ImageFont

from src.paths import resource_path
from src.state import PrintState, hmm

ACTIVE_STATES = {"RUNNING", "PAUSE"}
# At/above this remaining time we switch the icon to a compact whole-hours form
# ("10u") so it stays legible when Windows downscales to the tray size.
HOURS_COMPACT_THRESHOLD_MIN = 600  # 10h


def is_active_print(gcode_state: str) -> bool:
    """True when the printer is actively printing (RUNNING/PAUSE, case-insensitive)."""
    return (gcode_state or "").upper() in ACTIVE_STATES


def icon_text(state: PrintState) -> str | None:
    """Compact text for the tray icon, or None when there is no active print."""
    if not is_active_print(state.gcode_state):
        return None
    minutes = max(0, state.mc_remaining_time)
    if minutes >= HOURS_COMPACT_THRESHOLD_MIN:
        return f"{minutes // 60}u"  # e.g. "10u"
    return hmm(minutes)  # e.g. "1:23" -- reuse the single h:mm formatter


def _dutch_duration(minutes: int) -> str:
    """nog-form: <60 -> 'nog 5m'; >=60 -> 'nog 1u 23m'."""
    minutes = max(0, minutes)
    if minutes < 60:
        return f"nog {minutes}m"
    return f"nog {minutes // 60}u {minutes % 60}m"


def tooltip_text(state: PrintState) -> str:
    """Dutch hover tooltip: '47% — nog 1u 23m' active, 'Geen actieve print' idle."""
    if not is_active_print(state.gcode_state):
        return "Geen actieve print"
    return f"{state.mc_percent}% — {_dutch_duration(state.mc_remaining_time)}"


# --- Pillow rendering ------------------------------------------------------
# We render at 64x64 and let Windows downscale to the tray size (crisper than
# drawing directly at 16x16). The font is bundled in assets/ so the frozen .exe
# never depends on a system-installed font path.

ICON_SIZE = 64
_ICON_FONT_SIZE = 34
_FONT_PATH = resource_path("assets/DejaVuSans.ttf")


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    """Load the bundled DejaVuSans font (never a system font path)."""
    return ImageFont.truetype(_FONT_PATH, size)


def render_icon(text: str | None) -> Image.Image:
    """Render a 64x64 RGBA tray icon.

    ``text`` -> centered white time digits (active print).
    ``None`` -> a neutral grey dot, no digits (no active print).
    """
    img = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if text is None:
        # Neutral idle glyph: a centered filled dot, no digits.
        d.ellipse((22, 22, 42, 42), fill=(180, 180, 180, 255))
        return img
    font = _load_font(_ICON_FONT_SIZE)
    bbox = d.textbbox((0, 0), text, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text(
        ((ICON_SIZE - w) / 2 - bbox[0], (ICON_SIZE - h) / 2 - bbox[1]),
        text,
        font=font,
        fill=(255, 255, 255, 255),
    )
    return img


# --- Printer-fill glyph (Phase 6) ------------------------------------------
# The tray icon is a printer-frame outline whose FRAME color is the status
# color (printing=blue, paused=amber, done=green, error=red) and whose build
# area FILLs bottom-to-top with neutral material grey by progress. Keeping the
# fill neutral (never the status color) means the colored contour stays legible
# even at pct=1, where the fill is a thin sliver. Logged-out / neutral renders
# the frame in grey with NO fill. Colors are theme-aware (Windows dark/light).
#
# Status-color tokens are LOCKED by 06-CONTEXT.md / the Design Tokens table.
# The literal hex strings are kept in source so the locked map stays greppable.

# dark theme: printing #54C5FF, paused #FFCB45, done #6FD06A, error #FF8A95,
#             neutral #9AA0AA  (neutral is also the fill material grey)
# light theme: printing #0067C0, paused #9A5B00, done #107C10, error #C42B1C,
#              neutral #888892
_STATUS_HEX = {
    "dark": {
        "printing": "#54C5FF",
        "paused": "#FFCB45",
        "done": "#6FD06A",
        "error": "#FF8A95",
        "neutral": "#9AA0AA",
    },
    "light": {
        "printing": "#0067C0",
        "paused": "#9A5B00",
        "done": "#107C10",
        "error": "#C42B1C",
        "neutral": "#888892",
    },
}


def _hex_to_rgba(hexstr: str) -> tuple[int, int, int, int]:
    """Parse a ``#RRGGBB`` string to an ``(r, g, b, 255)`` tuple (fully opaque)."""
    h = hexstr.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 255)


def status_to_color(status: str, theme: str) -> tuple[int, int, int, int]:
    """Map an app status + theme to its locked frame color (RGBA, alpha 255).

    ``status`` is one of printing/paused/done/error/neutral; any unknown value
    falls back to the theme's neutral grey (never raises). ``theme`` is
    ``"dark"`` or ``"light"`` (anything else is treated as dark).
    """
    table = _STATUS_HEX.get(theme, _STATUS_HEX["dark"])
    hexstr = table.get(status, table["neutral"])
    return _hex_to_rgba(hexstr)


def status_from_gcode_state(gcode_state: str) -> str:
    """Map a raw broker ``gcode_state`` to a status key (never raises).

    RUNNING -> "printing", PAUSE -> "paused", FINISH -> "done",
    FAILED -> "error"; IDLE / PREPARE / unknown / "" -> "neutral".
    Classification is ``.upper()``-guarded set membership so any garbage string
    falls through to "neutral" (mirrors :func:`is_active_print`).
    """
    s = (gcode_state or "").upper()
    if s == "RUNNING":
        return "printing"
    if s == "PAUSE":
        return "paused"
    if s == "FINISH":
        return "done"
    if s == "FAILED":
        return "error"
    return "neutral"


# Windows light/dark theme detection. The whole registry read is wrapped so a
# missing key, a non-Windows host, or an unavailable winreg never crashes the
# always-on tray -- it defaults to "dark".
_THEME_KEY = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"

try:  # winreg is stdlib on Windows, absent on non-Windows CI runners.
    import winreg  # type: ignore
except ImportError:  # pragma: no cover - non-Windows
    winreg = None  # type: ignore


def detect_windows_theme() -> str:
    """Return ``"light"`` or ``"dark"`` from the Windows Personalize registry key.

    Reads ``SystemUsesLightTheme`` (1 = light, 0 = dark). Any failure -- key
    missing, value missing, winreg unavailable, non-Windows -- returns ``"dark"``
    and never raises (the tray must keep running).
    """
    if winreg is None:
        return "dark"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _THEME_KEY) as k:
            val, _ = winreg.QueryValueEx(k, "SystemUsesLightTheme")
        return "light" if val == 1 else "dark"
    except Exception:
        return "dark"


# --- Printer tray glyph, recreated 1:1 from the design handoff -----------------
# Source: design_handoff_printer_tray/3D-printer voortgang.dc.html, the default
# "printer" tray SVG. The design draws in a 0..24 viewBox; the printer itself only
# occupies x3.3..20.7 / y3.8..20.4 of that box, leaving wide padding. Scaling the
# raw viewBox to 64px would carry that padding through and Windows would render a
# small glyph. Instead we FIT the printer's bounding box to (most of) the canvas
# with a tiny margin (_PRN_MARGIN), so it fills the tray cell like the system
# icons. The shape is a real printer: a top GANTRY bar, two side POSTS, a PRINT
# HEAD hanging from the gantry, and a BUILD PLATE -- all in the status color. The
# build area between them fills bottom-to-top with translucent MATERIAL grey
# (never the status color) by progress, so the colored contour stays legible even
# at a 1% sliver. Logged out / neutral: grey frame, no fill.

# Printer bounding box in design units (gantry-left .. plate-right etc.).
_PRN_BBOX = (3.3, 3.8, 20.7, 20.4)  # x0, y0, x1, y1  -> 17.4 wide, 16.6 tall
_PRN_MARGIN = 2.0  # px of breathing room around the glyph in the 64px canvas
_PRN_CX = 12.0     # design-space centre (viewBox + bbox centres ~coincide)
# Uniform scale that maps the larger bbox dimension to (canvas - 2*margin).
_PRN_FIT = (ICON_SIZE - 2 * _PRN_MARGIN) / (_PRN_BBOX[2] - _PRN_BBOX[0])


def _ps(v: float) -> float:
    """Design (0..24) COORDINATE -> 64px canvas coordinate (centred + fitted)."""
    return ICON_SIZE / 2.0 + (v - _PRN_CX) * _PRN_FIT


def _pl(v: float) -> float:
    """Design LENGTH (radius/width) -> px length (no centre offset)."""
    return v * _PRN_FIT


# Build area the printed object fills (design clipPath rect x6 y6.5 w12 h11.5).
# The bottom equals the build-plate top, so the fill runs flush to the plate.
_PRN_BUILD_X = 6.0
_PRN_BUILD_W = 12.0
_PRN_BUILD_TOP = 6.5
_PRN_BUILD_BOTTOM = 18.0
_PRN_BUILD_H = _PRN_BUILD_BOTTOM - _PRN_BUILD_TOP  # 11.5

# Translucent printed-material grey (design ``printFill``): light-on-dark /
# dark-on-light, kept DISTINCT from the neutral FRAME grey so the contour wins.
_MATERIAL_FILL = {
    "dark": (232, 232, 236, 184),   # rgba(232,232,236,.72)
    "light": (58, 58, 66, 140),     # rgba(58,58,66,.55)
}


def render_printer_icon(
    pct: int,
    status: str,
    *,
    theme: str | None = None,
    logged_out: bool = False,
) -> Image.Image:
    """Render the printer tray glyph (design "printer" style) as 64x64 RGBA.

    The printer FRAME (gantry bar + two posts + print head + build plate) is
    drawn in ``status_to_color(status, theme)`` -- or neutral grey when
    ``logged_out``. The build area fills bottom-to-top with translucent MATERIAL
    grey to ``pct`` of its height, clipped to the build area and flush to the
    plate. ``pct`` is clamped 0..100. The fill is drawn ONLY for an active status
    (not ``logged_out`` and ``status != "neutral"``) with non-zero height;
    logged-out / neutral renders the grey frame with no fill. Recreated 1:1 from
    the design handoff SVG (scaled from its 24-unit viewBox to 64px).
    """
    theme = theme or detect_windows_theme()
    if theme not in _MATERIAL_FILL:
        theme = "dark"
    frame_color = status_to_color("neutral" if logged_out else status, theme)
    material = _MATERIAL_FILL[theme]

    img = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Build-area fill FIRST (so the frame + print head drawn next win on overlap,
    # keeping the colored contour legible even at high fill).
    if not logged_out and status != "neutral":
        p = max(0, min(100, pct)) / 100.0
        fill_h = _PRN_BUILD_H * p
        if fill_h > 0:
            top = max(_PRN_BUILD_TOP, _PRN_BUILD_BOTTOM - fill_h)
            r = min(1.0, fill_h / 2.0, _PRN_BUILD_W / 2.0)  # clamp so PIL never errors
            d.rounded_rectangle(
                [_ps(_PRN_BUILD_X), _ps(top),
                 _ps(_PRN_BUILD_X + _PRN_BUILD_W), _ps(_PRN_BUILD_BOTTOM)],
                radius=_pl(r), fill=material,
            )

    # Top gantry bar (design rect x4 y3.8 w16 h2 rx1).
    d.rounded_rectangle([_ps(4), _ps(3.8), _ps(20), _ps(5.8)], radius=_pl(1.0), fill=frame_color)
    # Two side posts (design path "M5 5.5 V18" / "M19 5.5 V18", stroke 1.9 round).
    _hw = 1.9 / 2.0
    d.rounded_rectangle([_ps(5 - _hw), _ps(5.5), _ps(5 + _hw), _ps(18)], radius=_pl(_hw), fill=frame_color)
    d.rounded_rectangle([_ps(19 - _hw), _ps(5.5), _ps(19 + _hw), _ps(18)], radius=_pl(_hw), fill=frame_color)
    # Print head hanging from the gantry (design rect x10.4 y5.4 w3.2 h2.6 rx.7).
    d.rounded_rectangle([_ps(10.4), _ps(5.4), _ps(13.6), _ps(8.0)], radius=_pl(0.7), fill=frame_color)
    # Build plate / base (design rect x3.3 y18 w17.4 h2.4 rx1).
    d.rounded_rectangle([_ps(3.3), _ps(18), _ps(20.7), _ps(20.4)], radius=_pl(1.0), fill=frame_color)

    return img


def render_for_state(state: PrintState) -> Image.Image:
    """Icon for a PrintState: the printer-fill glyph driven by gcode_state + pct.

    Status is derived from ``gcode_state`` (RUNNING->printing, PAUSE->paused,
    FINISH->done, FAILED->error, idle/unknown->neutral). The neutral case
    renders a grey frame with NO fill; active states fill by ``mc_percent``.
    ``render_icon`` / ``icon_text`` remain as the legacy text fallback path.
    """
    status = status_from_gcode_state(state.gcode_state)
    logged_out = status == "neutral"
    return render_printer_icon(state.mc_percent, status, logged_out=logged_out)


# --- 5-state display rendering (Phase 6: printer-fill glyph) ----------------
# The display-state machine lives in src.status (DisplayState / tooltip_for).
# Since Phase 6 every display state renders through render_printer_icon:
# ACTIVE_PRINT is a status-colored printer frame filled to mc_percent; the four
# non-active states all render the neutral grey "logged-out" frame with no fill
# (ICON-02). The at-a-glance difference between the non-active states lives in
# the tooltip wording (delegated to status.tooltip_for), which is unchanged.


def render_for_display_state(display_state: "DisplayState", state: PrintState) -> Image.Image:
    """Render the printer-fill glyph for one of the five display states.

    ACTIVE_PRINT       -> printer glyph in the status color (printing/paused by
                          gcode_state) with a neutral fill scaled to mc_percent.
    NO_ACTIVE_PRINT    -> neutral grey frame, NO fill.
    PRINTER_OFFLINE    -> neutral grey frame, NO fill.
    CLOUD_DISCONNECTED -> neutral grey frame, NO fill.
    TOKEN_EXPIRED      -> neutral grey frame, NO fill.

    The four non-active states all show the same neutral grey "logged-out" frame
    (ICON-02); the at-a-glance distinction between them lives in the tooltip
    wording, which is unchanged. Reads only gcode_state / mc_percent; no secret
    is referenced and nothing logs.
    """
    from src.status import DisplayState  # local import: avoids render<->status cycle

    if display_state is DisplayState.ACTIVE_PRINT:
        status = status_from_gcode_state(state.gcode_state)
        return render_printer_icon(state.mc_percent, status)
    # All non-active states (and any unexpected value) -> neutral grey frame,
    # no fill. Kept total and non-raising.
    return render_printer_icon(0, "neutral", logged_out=True)


def tooltip_for_display_state(display_state: "DisplayState", state: PrintState) -> str:
    """Exact locked Dutch tooltip for a display state (delegates to status.tooltip_for).

    Thin render-side entry point so the tray has a single call for both the glyph
    and its label. The locked strings live in status.py -- never duplicated here.
    """
    from src import status  # local import: single source of truth + no import cycle

    return status.tooltip_for(display_state, state)


# Imported for type/grep visibility; the cycle-safe runtime imports are local
# inside the functions above (status.py imports is_active_print from this module).
# Guarded so render stays importable regardless of import order: when status is
# imported first it is still mid-initialization here (DisplayState not yet bound),
# which must not hard-fail -- the real uses are the local imports above.
try:  # pragma: no cover - import-order guard
    from src.status import DisplayState  # noqa: E402,F401
except ImportError:  # status still initializing (status -> render -> status cycle)
    pass
