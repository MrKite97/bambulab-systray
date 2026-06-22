"""Tests for src.tray.TrayController -- the thread-safe, debounced bridge
between the network thread (which writes PrintState) and the pystray UI thread
(which owns the icon).

A FakeIcon stands in for a real pystray Icon: it has ``icon``/``title``
attributes plus counters, and NEVER launches a real tray. The load-bearing
assertions are (1) ``on_state_change`` (producer side) mutates the icon ZERO
times -- all repaints happen inside ``pump_once``/``reassert`` on the UI thread
-- and (2) the debounce key (icon_text, mc_percent) coalesces sub-minute MQTT
deltas to a single repaint. No network, no real pystray, no secrets.
"""

import threading

from src import status, tray
from src.state import PrintState
from src.status import ConnectionStatus, DisplayState, FRESHNESS_TIMEOUT_SECONDS


class FakeIcon:
    """Stand-in for a pystray Icon. Records icon/title assignments and counts
    how many times the bitmap is (re)assigned to a non-None value."""

    def __init__(self):
        self.icon = None
        self.title = None
        self.visible = False
        self.icon_set_count = 0
        self.title_set_count = 0

    def __setattr__(self, k, v):
        object.__setattr__(self, k, v)
        if k == "icon" and v is not None:
            object.__setattr__(self, "icon_set_count", self.icon_set_count + 1)
        if k == "title" and v is not None:
            object.__setattr__(self, "title_set_count", self.title_set_count + 1)


def _active(percent, remaining):
    """An active-print PrintState (RUNNING) with the given percent/remaining."""
    return PrintState(
        gcode_state="RUNNING", mc_percent=percent, mc_remaining_time=remaining
    )


class _FakeClock:
    """A tiny advanceable monotonic clock (no real waits). Tests read the current
    value via __call__ (so it drops in as TrayController(now=...)) and move time
    forward with advance()."""

    def __init__(self, start=1000.0):
        self.value = start

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def _connected(icon, state, now=None):
    """A TrayController already marked CONNECTED (so the active/idle print-display
    path is reachable). Connection status is set directly -- not via the queue --
    so the helper does not itself enqueue a paint."""
    if now is None:
        ctrl = tray.TrayController(icon, state)
    else:
        ctrl = tray.TrayController(icon, state, now=now)
    ctrl._status = ConnectionStatus.CONNECTED
    return ctrl


# --- Task 1: marshalling + debounce ----------------------------------------


def test_on_state_change_does_not_touch_icon():
    """Producer side enqueues only -- it must NOT mutate the icon at all,
    even when driven from a non-UI thread."""
    icon = FakeIcon()
    state = _active(42, 83)
    ctrl = tray.TrayController(icon, state)

    t = threading.Thread(target=ctrl.on_state_change)
    t.start()
    t.join()

    # Nothing painted before the UI thread pumps.
    assert icon.icon is None
    assert icon.title is None
    assert icon.icon_set_count == 0


def test_pump_once_applies_icon_and_title():
    """pump_once on the UI thread drains the queue and paints exactly once."""
    icon = FakeIcon()
    state = _active(42, 83)
    ctrl = _connected(icon, state)

    ctrl.on_state_change()
    ctrl.pump_once()

    assert icon.icon_set_count == 1
    assert icon.title == "42% — nog 1u 23m"


def test_pump_once_empty_queue_is_noop():
    """pump_once with nothing enqueued paints nothing."""
    icon = FakeIcon()
    ctrl = tray.TrayController(icon, _active(42, 83))

    ctrl.pump_once()

    assert icon.icon_set_count == 0
    assert icon.icon is None


def test_debounce_same_value_paints_once():
    """Two on_state_change calls with the SAME displayed value -> exactly one
    repaint after pumping."""
    icon = FakeIcon()
    state = _active(42, 83)
    ctrl = _connected(icon, state)

    ctrl.on_state_change()
    ctrl.pump_once()
    ctrl.on_state_change()  # identical key
    ctrl.pump_once()

    assert icon.icon_set_count == 1


def test_percent_change_triggers_repaint():
    """A change in mc_percent only (same shown minute) DOES repaint -- percent
    is part of the debounce key."""
    icon = FakeIcon()
    state = _active(41, 83)
    ctrl = _connected(icon, state)

    ctrl.on_state_change()
    ctrl.pump_once()
    state.mc_percent = 42  # same minute (83 -> "1:23"), percent changed
    ctrl.on_state_change()
    ctrl.pump_once()

    assert icon.icon_set_count == 2


def test_minute_change_triggers_repaint():
    """A change that crosses the shown minute DOES repaint."""
    icon = FakeIcon()
    state = _active(42, 84)  # "1:24"
    ctrl = _connected(icon, state)

    ctrl.on_state_change()
    ctrl.pump_once()
    state.mc_remaining_time = 83  # "1:23" -- shown minute changed
    ctrl.on_state_change()
    ctrl.pump_once()

    assert icon.icon_set_count == 2


