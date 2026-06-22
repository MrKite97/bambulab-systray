"""The single pure 5-state display-derivation seam for the tray.

This module turns a :class:`~src.state.PrintState`, a connection/token signal,
and an injected monotonic clock reading into exactly one of five locked display
states, and maps each state to its exact Dutch tooltip. It is the contract every
later Phase 3 plan (render / tray / app) renders from, so the state machine
lives in ONE pure, fully unit-testable place -- never scattered across modules.

Design constraints (kept deliberately narrow):
- No network, no threads, no pystray, no secrets. This layer reads ONLY
  ``gcode_state`` / ``mc_percent`` / ``mc_remaining_time`` /
  ``last_update_monotonic`` from PrintState. PrintState carries no token, so no
  secret can reach a tooltip, and nothing here logs.
- The freshness clock is injected (``now_monotonic`` is a plain float, e.g. a
  ``time.monotonic()`` reading passed in) so tests run with no real waits.
- ``gcode_state`` is untrusted broker input: classification is ``.upper()``-
  guarded set membership, so any unknown/garbage string falls through to a safe
  non-active branch and can never raise.

The five locked states (03-CONTEXT.md):
  ACTIVE_PRINT, NO_ACTIVE_PRINT, PRINTER_OFFLINE, CLOUD_DISCONNECTED, TOKEN_EXPIRED.

Idle is strictly separate from offline:
  idle    = gcode_state in {IDLE, FINISH, FAILED} with a fresh report,
  offline = connected but NO fresh report within FRESHNESS_TIMEOUT_SECONDS.
They are never conflated.
"""

from enum import Enum, auto

from src.render import is_active_print  # REUSE active classification; no duplicated RUNNING/PAUSE set
from src.state import PrintState

# Tunable after Phase 1 live validation: if connected but no report has arrived
# within this many seconds (after pushall), the printer is treated as offline.
FRESHNESS_TIMEOUT_SECONDS = 30  # tunable after Phase 1 live validation

# Idle gcode_states (case-insensitive). Distinct from "offline" (no fresh data).
IDLE_STATES = {"IDLE", "FINISH", "FAILED"}


class DisplayState(Enum):
    """The five mutually-exclusive states the tray can render."""

    ACTIVE_PRINT = auto()
    NO_ACTIVE_PRINT = auto()
    PRINTER_OFFLINE = auto()
    CLOUD_DISCONNECTED = auto()
    TOKEN_EXPIRED = auto()


class ConnectionStatus(Enum):
    """The network-layer signal feeding the derivation (set by app/MQTT path)."""

    CONNECTED = auto()
    DISCONNECTED = auto()
    TOKEN_EXPIRED = auto()


# Locked Dutch tooltips for the four non-active states (03-CONTEXT.md).
# ACTIVE_PRINT's body is single-sourced from render.tooltip_text (see tooltip_for).
TOOLTIP_NO_ACTIVE_PRINT = "Geen actieve print"
TOOLTIP_PRINTER_OFFLINE = "Printer offline"
TOOLTIP_CLOUD_DISCONNECTED = "Verbinden…"  # single-char ellipsis U+2026
TOOLTIP_TOKEN_EXPIRED = "Opnieuw inloggen vereist"


def _is_fresh(state: PrintState, now_monotonic: float) -> bool:
    """True when a report arrived within FRESHNESS_TIMEOUT_SECONDS of ``now``.

    ``last_update_monotonic == 0.0`` means no report has ever been received and
    is never fresh (-> offline, not idle).
    """
    last = state.last_update_monotonic
    if last <= 0.0:
        return False
    return (now_monotonic - last) <= FRESHNESS_TIMEOUT_SECONDS


def derive_display_state(
    state: PrintState, connection: ConnectionStatus, now_monotonic: float
) -> DisplayState:
    """Derive exactly one DisplayState from state + connection + injected clock.

    Precedence (most-severe-first):
      1. connection TOKEN_EXPIRED  -> TOKEN_EXPIRED
      2. connection DISCONNECTED   -> CLOUD_DISCONNECTED
      3. active gcode_state        -> ACTIVE_PRINT
      4. idle gcode_state + fresh  -> NO_ACTIVE_PRINT
      5. otherwise (no fresh data) -> PRINTER_OFFLINE

    Active prints stream reports continuously, so an ACTIVE gcode_state is
    reported as ACTIVE_PRINT even when slightly stale -- active printing keeps
    showing remaining time; offline only applies to the idle/no-data branch
    (staleness while active is the disconnect path, surfaced via ConnectionStatus,
    not the offline branch here).
    """
    if connection is ConnectionStatus.TOKEN_EXPIRED:
        return DisplayState.TOKEN_EXPIRED
    if connection is ConnectionStatus.DISCONNECTED:
        return DisplayState.CLOUD_DISCONNECTED

    # connection is CONNECTED from here on.
    if is_active_print(state.gcode_state):
        return DisplayState.ACTIVE_PRINT

    # Idle/finished/failed (or unknown) with a fresh report = no active print.
    # No fresh report at all = the printer/broker has gone quiet = offline.
    if _is_fresh(state, now_monotonic):
        return DisplayState.NO_ACTIVE_PRINT
    return DisplayState.PRINTER_OFFLINE


def tooltip_for(display_state: DisplayState, state: PrintState) -> str:
    """Map a DisplayState to its exact locked Dutch tooltip.

    ACTIVE_PRINT delegates to render.tooltip_text so the active body
    ("NN% — nog Xu Ym") stays single-sourced. Every non-active state returns a
    constant with no '%' and no 'nog ' -- so stale numbers from a previous active
    print can never leak into an idle/offline/disconnected/expired tooltip.
    """
    if display_state is DisplayState.ACTIVE_PRINT:
        from src.render import tooltip_text  # local import: active body single-sourced

        return tooltip_text(state)
    if display_state is DisplayState.NO_ACTIVE_PRINT:
        return TOOLTIP_NO_ACTIVE_PRINT
    if display_state is DisplayState.PRINTER_OFFLINE:
        return TOOLTIP_PRINTER_OFFLINE
    if display_state is DisplayState.CLOUD_DISCONNECTED:
        return TOOLTIP_CLOUD_DISCONNECTED
    if display_state is DisplayState.TOKEN_EXPIRED:
        return TOOLTIP_TOKEN_EXPIRED
    # Unreachable for the 5-member enum, but keep total and non-raising.
    return TOOLTIP_NO_ACTIVE_PRINT
