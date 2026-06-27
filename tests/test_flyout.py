"""Unit tests for src.flyout.FlyoutWindow via INJECTED fake webview/window.

No real GUI is created: a FakeWebview records the create_window kwargs and hands
back a FakeWindow that records move/show/hide/evaluate_js/destroy. This proves
the window-manager contract (one hidden frameless window, bottom-right anchor,
toggle, and every evaluate_js push path) without WebView2 -- and that importing
src.flyout never requires pywebview (webview is lazy-imported inside create()).
"""

import json

import pytest

from src.bridge import Api
from src.flyout import FlyoutWindow


class FakeEvent:
    """Tiny stand-in for a pywebview Event: supports ``+=`` (append a callback)
    and a ``fire()`` helper so tests can drive the DOM-loaded callback."""

    def __init__(self):
        self.callbacks = []

    def __iadd__(self, callback):
        self.callbacks.append(callback)
        return self

    def fire(self, *args, **kwargs):
        for cb in list(self.callbacks):
            cb(*args, **kwargs)


class FakeEvents:
    """Stand-in for ``window.events`` exposing a fireable ``loaded`` event."""

    def __init__(self):
        self.loaded = FakeEvent()


class FakeWindow:
    def __init__(self):
        self.moved = []
        self.shown = 0
        self.hidden = 0
        self.evaluated = []
        self.destroyed = 0
        self.resized = []
        self.events = FakeEvents()

    def move(self, x, y):
        self.moved.append((x, y))

    def resize(self, w, h):
        self.resized.append((w, h))

    def show(self):
        self.shown += 1

    def hide(self):
        self.hidden += 1

    def evaluate_js(self, code):
        self.evaluated.append(code)

    def destroy(self):
        self.destroyed += 1


class FakeWebview:
    def __init__(self):
        self.create_kwargs = []
        self.window = FakeWindow()

    def create_window(self, title, **kwargs):
        self.create_kwargs.append({"title": title, **kwargs})
        return self.window


def _make(screen_size=(1920, 1080)):
    api = Api(handlers={})
    fake = FakeWebview()
    fw = FlyoutWindow(
        api,
        webview=fake,
        screen_size_provider=lambda: screen_size,
        panel_url="file:///panel.html",
    )
    return api, fake, fw


# --------------------------------------------------------------------------- #
# create()                                                                    #
# --------------------------------------------------------------------------- #


def test_create_passes_locked_window_kwargs():
    api, fake, fw = _make()
    fw.create()
    assert len(fake.create_kwargs) == 1
    kw = fake.create_kwargs[0]
    assert kw["frameless"] is True
    assert kw["easy_drag"] is False
    assert kw["on_top"] is True
    assert kw["resizable"] is False
    assert kw["hidden"] is True
    assert kw["width"] == 352
    assert kw["js_api"] is api
    assert kw["url"] == "file:///panel.html"


def test_create_is_idempotent():
    api, fake, fw = _make()
    fw.create()
    fw.create()
    assert len(fake.create_kwargs) == 1


# --------------------------------------------------------------------------- #
# show / hide / toggle / position                                             #
# --------------------------------------------------------------------------- #


def test_show_anchors_bottom_right_exact():
    api, fake, fw = _make(screen_size=(1920, 1080))
    fw.create()
    fw.show()
    # x = 1920 - 352 - MARGIN ; y = 1080 - HEIGHT - TASKBAR - MARGIN
    expected_x = 1920 - FlyoutWindow.WIDTH - FlyoutWindow.MARGIN
    expected_y = (
        1080 - FlyoutWindow.HEIGHT - FlyoutWindow.TASKBAR_HEIGHT - FlyoutWindow.MARGIN
    )
    assert fake.window.moved[-1] == (expected_x, expected_y)
    assert fake.window.shown == 1
    assert fw.visible is True


