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

import os

from PIL import Image, ImageDraw, ImageFont

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
_FONT_PATH = os.path.join(os.path.dirname(__file__), "..", "assets", "DejaVuSans.ttf")


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
