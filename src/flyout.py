"""FlyoutWindow: the single pywebview window manager for the tray flyout.

This is the GUI seam of Phase 7. It owns exactly ONE frameless, on-top,
non-resizable WebView2 window created ``hidden`` at startup (CONTEXT LOCKED); a
tray LEFT-click toggles it, and it anchors bottom-right above the taskbar on
each show. The Python->page push paths (``push_state``/``push_theme``/
``push_auth_step``/``push_devices``/``push_error``) call the page's global entry
points via ``window.evaluate_js``, always escaping the payload with
``json.dumps`` so quotes/backslashes can never break out of the JS string
(T-07-01). Only serialize_state output (which carries no secret) is ever pushed.

Testability: ``webview`` and the screen-metrics provider are INJECTED. The real
``import webview`` is lazy INSIDE :meth:`create`, so importing this module never
requires a GUI backend -- the unit suite drives it with fake webview/window
objects and asserts the recorded calls.
"""

import json

from src.paths import resource_path


def _default_screen_size():
    """Return the primary monitor work-area size, or None on any failure.

    Used as the default ``screen_size_provider`` so :meth:`FlyoutWindow.show`
    can anchor bottom-right. On non-Windows hosts, a missing API, or any error
    this returns None and the caller falls back to NOT moving the window (the
    always-on app must never crash on a display quirk).
    """
    try:  # pragma: no cover - exercised only on a real Windows desktop
        import ctypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        # SM_CXSCREEN = 0, SM_CYSCREEN = 1 (primary monitor pixel size).
        width = user32.GetSystemMetrics(0)
        height = user32.GetSystemMetrics(1)
        if width and height:
            return (int(width), int(height))
        return None
    except Exception:  # pragma: no cover - non-Windows / no display
        return None


def _panel_file_url() -> str:
    """Build a ``file://`` URL for the bundled panel.html (dev + frozen)."""
    path = resource_path("src/web/panel.html")
    # pywebview accepts a file:// URL; normalize Windows backslashes to slashes.
    return "file:///" + path.replace("\\", "/").lstrip("/")


