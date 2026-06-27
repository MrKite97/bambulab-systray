"""TrayController: the thread-safe bridge between the network thread (which
writes :class:`~src.state.PrintState`) and the pystray UI thread (which owns
the icon).

LOCKED RULES (02-CONTEXT.md "Behavior & Threading" + "Redraw policy"):

1. THREADING -- the pystray icon is mutated ONLY on the UI thread. The network
   (paho) callback thread merely *enqueues* a render request via a thread-safe
   ``queue.Queue``; it NEVER calls ``icon.icon`` / ``icon.title``. This avoids
   cross-thread mutation of the STA Shell_NotifyIcon handle (T-02-03).

2. DEBOUNCE -- redraws happen only when the DISPLAYED value changes. The key is
   the DERIVED :class:`~src.status.DisplayState` plus, only when ACTIVE_PRINT,
   the ``(icon_text, mc_percent)`` tuple. ``icon_text`` already collapses
   remaining time to the shown minute / "10u" / None, so keying on it is exactly
   "the displayed minute changed". An identical derived display with identical
   shown numbers coalesces to zero repaints; a connection/token transition that
   changes the derived display repaints exactly once (T-02-04: no CPU churn on
   every delta).

3. CONNECTION/TOKEN STATUS -- the network layer signals connection/token status
   via :meth:`set_connection_status`, which (like ``on_state_change``) only
   ENQUEUES a render request from the network thread; it NEVER mutates the icon.
   The UI pump derives the 5-state display from (PrintState + the latest
   connection status + an injectable monotonic clock) and paints through the same
   single marshalling seam as print updates (REL-02/REL-03/STAT-05, T-03-04).

4. UPDATE BALLOON (Phase 13, D-09) -- the daemon update-check thread signals an
   available update via :meth:`signal_update_available`, ANOTHER enqueue-only
   signal (its own queue) drained on the UI thread (same discipline as connection
   status -- the daemon NEVER calls ``icon.notify``). It is drained in
   :meth:`pump_once` BEFORE the render early-out, so a balloon fires even when no
   repaint is pending. This module logs nothing, so a notify hiccup is swallowed
   silently (bare ``pass``), never via a logger.

This module forwards only ``render.*`` output, which reads non-secret PrintState
fields and a ``ConnectionStatus`` enum; it references no token and logs nothing
(T-02-05 / T-03-05).
"""

import queue
import time

from src import render, status


def make_open_item(toggle):
    """Build the hidden default LEFT-click menu item that toggles the flyout.

    On Windows pystray, a ``MenuItem(..., default=True)`` is invoked by a LEFT
    click on the tray icon, and ``visible=False`` keeps it OUT of the right-click
    menu (so the existing Afsluiten / autostart / re-login items are unchanged).
    The label "Open" is therefore never shown.

    ``toggle`` is a zero-arg callable (the bound ``FlyoutWindow.toggle`` wired in
    app.py). The returned item's callback simply invokes it -- it does NOT mutate
    the icon off-thread, so the locked marshalling boundary stays intact. The
    ``pystray`` import is local so importing this module needs no GUI backend.
    """
    import pystray

    return pystray.MenuItem(
        "Open",
        lambda icon, item: toggle(),
        default=True,
        visible=False,
    )


