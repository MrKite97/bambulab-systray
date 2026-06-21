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