def test_sub_minute_change_with_same_display_does_not_repaint():
    """A change leaving icon_text AND mc_percent identical does NOT repaint
    (icon_text only collapses to whole minutes, but here both are unchanged)."""
    icon = FakeIcon()
    state = _active(42, 83)
    ctrl = _connected(icon, state)

    ctrl.on_state_change()
    ctrl.pump_once()
    # Same percent (42) and same remaining minute (83) -> identical key.
    ctrl.on_state_change()
    ctrl.pump_once()

    assert icon.icon_set_count == 1


def test_idle_state_paints_neutral():
    """Applying an idle state (connected + fresh report) sets the tooltip to
    'Geen actieve print'."""
    icon = FakeIcon()
    clock = _FakeClock()
    # Fresh report (last_update == now) so idle derives NO_ACTIVE_PRINT, not
    # PRINTER_OFFLINE.
    state = PrintState(
        gcode_state="IDLE",
        mc_percent=0,
        mc_remaining_time=0,
        last_update_monotonic=clock.value,
    )
    ctrl = _connected(icon, state, now=clock)

    ctrl.on_state_change()
    ctrl.pump_once()

    assert icon.icon_set_count == 1
    assert icon.title == "Geen actieve print"


def test_pump_coalesces_multiple_enqueued_requests():
    """Several enqueued requests with differing keys collapse to one repaint
    reflecting the LATEST request when pump_once drains them together."""
    icon = FakeIcon()
    state = _active(40, 90)
    ctrl = _connected(icon, state)

    ctrl.on_state_change()  # 40 / 90
    state.mc_percent = 41
    ctrl.on_state_change()  # 41 / 90
    state.mc_percent = 42
    ctrl.on_state_change()  # 42 / 90
    ctrl.pump_once()  # drains all three -> single paint

    assert icon.icon_set_count == 1
    assert icon.title == "42% — nog 1u 30m"


# --- Task 2: reassert hook + build_setup UI-thread pump --------------------


def test_reassert_paints_even_when_key_unchanged():
    """reassert() forces a repaint regardless of the debounce key -- used after
    the tray icon is recreated (e.g. TaskbarCreated / Explorer restart)."""
    icon = FakeIcon()
    state = _active(42, 83)
    ctrl = _connected(icon, state)

    ctrl.on_state_change()
    ctrl.pump_once()  # paint #1, baseline now set
    ctrl.reassert()  # same key, but reassert always repaints

    assert icon.icon_set_count == 2
    assert icon.title == "42% — nog 1u 23m"


def test_reassert_resets_debounce_baseline():
    """After reassert(), a following on_state_change with the SAME value does
    NOT double-paint -- reassert resets the baseline to the current state."""
    icon = FakeIcon()
    state = _active(42, 83)
    ctrl = _connected(icon, state)

    ctrl.reassert()  # paint #1, baseline = current (42, "1:23")
    ctrl.on_state_change()  # identical value
    ctrl.pump_once()  # must be a no-op

    assert icon.icon_set_count == 1


def test_reassert_from_cold_paints_once():
    """reassert() works as the FIRST paint at startup (no prior pump)."""
    icon = FakeIcon()
    ctrl = _connected(icon, _active(42, 83))

    ctrl.reassert()

    assert icon.icon_set_count == 1
    assert icon.title == "42% — nog 1u 23m"


def test_build_setup_makes_visible_and_paints():
    """build_setup() returns a UI-thread callback that makes the icon visible
    and performs an initial reassert (so the icon shows immediately on launch)."""
    icon = FakeIcon()
    state = _active(42, 83)
    ctrl = _connected(icon, state)

    setup = ctrl.build_setup()
    setup(icon)

    assert icon.visible is True
    assert icon.icon_set_count == 1
    assert icon.title == "42% — nog 1u 23m"


def test_build_setup_no_double_paint_on_first_real_change():
    """After build_setup()'s initial paint, the first on_state_change with the
    same displayed value does NOT repaint (baseline was reset by reassert)."""
    icon = FakeIcon()
    state = _active(42, 83)
    ctrl = _connected(icon, state)

    ctrl.build_setup()(icon)  # initial paint, baseline set
    ctrl.on_state_change()  # same value
    ctrl.pump_once()

    assert icon.icon_set_count == 1


def test_build_setup_tolerates_icon_without_visible_attr():
    """build_setup() must not fail on an icon lacking a ``visible`` attribute;
    it still performs the initial paint."""

    class MinimalIcon:
        def __init__(self):
            self.icon = None
            self.title = None

    icon = MinimalIcon()
    ctrl = _connected(icon, _active(42, 83))

    ctrl.build_setup()(icon)  # must not raise

    assert icon.title == "42% — nog 1u 23m"