class TrayController:
    """Bridges PrintState (written by the network thread) to a pystray Icon
    (owned by the UI thread). Marshals via a thread-safe queue and debounces
    redraws so only displayed-value changes repaint.

    The pystray ``Icon`` is injected (a fake in tests) so this whole layer is
    unit-tested without launching a real tray.
    """

    def __init__(self, icon, state, *, now=time.monotonic):
        self._icon = icon  # pystray Icon (or a FakeIcon in tests)
        self._state = state  # shared PrintState (single source of truth)
        self._requests = queue.Queue()  # network -> UI render requests
        # Update-balloon requests (version, url), its OWN queue so a balloon is
        # never collapsed into the render-request debounce key. Enqueued off-thread
        # by signal_update_available, drained by pump_once on the UI thread.
        self._balloons = queue.Queue()
        self._last_key = object()  # sentinel: nothing applied yet
        # Injectable monotonic clock (tests pass a fake so freshness needs no real
        # waits). Defaults to time.monotonic so existing callers keep working.
        self._now = now
        # Connection/token status, a parallel marshalled signal alongside
        # PrintState. Default DISCONNECTED so before the first connect the display
        # is CLOUD_DISCONNECTED ("Verbinden…"), never a false idle.
        self._status = status.ConnectionStatus.DISCONNECTED

    def _key(self):
        """The displayed value, keyed on the DERIVED display state.

        Non-active displays debounce purely on the :class:`DisplayState` (so a
        connection/token transition repaints exactly once and an unchanged
        derived display repaints zero times). ACTIVE_PRINT additionally keys on
        the gcode-derived status (printing vs paused -- the frame color), the
        shown icon text (minute / '10u') and integer percent, so the active
        color/minute/percent each drive repaints. The status is load-bearing:
        RUNNING and PAUSE both derive to ACTIVE_PRINT, so without it a
        printing<->paused transition (same shown minute, same integer percent)
        would key identically and the amber/blue recolor would be debounced away.
        Equal keys mean an identical on-screen result."""
        display = status.derive_display_state(self._state, self._status, self._now())
        if display is status.DisplayState.ACTIVE_PRINT:
            return (
                display,
                render.status_from_gcode_state(self._state.gcode_state),
                render.icon_text(self._state),
                self._state.mc_percent,
            )
        return (display, None, None, None)

    def reset_to_logged_out(self):
        """Drop the tray back to the neutral logged-out glyph (logout).

        Clears the retained PrintState so it no longer describes the last print,
        then routes a DISCONNECTED status through the SAME marshalled
        ``set_connection_status`` seam (enqueue only, never an off-thread icon
        write). The derived display becomes CLOUD_DISCONNECTED -- the neutral grey
        frame the app also shows at startup -- instead of a stale ACTIVE_PRINT.
        Safe to call from the bridge/worker thread (logout runs there)."""
        self._state.reset()
        self.set_connection_status(status.ConnectionStatus.DISCONNECTED)

    def set_connection_status(self, status_value):
        """Call from the NETWORK thread. Updates the stored connection/token
        status and ENQUEUES a render request via the same queue as
        ``on_state_change``. It NEVER touches the icon (no ``.icon`` / ``.title``
        assignment here) -- all icon mutation stays on the UI thread (T-03-04)."""
        self._status = status_value
        self._requests.put(self._key())

    def on_state_change(self):
        """Call from the NETWORK thread. Enqueues a render request only; it
        NEVER touches the icon (no ``.icon`` / ``.title`` assignment here)."""
        self._requests.put(self._key())

    def signal_update_available(self, version, url):
        """Call from the NETWORK/daemon thread (Phase 13, D-09). Enqueues a
        tray-balloon request (version, url) onto its OWN queue; it NEVER calls
        ``icon.notify`` -- the balloon, like a render request, is drained on the
        UI thread inside :meth:`pump_once`. This preserves the locked cross-thread
        STA boundary (the daemon never touches the icon)."""
        self._balloons.put((version, url))

    def pump_once(self):
        """Call from the UI thread. Drains the update-balloon queue AND the
        render-request queue, then repaints AT MOST once (debounced).

        The balloon drain runs FIRST and is INDEPENDENT of the render-request
        queue: a balloon enqueued on a tick with NO pending render request (the
        COMMON case -- the daemon enqueues a balloon while print state is
        unchanged, so the render queue is empty) must STILL fire. So it is drained
        BEFORE the `if latest is None: return` render early-out below, which only
        short-circuits the REPAINT, never the balloon."""
        # 1) Balloon drain -- independent of the render queue, fires even when no
        #    repaint is pending.
        balloon = None
        try:
            while True:
                balloon = self._balloons.get_nowait()
        except queue.Empty:
            pass
        if balloon is not None:
            version, _url = balloon
            try:
                self._icon.notify(
                    f"Versie {version} is beschikbaar", "Bambu Lab systray"
                )
            except Exception:  # noqa: BLE001 - a notify hiccup must never break the pump
                pass

        # 2) Render-request drain/apply -- its early-out short-circuits ONLY the
        #    repaint, NOT the balloon drain above.
        latest = None
        try:
            while True:
                latest = self._requests.get_nowait()
        except queue.Empty:
            pass
        if latest is None:  # queue was empty -- nothing to repaint
            return
        if latest == self._last_key:  # displayed value unchanged -- debounce
            return
        self._last_key = latest
        self._apply()

    def _apply(self):
        """Repaint from the CURRENT shared PrintState + connection status. The
        key only gates *whether* to repaint; we read state fresh here and derive
        the 5-state display once (UI thread), then paint the matching glyph and
        locked tooltip (image first, then tooltip)."""
        display = status.derive_display_state(self._state, self._status, self._now())
        self._icon.icon = render.render_for_display_state(display, self._state)
        self._icon.title = render.tooltip_for_display_state(display, self._state)

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
        self._last_key = self._key()

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
