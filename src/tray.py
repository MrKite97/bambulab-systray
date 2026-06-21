"""TrayController: the thread-safe bridge between the network thread (which
writes :class:`~src.state.PrintState`) and the pystray UI thread (which owns
the icon).

LOCKED RULES (02-CONTEXT.md "Behavior & Threading" + "Redraw policy"):

1. THREADING -- the pystray icon is mutated ONLY on the UI thread. The network
   (paho) callback thread merely *enqueues* a render request via a thread-safe
   ``queue.Queue``; it NEVER calls ``icon.icon`` / ``icon.title``. This avoids
   cross-thread mutation of the STA Shell_NotifyIcon handle (T-02-03).

2. DEBOUNCE -- redraws happen only when the DISPLAYED value changes. The key is
   ``(render.icon_text(state), state.mc_percent)``. ``icon_text`` already
   collapses remaining time to the shown minute / "10u" / None, so keying on it
   is exactly "the displayed minute changed". Sub-minute MQTT deltas that leave
   both the shown minute and the integer percent unchanged are coalesced to zero
   repaints (T-02-04: no CPU churn on every delta).

This module forwards only ``render.*`` output, which reads non-secret PrintState
fields; it references no token and logs nothing (T-02-05).
"""

import queue

from src import render


class TrayController:
    """Bridges PrintState (written by the network thread) to a pystray Icon
    (owned by the UI thread). Marshals via a thread-safe queue and debounces
    redraws so only displayed-value changes repaint.

    The pystray ``Icon`` is injected (a fake in tests) so this whole layer is
    unit-tested without launching a real tray.
    """

    def __init__(self, icon, state):
        self._icon = icon  # pystray Icon (or a FakeIcon in tests)
        self._state = state  # shared PrintState (single source of truth)
        self._requests = queue.Queue()  # network -> UI render requests
        self._last_key = object()  # sentinel: nothing applied yet

    @staticmethod
    def _key(state):
        """The displayed value: shown icon text (minute / '10u' / None) plus the
        integer percent. Equal keys mean an identical on-screen result."""
        return (render.icon_text(state), state.mc_percent)

    def on_state_change(self):
        """Call from the NETWORK thread. Enqueues a render request only; it
        NEVER touches the icon (no ``.icon`` / ``.title`` assignment here)."""
        self._requests.put(self._key(self._state))

    def pump_once(self):
        """Call from the UI thread. Drains all pending requests (coalescing to
        the latest) and repaints AT MOST once -- only if the displayed value
        changed versus the last applied key."""
        latest = None
        try:
            while True:
                latest = self._requests.get_nowait()
        except queue.Empty:
            pass
        if latest is None:  # queue was empty -- nothing to do
            return
        if latest == self._last_key:  # displayed value unchanged -- debounce
            return
        self._last_key = latest
        self._apply()

    def _apply(self):
        """Repaint from the CURRENT shared PrintState. The key only gates
        *whether* to repaint; the state object is the single source of truth for
        *what* to draw, so we read it fresh here (image first, then tooltip)."""
        self._icon.icon = render.render_for_state(self._state)
        self._icon.title = render.tooltip_text(self._state)
