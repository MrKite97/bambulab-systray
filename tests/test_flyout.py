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


class FakeWindow:
    def __init__(self):
        self.moved = []
        self.shown = 0
        self.hidden = 0
        self.evaluated = []
        self.destroyed = 0

    def move(self, x, y):
        self.moved.append((x, y))

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
