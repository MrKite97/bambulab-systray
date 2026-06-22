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


# Printer-frame geometry on the 64x64 canvas. The build area is the inner region
# the printed object fills; the fill runs flush to the plate (no bottom gap).
_BUILD_LEFT = 14
_BUILD_RIGHT = 50
_BUILD_TOP = 18
_PLATE_Y = 50
_FRAME_STROKE = 4


def render_printer_icon(
    pct: int,
    status: str,
    *,
    theme: str | None = None,
    logged_out: bool = False,
) -> Image.Image:
    """Render the printer-fill tray glyph as a 64x64 RGBA image.

    The printer frame (body outline + build plate) is drawn in
    ``status_to_color(status, theme)``. The build area fills bottom-to-top with
    NEUTRAL material grey (never the status color) to a height of
    ``pct/100 x build-area height``, clipped to the build area and flush to the
    plate. ``pct`` is clamped to 0..100. The fill is drawn ONLY when there is an
    active status (not ``logged_out`` and ``status != "neutral"``) and the fill
    height is non-zero; logged-out / neutral renders a grey frame with no fill.
    """
    theme = theme or detect_windows_theme()
    frame_color = status_to_color(status, theme)
    neutral_fill = status_to_color("neutral", theme)

    img = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Fill the build area FIRST (neutral grey), then stroke the frame on top so
    # the colored contour always wins where they overlap (keeps pct=1 legible).
    show_fill = not logged_out and status != "neutral"
    if show_fill:
        build_h = _PLATE_Y - _BUILD_TOP
        fill_h = round(max(0, min(100, pct)) / 100 * build_h)
        if fill_h > 0:
            fill_top = _PLATE_Y - fill_h
            d.rectangle(
                [_BUILD_LEFT, fill_top, _BUILD_RIGHT, _PLATE_Y],
                fill=neutral_fill,
            )

    # Printer body outline (the build-area rectangle) + the build plate line,
    # both in the frame/status color at full stroke.
    d.rectangle(
        [_BUILD_LEFT, _BUILD_TOP, _BUILD_RIGHT, _PLATE_Y],
        outline=frame_color,
        width=_FRAME_STROKE,
    )
    # Emphasize the build plate (bottom) as a solid base line.
    d.line(
        [(_BUILD_LEFT, _PLATE_Y), (_BUILD_RIGHT, _PLATE_Y)],
        fill=frame_color,
        width=_FRAME_STROKE,
    )
    return img


def render_for_state(state: PrintState) -> Image.Image:
    """Icon for a PrintState: time digits when active, neutral glyph when idle."""
    return render_icon(icon_text(state))


# --- 5-state display rendering (Phase 3) -----------------------------------
# The display-state machine lives in src.status (DisplayState / tooltip_for).
# This layer turns each of the five states into a distinct, 16x16-survivable
# 64x64 glyph and a tooltip. ACTIVE_PRINT / NO_ACTIVE_PRINT reuse the existing
# active/idle render path unchanged; the three connection/token states each get
# a legible centered glyph drawn with the bundled font. The tooltip carries the
# exact locked Dutch wording (delegated to status.tooltip_for) -- this glyph
# only needs to be visually distinct at a glance.

_GLYPH_FONT_SIZE = 48


def _render_centered_glyph(text: str, fill: tuple[int, int, int, int]) -> Image.Image:
    """Render a single centered glyph string on a 64x64 RGBA canvas (no digits path)."""
    img = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    font = _load_font(_GLYPH_FONT_SIZE)
    bbox = d.textbbox((0, 0), text, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text(
        ((ICON_SIZE - w) / 2 - bbox[0], (ICON_SIZE - h) / 2 - bbox[1]),
        text,
        font=font,
        fill=fill,
    )
    return img


def render_for_display_state(display_state: "DisplayState", state: PrintState) -> Image.Image:
    """Render a distinct 64x64 RGBA glyph for one of the five display states.

    ACTIVE_PRINT       -> existing time-digit render (render_for_state).
    NO_ACTIVE_PRINT    -> existing neutral grey dot (render_icon(None)).
    PRINTER_OFFLINE    -> a dimmed hollow ring (distinct from the idle dot).
    CLOUD_DISCONNECTED -> a centered ellipsis (connecting).
    TOKEN_EXPIRED      -> a centered '!' (sign-in required).

    Reads only DisplayState + PrintState fields the existing paths already use;
    no secret is referenced and nothing logs.
    """
    from src.status import DisplayState  # local import: avoids render<->status cycle

    if display_state is DisplayState.ACTIVE_PRINT:
        return render_for_state(state)
    if display_state is DisplayState.NO_ACTIVE_PRINT:
        return render_icon(None)
    if display_state is DisplayState.PRINTER_OFFLINE:
        # Dimmed hollow ring: same footprint band as the idle dot but an outline
        # (not a fill) in a dimmer grey -> a different opaque-pixel set.
        img = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((18, 18, 46, 46), outline=(110, 110, 110, 255), width=4)
        return img
    if display_state is DisplayState.CLOUD_DISCONNECTED:
        return _render_centered_glyph("…", (180, 180, 180, 255))
    if display_state is DisplayState.TOKEN_EXPIRED:
        return _render_centered_glyph("!", (235, 180, 40, 255))
    # Unreachable for the 5-member enum, but keep total and non-raising.
    return render_icon(None)


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