def test_show_arms_blur_grace_stamp():
    """show() stamps window.__flyoutShownAt so the page's click-away blur handler
    can ignore the spurious focus->blur that fires the instant a frameless on-top
    window appears (which would otherwise immediately hide the flyout)."""
    api, fake, fw = _make()
    fw.create()
    fw.show()
    assert fake.window.shown == 1
    stamps = [c for c in fake.window.evaluated if "__flyoutShownAt" in c]
    assert stamps == ["window.__flyoutShownAt = Date.now()"]


def test_resize_to_resizes_and_reanchors_bottom_right_when_visible():
    """While VISIBLE, resize_to() resizes to (WIDTH, height) and re-anchors
    bottom-right using the NEW height so the flyout stays glued above the
    taskbar as it grows."""
    api, fake, fw = _make(screen_size=(1920, 1080))
    fw.create()
    fw.show()  # must be visible: resize/move would otherwise un-hide it
    fw.resize_to(440)
    assert fake.window.resized[-1] == (FlyoutWindow.WIDTH, 440)
    expected_x = 1920 - FlyoutWindow.WIDTH - FlyoutWindow.MARGIN
    expected_y = 1080 - 440 - FlyoutWindow.TASKBAR_HEIGHT - FlyoutWindow.MARGIN
    assert fake.window.moved[-1] == (expected_x, expected_y)


def test_resize_to_while_hidden_does_not_resize_or_move():
    """While HIDDEN, resize_to() must NOT resize/move (those un-hide the WebView2
    window, popping the panel open by itself on a background render) -- it only
    records the height for the next show()."""
    api, fake, fw = _make(screen_size=(1920, 1080))
    fw.create()  # created hidden, never shown -> visible is False
    fw.resize_to(440)
    assert fake.window.resized == []   # no resize while hidden
    assert fake.window.moved == []     # no move while hidden
    # The height is remembered: a later show() anchors using it.
    fw.show()
    expected_y = 1080 - 440 - FlyoutWindow.TASKBAR_HEIGHT - FlyoutWindow.MARGIN
    assert fake.window.moved[-1][1] == expected_y


def test_resize_to_ignores_nonpositive_and_nonnumeric():
    """A non-positive or non-numeric height is ignored (no resize, no raise)."""
    api, fake, fw = _make()
    fw.create()
    for bad in (0, -10, None, "abc"):
        fw.resize_to(bad)
    assert fake.window.resized == []


def test_hide_sets_invisible():
    api, fake, fw = _make()
    fw.create()
    fw.show()
    fw.hide()
    assert fake.window.hidden == 1
    assert fw.visible is False


def test_toggle_shows_then_hides():
    api, fake, fw = _make()
    fw.create()
    fw.toggle()
    assert fw.visible is True
    assert fake.window.shown == 1
    fw.toggle()
    assert fw.visible is False
    assert fake.window.hidden == 1


def test_show_without_screen_metrics_does_not_move():
    api = Api(handlers={})
    fake = FakeWebview()
    fw = FlyoutWindow(
        api,
        webview=fake,
        screen_size_provider=lambda: None,
        panel_url="file:///panel.html",
    )
    fw.create()
    fw.show()
    assert fake.window.moved == []  # fell back to default position, no move
    assert fake.window.shown == 1


def test_show_when_screen_provider_raises_does_not_move():
    api = Api(handlers={})
    fake = FakeWebview()

    def boom():
        raise RuntimeError("no display")

    fw = FlyoutWindow(
        api, webview=fake, screen_size_provider=boom, panel_url="file:///panel.html"
    )
    fw.create()
    fw.show()  # must not raise
    assert fake.window.moved == []
    assert fake.window.shown == 1


# --------------------------------------------------------------------------- #
# evaluate_js push paths                                                       #
# --------------------------------------------------------------------------- #


def test_push_state_emits_applyState_with_json():
    api, fake, fw = _make()
    fw.create()
    state = {"loggedIn": True, "pct": 63, "file": 'a"b'}
    fw.push_state(state)
    assert len(fake.window.evaluated) == 1
    code = fake.window.evaluated[0]
    assert "window.applyState(" in code
    assert json.dumps(state) in code


