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
        self.notifications = []

    def notify(self, message, title=None):
        self.notifications.append((message, title))

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


def _paused(percent, remaining):
    """A paused-print PrintState (PAUSE) with the given percent/remaining.

    PAUSE is also an active print (is_active_print includes RUNNING + PAUSE) so
    it derives to DisplayState.ACTIVE_PRINT -- the same display as RUNNING. Only
    the frame COLOR differs (blue printing vs amber paused)."""
    return PrintState(
        gcode_state="PAUSE", mc_percent=percent, mc_remaining_time=remaining
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


def test_running_to_pause_triggers_repaint_same_percent_same_minute():
    """REGRESSION (paused-icon-no-recolor): a RUNNING->PAUSE transition with the
    SAME percent AND the SAME shown minute MUST repaint -- the frame color goes
    blue->amber, so the gcode-derived status is part of the debounce key. Without
    it the recolor was debounced away and the icon stayed blue while paused."""
    icon = FakeIcon()
    state = _active(42, 83)  # RUNNING, "1:23"
    ctrl = _connected(icon, state)

    ctrl.on_state_change()
    ctrl.pump_once()  # paint #1: printing (blue)
    assert icon.icon_set_count == 1

    state.gcode_state = "PAUSE"  # same percent (42), same minute (83 -> "1:23")
    ctrl.on_state_change()
    ctrl.pump_once()  # MUST repaint -> paused (amber)

    assert icon.icon_set_count == 2
    # Still an active print, so the tooltip body is unchanged (status drives the
    # icon COLOR, not the tooltip wording).
    assert icon.title == "42% — nog 1u 23m"


def test_running_pause_keys_differ():
    """The debounce key itself must differ between RUNNING and PAUSE at identical
    percent/remaining (locks the color-determining input into the key)."""
    running = _connected(FakeIcon(), _active(42, 83))
    paused = _connected(FakeIcon(), _paused(42, 83))

    assert running._key() != paused._key()


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


# --- Task 3 (this plan): connection/token status marshalling ----------------
#
# Connection/token status is a PARALLEL marshalled signal alongside PrintState.
# These tests prove (1) it is enqueue-only off the UI thread (no icon mutation),
# (2) a connection transition repaints exactly once with the correct locked
# tooltip, (3) the token-expired state surfaces its locked tooltip, (4) the
# freshness-driven offline state is reachable via an injected clock (no real
# waits), and (5) an unchanged derived display debounces to a single repaint.


def test_set_connection_status_does_not_touch_icon():
    """set_connection_status from a non-UI thread enqueues only -- it must NOT
    mutate the icon at all before pump_once (icon stays None, count 0)."""
    icon = FakeIcon()
    state = _active(42, 83)
    ctrl = tray.TrayController(icon, state)

    t = threading.Thread(
        target=ctrl.set_connection_status, args=(ConnectionStatus.CONNECTED,)
    )
    t.start()
    t.join()

    # Nothing painted before the UI thread pumps.
    assert icon.icon is None
    assert icon.title is None
    assert icon.icon_set_count == 0


def test_connection_transition_repaints_once():
    """CONNECTED + active paints; flipping to DISCONNECTED enqueues and pump_once
    repaints exactly once with the locked 'Verbinden…' tooltip."""
    icon = FakeIcon()
    state = _active(42, 83)
    ctrl = _connected(icon, state)

    ctrl.on_state_change()
    ctrl.pump_once()  # paint #1: active print
    assert icon.icon_set_count == 1
    assert icon.title == "42% — nog 1u 23m"

    ctrl.set_connection_status(ConnectionStatus.DISCONNECTED)  # enqueue only
    ctrl.pump_once()  # exactly one more repaint

    assert icon.icon_set_count == 2
    assert icon.title == "Verbinden…"


def test_token_expired_status_shows_relogin_tooltip():
    """A TOKEN_EXPIRED connection status surfaces the locked re-login tooltip."""
    icon = FakeIcon()
    state = _active(42, 83)
    ctrl = _connected(icon, state)

    ctrl.set_connection_status(ConnectionStatus.TOKEN_EXPIRED)
    ctrl.pump_once()

    assert icon.title == "Opnieuw inloggen vereist"
    assert icon.icon_set_count == 1


def test_freshness_offline_via_injected_clock():
    """CONNECTED + idle: within the freshness window the derived display is
    NO_ACTIVE_PRINT ('Geen actieve print'); once the injected clock advances past
    FRESHNESS_TIMEOUT_SECONDS it becomes PRINTER_OFFLINE ('Printer offline') --
    proven with no real waits."""
    icon = FakeIcon()
    clock = _FakeClock()
    state = PrintState(
        gcode_state="IDLE",
        mc_percent=0,
        mc_remaining_time=0,
        last_update_monotonic=clock.value,
    )
    ctrl = _connected(icon, state, now=clock)

    # Within the freshness window: no active print, not offline.
    ctrl.on_state_change()
    ctrl.pump_once()
    assert icon.title == "Geen actieve print"
    assert (
        status.derive_display_state(state, ConnectionStatus.CONNECTED, clock())
        is DisplayState.NO_ACTIVE_PRINT
    )

    # Advance past the timeout: same idle state now derives offline.
    clock.advance(FRESHNESS_TIMEOUT_SECONDS + 1)
    ctrl.on_state_change()
    ctrl.pump_once()
    assert icon.title == "Printer offline"
    assert (
        status.derive_display_state(state, ConnectionStatus.CONNECTED, clock())
        is DisplayState.PRINTER_OFFLINE
    )


def test_unchanged_display_debounces():
    """Two set_connection_status(DISCONNECTED) in a row collapse to a single
    repaint -- the derived display (CLOUD_DISCONNECTED) is unchanged."""
    icon = FakeIcon()
    state = _active(42, 83)
    ctrl = _connected(icon, state)

    ctrl.set_connection_status(ConnectionStatus.DISCONNECTED)
    ctrl.pump_once()  # paint #1: CLOUD_DISCONNECTED
    ctrl.set_connection_status(ConnectionStatus.DISCONNECTED)  # identical display
    ctrl.pump_once()  # debounced -> no second paint

    assert icon.icon_set_count == 1
    assert icon.title == "Verbinden…"


# --- Plan 07-03: hidden default left-click flyout-toggle item ----------------


def test_make_open_item_is_hidden_default():
    """make_open_item returns a pystray MenuItem flagged default=True (LEFT-click
    invokes it) and visible=False (kept out of the right-click menu)."""
    item = tray.make_open_item(lambda: None)

    assert item.default is True
    assert item.visible is False


def test_make_open_item_callback_invokes_toggle_once():
    """Invoking the item's callback (as pystray does, with icon+item) calls the
    injected zero-arg toggle exactly once."""
    calls = []
    item = tray.make_open_item(lambda: calls.append(True))

    # pystray invokes a selected menu item as item(icon); it then forwards to the
    # wrapped action as action(icon, item) internally.
    item(object())

    assert calls == [True]


# --- reset_to_logged_out (logout drops the tray to the neutral glyph) -------


def test_reset_to_logged_out_clears_state_and_shows_neutral():
    """On logout the tray must stop showing the last print. reset_to_logged_out
    clears the retained PrintState and routes DISCONNECTED through the marshalled
    seam, so after a pump the display is the neutral CLOUD_DISCONNECTED glyph
    (its 'Verbinden…' tooltip) instead of a stale ACTIVE_PRINT."""
    icon = FakeIcon()
    state = _active(37, 92)
    ctrl = _connected(icon, state)
    ctrl.reassert()  # first paint: ACTIVE_PRINT
    assert icon.title == "37% — nog 1u 32m"

    ctrl.reset_to_logged_out()
    # state cleared back to defaults (no longer an active print)
    assert state.gcode_state == "unknown"
    assert state.mc_percent == 0
    assert ctrl._status is ConnectionStatus.DISCONNECTED
    # reset enqueues a repaint; the UI pump applies it on its thread
    ctrl.pump_once()
    assert icon.title == status.TOOLTIP_CLOUD_DISCONNECTED


# --- Phase 13-02: update-balloon (enqueue-only, UI-thread drain) -------------
#
# The update balloon rides its OWN queue (separate from the render-request queue)
# so it is never collapsed into the render debounce key. signal_update_available
# is enqueue-only from the daemon thread; only pump_once calls icon.notify on the
# UI thread. CRITICAL regression guard: a balloon enqueued on a tick with NO
# pending render request (the COMMON case) must STILL fire exactly one notify --
# it must be drained BEFORE pump_once's `if latest is None: return` render
# early-out.


def test_signal_update_available_does_not_notify_off_thread():
    """signal_update_available from a non-UI thread enqueues only -- it must NOT
    call icon.notify before pump_once."""
    icon = FakeIcon()
    ctrl = tray.TrayController(icon, _active(42, 83))

    t = threading.Thread(
        target=ctrl.signal_update_available, args=("2.2.0", "https://x/rel")
    )
    t.start()
    t.join()

    assert icon.notifications == []


def test_pump_once_drains_balloon_and_notifies_once():
    """pump_once drains a queued balloon and calls icon.notify exactly once on the
    UI thread, with a message mentioning the new version."""
    icon = FakeIcon()
    ctrl = _connected(icon, _active(42, 83))

    ctrl.signal_update_available("2.2.0", "https://x/rel")
    ctrl.pump_once()

    assert len(icon.notifications) == 1
    message, _title = icon.notifications[0]
    assert "2.2.0" in message


def test_balloon_with_no_pending_render_still_fires_once():
    """REGRESSION (early-return-swallow): a balloon enqueued while the render queue
    is EMPTY (the common case -- print state unchanged) must STILL fire exactly one
    notify. This guards against the balloon being placed below pump_once's
    `if latest is None: return` render early-out."""
    icon = FakeIcon()
    ctrl = _connected(icon, _active(42, 83))

    # No on_state_change() -> render queue is empty. Only a balloon is queued.
    ctrl.signal_update_available("2.3.0", "https://x/rel2")
    ctrl.pump_once()

    assert len(icon.notifications) == 1
    assert "2.3.0" in icon.notifications[0][0]
    # And the render queue being empty means no repaint happened.
    assert icon.icon_set_count == 0


def test_balloon_and_render_in_same_pump_both_apply():
    """A queued balloon AND a queued render request in the same pump cycle both
    apply: the render repaints AND the balloon fires exactly one notify (the
    balloon is not swallowed by the render debounce)."""
    icon = FakeIcon()
    ctrl = _connected(icon, _active(42, 83))

    ctrl.on_state_change()  # render request queued
    ctrl.signal_update_available("2.2.0", "https://x/rel")  # balloon queued
    ctrl.pump_once()

    assert icon.icon_set_count == 1  # render applied
    assert len(icon.notifications) == 1  # balloon fired
    assert "2.2.0" in icon.notifications[0][0]


def test_no_balloon_queued_pump_never_notifies():
    """With no balloon queued, pump_once never calls icon.notify."""
    icon = FakeIcon()
    ctrl = _connected(icon, _active(42, 83))

    ctrl.on_state_change()
    ctrl.pump_once()

    assert icon.notifications == []
