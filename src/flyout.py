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
        y = screen_h - self.HEIGHT - self.TASKBAR_HEIGHT - self.MARGIN
        return (x, y)

    def show(self):
        """Anchor bottom-right (if metrics available) then show the window."""
        if self._window is None:
            return
        anchor = self._anchor()
        if anchor is not None:
            self._window.move(anchor[0], anchor[1])
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