class FlyoutWindow:
    """Manage one hidden frameless flyout window with show/hide/toggle + pushes.

    Construct with the :class:`~src.bridge.Api` (the js_api). ``webview`` and
    ``screen_size_provider`` are injectable for tests; in production ``webview``
    defaults to the lazily-imported real module and ``screen_size_provider`` to
    :func:`_default_screen_size`.
    """

    WIDTH = 352
    HEIGHT = 560
    MARGIN = 12
    TASKBAR_HEIGHT = 48  # typical Win11 taskbar; anchor sits above it

    def __init__(
        self,
        api,
        *,
        webview=None,
        screen_size_provider=None,
        panel_url=None,
    ):
        self._api = api
        self._webview = webview  # None -> lazy real import in create()
        self._screen_size_provider = screen_size_provider or _default_screen_size
        self._panel_url = panel_url
        self._window = None
        self.visible = False
        # Current window height; the page drives this via :meth:`resize_to` so the
        # flyout hugs its content. Drives the bottom-right anchor so the window
        # stays glued above the taskbar as it grows/shrinks.
        self._height = self.HEIGHT

    # --- window lifecycle ------------------------------------------------- #

    def create(self):
        """Create the single hidden frameless window (idempotent).

        Lazily imports the real ``webview`` only if none was injected, so module
        import stays GUI-free. A second call is a no-op (one window per app).
        """
        if self._window is not None:
            return self._window
        webview = self._webview
        if webview is None:
            import webview  # lazy: keeps src.flyout importable without a backend
        url = self._panel_url or _panel_file_url()
        self._window = webview.create_window(
            "Bambu Lab systray",
            url=url,
            js_api=self._api,
            frameless=True,
            easy_drag=False,
            on_top=True,
            resizable=False,
            width=self.WIDTH,
            height=self.HEIGHT,
            hidden=True,
        )
        return self._window

    def _anchor(self):
        """Return the bottom-right (x, y) for the window, or None if unknown.

        Computes the corner above the taskbar from the screen size; any failure
        (provider raises or returns None) yields None so :meth:`show` leaves the
        window at its default position rather than crashing.
        """
        try:
            size = self._screen_size_provider()
        except Exception:
            return None
        if not size:
            return None
        screen_w, screen_h = size
        x = screen_w - self.WIDTH - self.MARGIN
        y = screen_h - self._height - self.TASKBAR_HEIGHT - self.MARGIN
        return (x, y)

    def resize_to(self, height):
        """Resize the window to ``height`` px and keep it bottom-right anchored.

        The page calls this (``api.resize``) after each render so the flyout
        hugs its content -- no empty strip below the panel and no scrollbars.
        Re-anchoring keeps the bottom edge glued above the taskbar as the height
        changes (the window grows upward). Non-positive / non-numeric heights are
        ignored and nothing here raises into the GUI loop (a resize hiccup must
        never break the panel).

        CRITICAL: only resize/move while the window is actually VISIBLE. On the
        WebView2 backend ``move``/``resize`` UN-HIDE a hidden window, so applying
        them on a background render (e.g. a live MQTT report's push when the panel
        is closed) would pop the flyout open by itself. When hidden we just record
        the height so the next :meth:`show` anchors to it."""
        if self._window is None:
            return
        try:
            h = int(height)
        except (TypeError, ValueError):
            return
        if h <= 0:
            return
        self._height = h
        if not self.visible:
            return  # don't un-hide a closed panel; show() will use _height
        try:
            self._window.resize(self.WIDTH, h)
        except Exception:  # noqa: BLE001 - a resize failure must not break the UI
            return
        anchor = self._anchor()
        if anchor is not None:
            self._window.move(anchor[0], anchor[1])

    def show(self):
        """Anchor bottom-right (if metrics available) then show the window.

        Stamps ``window.__flyoutShownAt`` in the page BEFORE the OS show call so
        the page's click-away ``blur`` handler can ignore the spurious
        focus->blur that fires the instant a frameless on-top window appears
        (otherwise the flyout would hide itself immediately). Stamping before
        ``show()`` avoids a race with that first blur. ``evaluate_js`` is a
        no-op before the window/page exists, so this stays safe at startup.
        """
        if self._window is None:
            return
        anchor = self._anchor()
        if anchor is not None:
            self._window.move(anchor[0], anchor[1])
        self._evaluate("window.__flyoutShownAt = Date.now()")
        self._window.show()
        self.visible = True

    def hide(self):
        """Hide the window (click-away / tray toggle)."""
        if self._window is None:
            return
        self._window.hide()
        self.visible = False

    def toggle(self):
        """Hide if visible, else show -- the tray LEFT-click action."""
        if self.visible:
            self.hide()
        else:
            self.show()

    def destroy(self):
        """Destroy the window so ``webview.start()`` returns (Afsluiten path)."""
        if self._window is None:
            return
        self._window.destroy()
        self._window = None
        self.visible = False

    def on_loaded(self, callback):
        """Register a callback to run once the window's DOM has loaded.

        pywebview fires ``window.events.loaded`` AFTER ``webview.start()`` is
        running and the page DOM is ready, which is the earliest point at which
        ``evaluate_js`` / the ``push_*`` paths / :meth:`show` are safe. Calling
        any of those before that point raises "Main window failed to start"
        (the v2 startup crash this guards against).

        No-op when there is no window yet, or when the backend exposes no
        ``events`` (e.g. a test fake without an event hub). The real pywebview
        ``Event`` supports ``+=`` (``__iadd__`` returns the event), so we assign
        the result back to keep both real and fake events working.
        """
        if self._window is None:
            return
        events = getattr(self._window, "events", None)
        loaded = getattr(events, "loaded", None)
        if loaded is None:
            return
        self._window.events.loaded += callback

    # --- Python -> page pushes (json.dumps-escaped, no secret) ------------- #

    def _evaluate(self, code: str):
        """Run JS in the page; a no-op before the window exists (never raises)."""
        if self._window is None:
            return
        self._window.evaluate_js(code)

    def push_state(self, state: dict):
        """Push a serialized state dict to ``window.applyState``."""
        self._evaluate(f"window.applyState({json.dumps(state)})")

    def push_theme(self, theme: str):
        """Push the light/dark theme to ``window.applyTheme``."""
        self._evaluate(f"window.applyTheme({json.dumps(theme)})")

    def push_auth_step(self, step: str):
        """Push the auth step (login/code/select) to ``window.applyAuthStep``."""
        self._evaluate(f"window.applyAuthStep({json.dumps(step)})")

    def push_devices(self, devices):
        """Push the printer list to ``window.applyDevices``."""
        self._evaluate(f"window.applyDevices({json.dumps(devices)})")

    def push_error(self, msg):
        """Push an error banner message to ``window.applyError``."""
        self._evaluate(f"window.applyError({json.dumps(msg)})")
