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

    # --- TaskbarCreated / startup re-registration --------------------------
    #
    # TaskbarCreated handling decision: pystray's Windows backend listens for
    # the broadcast ``TaskbarCreated`` message and re-adds its tray icon on its
    # OWN UI thread after an Explorer restart -- but it re-adds a DEFAULT icon,
    # losing our drawn bitmap/tooltip. We hook into that recreation by calling
    # ``reassert()`` from the UI thread, which repaints our content from the
    # current PrintState. We deliberately do NOT register an explicit win32
    # ``TaskbarCreated`` listener yet: the default expectation is that pystray
    # handles the re-add and we only need to repaint. If the Plan 03 human-verify
    # shows the icon does NOT reappear after an Explorer restart, add an explicit
    # listener then. (Real Explorer-restart survival is a Plan 03 human-verify
    # item.)

    def reassert(self):
        """Repaint unconditionally and reset the debounce baseline.

        Used (a) for the first paint at startup and (b) after the tray icon is
        recreated following a ``TaskbarCreated`` (Explorer restart). Unlike
        ``pump_once`` this ignores the debounce key, then re-baselines it to the
        current state so the NEXT identical ``on_state_change`` does not
        double-paint."""
        self._apply()
        self._last_key = self._key(self._state)

    def build_setup(self):
        """Return a pystray ``run(setup=...)`` callback. It runs ONCE on the UI
        thread when the icon becomes visible: it makes the icon visible and
        paints the first frame so the icon shows immediately on launch.

        pystray itself re-creates the icon on ``TaskbarCreated``; the run loop
        should call :meth:`reassert` after such a recreation to restore our
        drawn content."""

        def _setup(icon):
            if hasattr(icon, "visible"):
                icon.visible = True
            self.reassert()

        return _setup