def test_push_theme_emits_applyTheme_with_json():
    api, fake, fw = _make()
    fw.create()
    fw.push_theme("light")
    code = fake.window.evaluated[-1]
    assert "window.applyTheme(" in code
    assert json.dumps("light") in code


def test_push_auth_step_emits_applyAuthStep():
    api, fake, fw = _make()
    fw.create()
    fw.push_auth_step("code")
    code = fake.window.evaluated[-1]
    assert "window.applyAuthStep(" in code
    assert json.dumps("code") in code


def test_push_devices_emits_applyDevices():
    api, fake, fw = _make()
    fw.create()
    devices = [{"id": "dev1", "name": "X1"}]
    fw.push_devices(devices)
    code = fake.window.evaluated[-1]
    assert "window.applyDevices(" in code
    assert json.dumps(devices) in code


def test_push_error_emits_applyError():
    api, fake, fw = _make()
    fw.create()
    fw.push_error("oops")
    code = fake.window.evaluated[-1]
    assert "window.applyError(" in code
    assert json.dumps("oops") in code


def test_push_update_emits_applyUpdate_with_json():
    api, fake, fw = _make()
    fw.create()
    info = {"version": "2.2.0", "html_url": 'https://x/"rel"'}
    fw.push_update(info)
    code = fake.window.evaluated[-1]
    assert "window.applyUpdate(" in code
    # json.dumps-escaped so the embedded quotes can't break out of the JS string.
    assert json.dumps(info) in code


def test_push_update_before_create_is_noop():
    api, fake, fw = _make()
    fw.push_update({"version": "2.2.0", "html_url": "https://x/rel"})  # no create()
    assert fake.window.evaluated == []


def test_push_before_create_is_noop():
    api, fake, fw = _make()
    # no create() called
    fw.push_state({"a": 1})
    fw.push_theme("dark")
    # nothing to evaluate against; must not raise and window never used
    assert fake.window.evaluated == []


def test_destroy_calls_window_destroy():
    api, fake, fw = _make()
    fw.create()
    fw.destroy()
    assert fake.window.destroyed == 1


def test_destroy_before_create_is_noop():
    api, fake, fw = _make()
    fw.destroy()  # must not raise
    assert fake.window.destroyed == 0


# --------------------------------------------------------------------------- #
# lazy webview import + no-secret invariant                                   #
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# on_loaded() DOM-loaded callback registration                                #
# --------------------------------------------------------------------------- #


def test_on_loaded_registers_on_events_loaded():
    api, fake, fw = _make()
    fw.create()
    fired = []
    fw.on_loaded(lambda: fired.append("loaded"))
    # Registered on the window's events.loaded but not yet fired.
    assert fake.window.events.loaded.callbacks  # callback registered
    assert fired == []
    # Firing the loaded event runs the callback.
    fake.window.events.loaded.fire()
    assert fired == ["loaded"]


def test_on_loaded_before_create_is_noop():
    api, fake, fw = _make()
    # no create() -> no window yet; registering must not raise and do nothing.
    fw.on_loaded(lambda: None)
    assert fake.window.events.loaded.callbacks == []


def test_on_loaded_noop_when_window_has_no_events():
    api, fake, fw = _make()
    fw.create()

    class NoEventsWindow:
        pass

    # Simulate a backend/fake window that exposes no events hub.
    fw._window = NoEventsWindow()
    fw.on_loaded(lambda: None)  # must not raise


def test_module_imports_without_pywebview():
    # Importing src.flyout must not require a real webview backend; the import
    # of pywebview is lazy inside create(). This test simply imports the module
    # and inspects that no top-level `import webview` statement exists.
    import inspect

    import src.flyout as flyout_mod

    src = inspect.getsource(flyout_mod)
    # The only `webview` references at module scope should be the lazy import
    # inside create(); assert there is no bare top-level import line.
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("import webview") and not line.startswith(" "):
            pytest.fail("webview imported at module top-level (must be lazy)")
