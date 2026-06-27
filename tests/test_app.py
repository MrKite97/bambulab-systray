"""Tests for src.app -- the tray wiring layer.

Everything is faked: NO real broker, NO real login, NO real tray, NO real
credentials/tokens. The pystray ``Icon`` is replaced by a ``FakeIcon`` (with a
``stop()`` recorder + ``icon``/``title`` attrs); ``auth.*`` and
``mqtt_client.build_client`` are monkeypatched so ``build_app`` runs without
network. Assertions are on the load-bearing contracts:

  * the menu label is exactly 'Afsluiten' (QUIT_LABEL),
  * 'Afsluiten' disconnects the client + disposes the icon (icon.stop() once),
  * the on_message wrapper merges the delta AND signals on_state_change,
  * a malformed payload does NOT raise (reuses the guarded mqtt_client merge),
  * build_app returns a wired controller/icon/client triple with no network.

Mirrors the CallRecorder + monkeypatch style of tests/test_spike.py.
"""

import json
import threading

from src import app
from src.state import PrintState
from src.status import ConnectionStatus, DisplayState
from src.tray import TrayController


class CallRecorder:
    """Records calls to a patched function and returns a canned value."""

    def __init__(self, return_value=None):
        self.return_value = return_value
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.return_value


class FakeIcon:
    """Stand-in for a pystray Icon: records icon/title assignments, stop(), and
    the v2 run_detached(setup=...) call (so main()'s threading inversion is
    verifiable without a real tray)."""

    def __init__(self):
        self.icon = None
        self.title = None
        self.visible = False
        self.stop_count = 0
        self.run_detached_calls = 0
        self.run_count = 0

    def stop(self):
        self.stop_count += 1

    def run(self, setup=None):
        self.run_count += 1

    def run_detached(self, setup=None):
        self.run_detached_calls += 1


class FakeClient:
    """Stand-in for a paho client: records disconnect()/user_data_set and the
    callbacks wired onto it (so build_app's wiring can be inspected)."""

    def __init__(self, *, client_id=None, username=None, access_token=None):
        self.client_id = client_id
        self.username = username
        self.access_token = access_token
        self.userdata = None
        self.disconnect_count = 0
        self.on_connect = None
        self.on_message = None
        self.on_disconnect = None

    def user_data_set(self, ud):
        self.userdata = ud

    def disconnect(self):
        self.disconnect_count += 1


class FakeController:
    """Records on_state_change calls (the producer-side signal) and every
    connection-status enqueue (so the status transitions can be asserted without
    a real icon or pump)."""

    def __init__(self):
        self.signal_count = 0
        self.statuses = []

    def on_state_change(self):
        self.signal_count += 1

    def set_connection_status(self, status_value):
        self.statuses.append(status_value)


class FakeMsg:
    """A fake MQTT message carrying a JSON (or raw) payload."""

    def __init__(self, payload):
        self.payload = payload


class FakeWindow:
    """Stand-in for a pywebview window: records show/hide/move/destroy and the
    evaluate_js calls (so theme/state pushes can be inspected) -- no real GUI."""

    def __init__(self):
        self.shown = 0
        self.hidden = 0
        self.destroyed = 0
        self.moves = []
        self.evaluated = []

    def show(self):
        self.shown += 1

    def hide(self):
        self.hidden += 1

    def move(self, x, y):
        self.moves.append((x, y))

    def destroy(self):
        self.destroyed += 1

    def evaluate_js(self, code):
        self.evaluated.append(code)


class FakeWebview:
    """Stand-in for the pywebview module injected into build_app/main.

    ``create_window`` returns a FakeWindow (recorded) and ``start`` is a no-op
    that records the call -- so the whole flyout/threading wiring is verified
    with NO real GUI backend."""

    def __init__(self):
        self.windows = []
        self.created = []
        self.start_calls = 0

    def create_window(self, title, **kwargs):
        win = FakeWindow()
        win.title = title
        win.kwargs = kwargs
        self.windows.append(win)
        self.created.append((title, kwargs))
        return win

    def start(self, *args, **kwargs):
        self.start_calls += 1


# --- Menu label -------------------------------------------------------------


def test_quit_label_is_afsluiten():
    """The single right-click menu label is exactly 'Afsluiten' (LOCKED)."""
    assert app.QUIT_LABEL == "Afsluiten"


def test_built_menu_items_are_relogin_and_afsluiten(monkeypatch):
    """The pystray menu carries BOTH 'Opnieuw verbinden / inloggen' AND 'Afsluiten'
    (the re-login item sits alongside Afsluiten -- REL-02)."""
    icon = _patch_build_app(monkeypatch)
    app.build_app("HEADER.eyJ1c2VybmFtZSI6InVfMSJ9.SIG")
    labels = [text for text, _ in icon["menu_items"]]
    assert "Opnieuw verbinden / inloggen" in labels
    assert "Afsluiten" in labels


# --- Quit teardown ----------------------------------------------------------


def test_quit_handler_disconnects_and_stops_icon():
    """Calling 'Afsluiten' sets the shutdown event, disconnects the client, and
    disposes the icon (icon.stop called exactly once -- orphaned-icon guard)."""
    client = FakeClient()
    event = threading.Event()
    icon = FakeIcon()

    handler = app.make_quit_handler(client, event)
    handler(icon, item=None)

    assert event.is_set()
    assert client.disconnect_count == 1
    assert icon.stop_count == 1


def test_quit_handler_stops_icon_even_if_disconnect_raises():
    """A disconnect() failure must NOT prevent icon.stop() -- no orphaned icon."""

    class RaisingClient(FakeClient):
        def disconnect(self):
            raise RuntimeError("already disconnected")

    event = threading.Event()
    icon = FakeIcon()

    handler = app.make_quit_handler(RaisingClient(), event)
    handler(icon, item=None)

    assert event.is_set()
    assert icon.stop_count == 1  # icon still disposed despite disconnect error


# --- Phase 14: make_update_apply (download -> verify -> spawn -> teardown) --- #


class _OrderRecorder:
    """A single ordered list every fake appends to, so the download -> spawn ->
    teardown call order is observable end-to-end."""

    def __init__(self):
        self.events = []


def _apply_fakes(order, *, download_raises=False, info=object()):
    """Build the icon/client/flyout/download/spawn fakes wired to one recorder."""

    class _Icon:
        def stop(self):
            order.events.append("stop")

    class _Client:
        def disconnect(self):
            order.events.append("disconnect")

    class _Flyout:
        def destroy(self):
            order.events.append("destroy")

        def push_error(self, msg):
            order.events.append(("push_error", msg))

    def _download(i):
        order.events.append("download")
        if download_raises:
            raise app.UpdateError("boom")
        return "C:/Temp/Setup.exe"

    def _spawn(path):
        order.events.append("spawn")

    return _Icon(), _Client(), _Flyout(), _download, _spawn


def test_make_update_apply_success_order():
    """On a verified download: download -> spawn -> (destroy -> disconnect ->
    stop), proving verify-before-spawn THEN the exact locked quit order."""
    order = _OrderRecorder()
    info = object()
    icon, client, flyout, download, spawn = _apply_fakes(order, info=info)
    event = threading.Event()

    apply = app.make_update_apply(
        icon,
        client,
        event,
        flyout,
        get_update_info=lambda: info,
        download=download,
        spawn=spawn,
    )
    apply()

    assert order.events == ["download", "spawn", "destroy", "disconnect", "stop"]
    assert event.is_set()


def test_make_update_apply_failure_no_spawn_no_teardown():
    """A download/verify failure pushes a visible error and does NOT spawn or tear
    down -- the app keeps running."""
    order = _OrderRecorder()
    info = object()
    icon, client, flyout, download, spawn = _apply_fakes(
        order, download_raises=True, info=info
    )
    event = threading.Event()

    apply = app.make_update_apply(
        icon,
        client,
        event,
        flyout,
        get_update_info=lambda: info,
        download=download,
        spawn=spawn,
    )
    apply()

    assert ("push_error", app.UPDATE_FAILED_MESSAGE) in order.events
    assert "spawn" not in order.events
    assert "destroy" not in order.events
    assert "disconnect" not in order.events
    assert "stop" not in order.events
    assert not event.is_set()  # app stays up


def test_make_update_apply_no_info_pushes_error_no_spawn():
    """No actionable UpdateInfo -> push_error, never download/spawn/teardown."""
    order = _OrderRecorder()
    icon, client, flyout, download, spawn = _apply_fakes(order)
    event = threading.Event()

    apply = app.make_update_apply(
        icon,
        client,
        event,
        flyout,
        get_update_info=lambda: None,
        download=download,
        spawn=spawn,
    )
    apply()

    assert ("push_error", app.UPDATE_FAILED_MESSAGE) in order.events
    assert "download" not in order.events
    assert "spawn" not in order.events
    assert not event.is_set()


# --- on_message signalling --------------------------------------------------


def test_on_message_merges_and_signals():
    """The wrapped on_message merges the delta (percent==42) AND calls the
    controller's on_state_change exactly once."""
    controller = FakeController()
    on_message = app.make_on_message(controller)

    state = PrintState()
    userdata = {"serial": "S", "state": state}
    payload = json.dumps(
        {"print": {"mc_percent": 42, "gcode_state": "RUNNING", "mc_remaining_time": 83}}
    )

    on_message(client=None, userdata=userdata, msg=FakeMsg(payload))

    assert state.mc_percent == 42  # delta merged into the shared PrintState
    assert state.gcode_state == "RUNNING"
    assert controller.signal_count == 1  # controller signalled exactly once


def test_on_message_bad_payload_does_not_raise():
    """A non-JSON payload does NOT raise (reuses the guarded mqtt_client merge).
    The signal still fires (enqueue cannot fail on bad input)."""
    controller = FakeController()
    on_message = app.make_on_message(controller)

    state = PrintState()
    userdata = {"serial": "S", "state": state}

    # Must not raise on garbage bytes.
    on_message(client=None, userdata=userdata, msg=FakeMsg(b"not-json{{{"))

    # State untouched (still defaults); the guarded merge swallowed the bad input.
    assert state.mc_percent == 0
    assert controller.signal_count == 1


# --- Plan 09-01: live-push of serialized state to the flyout ----------------


class RecordingPushFlyout:
    """Stand-in for FlyoutWindow recording every push_state(dict) call so the
    live-push wiring (serialize-and-push on report + throttle + re-push on show)
    is assertable with NO real GUI/broker. ``visible`` is settable so the
    show/hide branches of the toggle can be driven."""

    def __init__(self, visible=False):
        self.visible = visible
        self.pushes = []        # every state dict pushed to the page
        self.themes = []        # every theme pushed
        self.events = []        # ordered call log (toggle/show/hide)

    def push_state(self, state):
        self.pushes.append(state)

    def push_theme(self, theme):
        self.themes.append(theme)
        self.events.append(("push_theme", theme))

    def show(self):
        self.events.append("show")
        self.visible = True

    def hide(self):
        self.events.append("hide")
        self.visible = False

    def toggle(self):
        self.events.append("toggle")
        self.visible = not self.visible


def _report(**fields):
    """Build a FakeMsg carrying a JSON ``{"print": {...}}`` report delta."""
    return FakeMsg(json.dumps({"print": fields}))


def test_on_message_pushes_serialized_state_to_flyout(monkeypatch):
    """A report that changes a displayed field is merged AND its serialized state
    is pushed to the flyout, with pct/etaLabel/status reflecting the merge."""
    monkeypatch.setattr(app.render, "detect_windows_theme", lambda: "dark")
    controller = FakeController()
    flyout = RecordingPushFlyout()
    state = PrintState()

    on_message = app.make_on_message(
        controller, flyout=flyout, state=state, printer_name="MyP1"
    )
    userdata = {"serial": "S", "state": state}
    on_message(
        client=None,
        userdata=userdata,
        msg=_report(mc_percent=42, gcode_state="RUNNING", mc_remaining_time=83),
    )

    assert controller.signal_count == 1  # tray repaint still enqueued
    assert len(flyout.pushes) == 1
    pushed = flyout.pushes[0]
    assert pushed["pct"] == 42
    assert pushed["status"] == "printing"
    assert pushed["etaLabel"] == "nog 1 u 23 min"  # MINUTES -> ETA, not regressed
    assert pushed["printerName"] == "MyP1"


def test_on_message_without_flyout_still_signals(monkeypatch):
    """make_on_message stays backward-compatible: with no flyout it merges +
    signals exactly as before and never tries to push."""
    controller = FakeController()
    on_message = app.make_on_message(controller)  # no flyout (v1 path)
    state = PrintState()
    userdata = {"serial": "S", "state": state}

    on_message(client=None, userdata=userdata, msg=_report(mc_percent=7, gcode_state="RUNNING"))

    assert state.mc_percent == 7
    assert controller.signal_count == 1


def test_on_message_throttles_identical_reports(monkeypatch):
    """Rapid IDENTICAL reports (no displayed field changed) push to the flyout
    FEWER times than the number of reports -- the throttle coalesces them."""
    monkeypatch.setattr(app.render, "detect_windows_theme", lambda: "dark")
    controller = FakeController()
    flyout = RecordingPushFlyout()
    state = PrintState()
    on_message = app.make_on_message(controller, flyout=flyout, state=state)
    userdata = {"serial": "S", "state": state}

    msg = _report(mc_percent=10, gcode_state="RUNNING", mc_remaining_time=30)
    for _ in range(5):
        on_message(client=None, userdata=userdata, msg=msg)

    # The throttle suppressed the redundant identical pushes.
    assert len(flyout.pushes) < 5
    assert len(flyout.pushes) == 1  # only the first (changed) report pushed


def test_on_message_terminal_state_always_pushed(monkeypatch):
    """A report flipping gcode_state to FINISH (status 'done') is ALWAYS pushed,
    even when the throttle would otherwise suppress it -- terminal state is never
    silently dropped (security_note T-09-04)."""
    monkeypatch.setattr(app.render, "detect_windows_theme", lambda: "dark")
    controller = FakeController()
    flyout = RecordingPushFlyout()
    state = PrintState()
    on_message = app.make_on_message(controller, flyout=flyout, state=state)
    userdata = {"serial": "S", "state": state}

    # First, a steady RUNNING report (pushed once, sets the throttle baseline).
    on_message(client=None, userdata=userdata,
               msg=_report(mc_percent=100, gcode_state="RUNNING", mc_remaining_time=0))
    pushes_before = len(flyout.pushes)

    # Now flip to FINISH. Even if pct/eta are unchanged, the terminal transition
    # must push.
    on_message(client=None, userdata=userdata,
               msg=_report(gcode_state="FINISH"))

    assert len(flyout.pushes) == pushes_before + 1
    assert flyout.pushes[-1]["status"] == "done"


def test_on_message_does_not_block_on_evaluate_js(monkeypatch):
    """The push is fire-and-forget: make_on_message never gates the network thread
    on a push_state return value, and a push_state that raises must not crash the
    callback thread (the report merge + signal still complete)."""
    monkeypatch.setattr(app.render, "detect_windows_theme", lambda: "dark")
    controller = FakeController()
    state = PrintState()

    class RaisingFlyout(RecordingPushFlyout):
        def push_state(self, s):
            raise RuntimeError("page JS blew up")

    on_message = app.make_on_message(controller, flyout=RaisingFlyout(), state=state)
    userdata = {"serial": "S", "state": state}

    # Must not raise out of the network callback even though push_state raises.
    on_message(client=None, userdata=userdata,
               msg=_report(mc_percent=5, gcode_state="RUNNING"))

    assert state.mc_percent == 5          # merge still happened
    assert controller.signal_count == 1   # tray signal still fired


# --- build_app with no network ----------------------------------------------


def _patch_build_app(monkeypatch):
    """Monkeypatch auth.* + pystray so build_app runs with no network/tray.

    Returns a shared dict capturing the constructed FakeIcon and the menu items
    so tests can inspect the wiring. A FakeWebview is injected as the default
    ``webview=`` of build_app so flyout.create() builds a FakeWindow (NO real GUI
    backend is ever imported); it is exposed as ``captured["webview"]``.
    """
    captured = {"menu_items": [], "icon_obj": None}

    # Inject a FakeWebview into every build_app call (so flyout.create() makes a
    # FakeWindow, never importing the real pywebview). Wrap build_app so tests
    # that don't pass webview= still run fully headless; an explicit webview=
    # still wins.
    _fake_webview = FakeWebview()
    captured["webview"] = _fake_webview
    _real_build_app = app.build_app

    def _build_app(token, **kwargs):
        kwargs.setdefault("webview", _fake_webview)
        return _real_build_app(token, **kwargs)

    monkeypatch.setattr(app, "build_app", _build_app)

    monkeypatch.setattr(app.auth, "mqtt_username_from_token", lambda t: "u_1")
    monkeypatch.setattr(app.auth, "get_device_list", lambda t: [{"dev_id": "SER"}])
    monkeypatch.setattr(app.auth, "pick_serial", lambda devs: "SER")
    # Never touch the real settings JSON / registry from build_app: stub the
    # module-level settings + autostart so persist_serial and the checkable item
    # run against in-memory fakes.
    monkeypatch.setattr(app.settings_module, "load_settings", lambda: {"region": "global", "serial": None})
    monkeypatch.setattr(app.settings_module, "save_settings", lambda s: None)
    monkeypatch.setattr(app.autostart, "is_enabled", lambda: False)
    monkeypatch.setattr(app.autostart, "enable", lambda: None)
    monkeypatch.setattr(app.autostart, "disable", lambda: None)
    monkeypatch.setattr(
        app.mqtt_client,
        "build_client",
        lambda client_id, username, access_token: FakeClient(
            client_id=client_id, username=username, access_token=access_token
        ),
    )

    # Fake pystray Icon/Menu/MenuItem so no real tray is constructed.
    class FakeMenuItem:
        def __init__(self, text, action, checked=None, default=False, visible=True):
            self.text = text
            self.action = action
            self.checked = checked
            self.default = default
            self.visible = visible
            # Only record VISIBLE items in menu_items so the existing label
            # assertions (autostart / re-login / Afsluiten) ignore the hidden
            # default left-click 'Open' item.
            if visible:
                captured["menu_items"].append((text, action))
            captured.setdefault("menu_objs", []).append(self)

        def __call__(self, icon):
            # Mirror pystray: invoking a selected item forwards to the action as
            # action(icon, item). Used by the flyout-toggle wiring test.
            return self.action(icon, self)

    def fake_menu(*items):
        return list(items)

    def fake_icon(name, icon=None, title=None, menu=None):
        obj = FakeIcon()
        obj.name = name
        obj.icon = icon
        obj.title = title
        obj.menu = menu
        captured["icon_obj"] = obj
        return obj

    monkeypatch.setattr(app.pystray, "Icon", fake_icon)
    monkeypatch.setattr(app.pystray, "Menu", fake_menu)
    monkeypatch.setattr(app.pystray, "MenuItem", FakeMenuItem)

    return captured


def test_build_app_returns_wired_triple_without_network(monkeypatch):
    """build_app composes icon + controller + client (and a network_runner)
    using only fakes -- no real login, broker, or tray."""
    _patch_build_app(monkeypatch)

    result = app.build_app("HEADER.eyJ1c2VybmFtZSI6InVfMSJ9.SIG")

    assert result["icon"] is not None
    assert result["controller"] is not None
    assert isinstance(result["client"], FakeClient)
    assert callable(result["network_runner"])
    # The client was wired: userdata carries serial+state, on_message is set.
    assert result["client"].userdata["serial"] == "SER"
    assert result["client"].userdata["state"] is result["state"]
    assert result["client"].on_message is not None
    # on_connect is now WRAPPED (it also publishes the CONNECTED status) -- it is
    # no longer the bare mqtt_client.on_connect but a wrapper that calls it.
    assert callable(result["client"].on_connect)
    assert result["client"].on_connect is not app.mqtt_client.on_connect
    assert callable(result["client"].on_disconnect)


def test_build_app_does_not_log_token(monkeypatch, caplog):
    """build_app must never emit the token into logs (T-02-06)."""
    import logging

    _patch_build_app(monkeypatch)
    token = "HEADER.eyJ1c2VybmFtZSI6InVfMSJ9.SUPERSECRETSIG"
    with caplog.at_level(logging.DEBUG, logger="app"):
        app.build_app(token)

    assert "SUPERSECRETSIG" not in caplog.text
    assert token not in caplog.text


# --- Status transitions (connect / disconnect / 401) ------------------------


class FakeClock:
    """An injectable monotonic clock the tests advance by hand (no real waits)."""

    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def test_on_connect_wrapper_sets_connected_status():
    """The wrapped on_connect calls mqtt_client.on_connect AND enqueues a
    CONNECTED status transition (network-thread enqueue, never icon mutation)."""
    controller = FakeController()
    on_connect = app.make_on_connect(controller)

    # Drive the wrapped callback with the same signature paho v2 uses; userdata
    # carries the serial the inner mqtt_client.on_connect subscribes/pushes for.
    class FakeClientCb:
        def __init__(self):
            self.subscribed = []
            self.published = []

        def subscribe(self, topic):
            self.subscribed.append(topic)

        def publish(self, topic, payload):
            self.published.append((topic, payload))

    client = FakeClientCb()
    on_connect(client, {"serial": "SER"}, None, 0, None)

    assert client.subscribed == ["device/SER/report"]  # inner on_connect ran
    assert controller.statuses == [ConnectionStatus.CONNECTED]


def test_on_disconnect_wrapper_sets_disconnected_status():
    """The wrapped on_disconnect calls the inert mqtt_client.on_disconnect AND
    enqueues a DISCONNECTED status transition."""
    controller = FakeController()
    on_disconnect = app.make_on_disconnect(controller)

    on_disconnect(client=None, userdata=None, disconnect_flags=None, reason_code=0, properties=None)

    assert controller.statuses == [ConnectionStatus.DISCONNECTED]


def test_auth_failure_sets_token_expired_status():
    """A 401/auth rejection on connect (detected via spike._auth_failed) sets
    ConnectionStatus.TOKEN_EXPIRED before the re-login path runs -- and the
    connect wrapper still drives mqtt_client.run_session's backoff (no new loop:
    it raises StopSession to hand control back to run_session)."""
    controller = FakeController()
    relogin_calls = []

    def fake_relogin():
        relogin_calls.append(True)

    # An inner connect that raises an auth-equivalent error (paho phrasing).
    def failing_connect(client):
        raise RuntimeError("Connection Refused: not authorized.")

    wrapped = app.make_status_connect(
        controller, failing_connect, on_auth_fail=fake_relogin
    )

    # The wrapper must detect the auth failure, flag TOKEN_EXPIRED, run the
    # re-login hook, then re-raise so run_session keeps owning the loop/backoff.
    import pytest

    with pytest.raises(Exception):
        wrapped(client=None)

    assert ConnectionStatus.TOKEN_EXPIRED in controller.statuses
    assert relogin_calls == [True]


def test_non_auth_failure_does_not_set_token_expired():
    """A transient (non-auth) connect failure must NOT flag TOKEN_EXPIRED -- it
    just propagates so run_session backs off and retries."""
    controller = FakeController()

    def failing_connect(client):
        raise RuntimeError("Connection timed out")

    wrapped = app.make_status_connect(controller, failing_connect, on_auth_fail=lambda: None)

    import pytest

    with pytest.raises(Exception):
        wrapped(client=None)

    assert ConnectionStatus.TOKEN_EXPIRED not in controller.statuses


def test_freshness_pump_tick_flips_to_offline():
    """A pump tick AFTER advancing the injected clock past FRESHNESS_TIMEOUT_SECONDS
    flips an idle-but-stale stream to the 'Printer offline' tooltip WITHOUT a new
    MQTT message -- the freshness watcher rides the existing pump."""
    from src import status

    clock = FakeClock(now=1000.0)
    icon = FakeIcon()
    state = PrintState()
    controller = TrayController(icon, state, now=clock)

    # Simulate a fresh idle report just arrived (connected, gcode_state idle).
    controller.set_connection_status(ConnectionStatus.CONNECTED)
    state.gcode_state = "IDLE"
    state.last_update_monotonic = clock.now  # fresh as of "now"
    controller.on_state_change()
    controller.pump_once()
    assert icon.title == status.TOOLTIP_NO_ACTIVE_PRINT  # idle, not offline

    # No new message arrives; time passes beyond the freshness window.
    clock.advance(status.FRESHNESS_TIMEOUT_SECONDS + 1)

    # A bare pump tick (the watcher) must now repaint to "Printer offline".
    controller.on_state_change()  # the watcher re-enqueues the derived key
    controller.pump_once()
    assert icon.title == status.TOOLTIP_PRINTER_OFFLINE


def test_build_app_injects_clock_into_controller(monkeypatch):
    """build_app accepts an injectable monotonic clock and threads it into the
    TrayController so freshness is driveable in tests (no real waits)."""
    _patch_build_app(monkeypatch)
    clock = FakeClock(now=500.0)

    result = app.build_app("HEADER.eyJ1c2VybmFtZSI6InVfMSJ9.SIG", now=clock)

    # The controller derives freshness from the injected clock.
    assert result["controller"]._now is clock


# --- Re-login menu + flow ---------------------------------------------------


def test_relogin_label_is_locked():
    """RELOGIN_LABEL is exactly 'Opnieuw verbinden / inloggen' (LOCKED)."""
    assert app.RELOGIN_LABEL == "Opnieuw verbinden / inloggen"


def test_relogin_menu_clears_token_and_relogins(monkeypatch):
    """The re-login handler, in order: flags TOKEN_EXPIRED, clears the stale token
    BEFORE the fresh login, then drives get_token(force_relogin=True). No silent
    refresh; no real network/stdin."""
    events = []

    monkeypatch.setattr(
        app.token_store, "clear_token", lambda: events.append("clear_token")
    )

    def fake_get_token(*, force_relogin):
        events.append(("get_token", force_relogin))
        return "NEWTOKEN"

    controller = FakeController()
    client = FakeClient()
    handler = app.make_relogin_handler(
        client, threading.Event(), get_token=fake_get_token, controller=controller
    )

    # Invoke like a pystray menu callback (icon, item).
    handler(icon=FakeIcon(), item=None)

    # TOKEN_EXPIRED flagged so the tray shows "Opnieuw inloggen vereist".
    assert ConnectionStatus.TOKEN_EXPIRED in controller.statuses
    # clear_token ran BEFORE the fresh login (stale-credential reuse prevented).
    assert events == ["clear_token", ("get_token", True)]
    # The live session was torn down so the existing backoff reconnects fresh.
    assert client.disconnect_count == 1


def test_relogin_handler_callable_with_no_args(monkeypatch):
    """The handler also works as a zero-arg on_auth_fail() hook (the connect
    wrapper calls it without icon/item)."""
    monkeypatch.setattr(app.token_store, "clear_token", lambda: None)
    controller = FakeController()
    handler = app.make_relogin_handler(
        FakeClient(), threading.Event(),
        get_token=lambda *, force_relogin: "T", controller=controller,
    )
    handler()  # no args -- must not raise
    assert ConnectionStatus.TOKEN_EXPIRED in controller.statuses


def test_401_auto_relogin_flags_token_expired(monkeypatch):
    """The automatic 401 path drives the shared _ReloginHandler: a token rejection
    on connect flags TOKEN_EXPIRED on the controller and runs the fresh login.

    Plan 08-02: the MENU re-login item now drives the PANEL login (a separate
    callback -- see test_relogin_menu_drives_panel_not_console); the AUTOMATIC 401
    path still routes through the shared _ReloginHandler exposed as
    result["relogin_handler"]."""
    _patch_build_app(monkeypatch)
    monkeypatch.setattr(app.token_store, "clear_token", lambda: None)

    calls = []

    def fake_get_token(*, force_relogin):
        calls.append(force_relogin)
        return "NEWTOKEN"

    # An injected connect that fails with an auth-equivalent error on the FIRST
    # attempt (routing through the shared re-login routine) and then, once the
    # shutdown event is set, raises StopSession so run_session ends (no infinite
    # loop). The event is created inside build_app, so capture it after build.
    state = {"attempts": 0, "event": None}

    def failing_connect(client):
        state["attempts"] += 1
        if state["event"] is not None and state["event"].is_set():
            raise app.mqtt_client.StopSession()
        raise RuntimeError("Connection Refused: not authorized.")

    result = app.build_app(
        "HEADER.eyJ1c2VybmFtZSI6InVfMSJ9.SIG",
        connect=failing_connect,
        sleep=lambda s: state["event"].set(),  # after one backoff, end the loop
        get_token=fake_get_token,
    )
    state["event"] = result["shutdown_event"]

    # The shared 401 routine IS a _ReloginHandler instance (the connect wrapper's
    # on_auth_fail), exposed for the automatic path.
    assert isinstance(result["relogin_handler"], app._ReloginHandler)

    # Drive the network runner. First connect raises an auth error; the wrapper
    # flags TOKEN_EXPIRED and runs the SAME routine. run_session then backs off
    # (sleep sets the shutdown event) and retries; the second attempt raises
    # StopSession so the loop ends cleanly.
    result["network_runner"]()

    # The shared routine ran the fresh login (force_relogin=True).
    assert calls and calls[0] is True
    # The auto-401 path flagged TOKEN_EXPIRED on the controller.
    assert result["controller"]._status is ConnectionStatus.TOKEN_EXPIRED


# --- Autostart toggle ("Met Windows opstarten") -----------------------------


class FakeAutostart:
    """In-memory stand-in for the autostart module: tracks the on/off state and
    records enable()/disable() calls (no real registry write)."""

    def __init__(self, enabled=False):
        self.enabled = enabled
        self.enable_calls = 0
        self.disable_calls = 0

    def is_enabled(self):
        return self.enabled

    def enable(self):
        self.enable_calls += 1
        self.enabled = True

    def disable(self):
        self.disable_calls += 1
        self.enabled = False


def test_autostart_label_is_locked():
    assert app.AUTOSTART_LABEL == "Met Windows opstarten"


def test_autostart_toggle_enables_when_disabled():
    fake = FakeAutostart(enabled=False)
    toggle = app.make_autostart_toggle(fake)
    toggle(icon=None, item=None)
    assert fake.enable_calls == 1
    assert fake.disable_calls == 0
    assert fake.enabled is True


def test_autostart_toggle_disables_when_enabled():
    fake = FakeAutostart(enabled=True)
    toggle = app.make_autostart_toggle(fake)
    toggle(icon=None, item=None)
    assert fake.disable_calls == 1
    assert fake.enable_calls == 0
    assert fake.enabled is False


def test_menu_has_checkable_autostart_item_reflecting_state(monkeypatch):
    """build_app adds a 'Met Windows opstarten' item whose checked= lambda reads
    the injected autostart module's is_enabled()."""
    captured = _patch_build_app(monkeypatch)
    fake = FakeAutostart(enabled=True)

    app.build_app(
        "HEADER.eyJ1c2VybmFtZSI6InVfMSJ9.SIG", autostart_mod=fake
    )

    labels = [text for text, _ in captured["menu_items"]]
    assert app.AUTOSTART_LABEL in labels

    autostart_item = next(
        m for m in captured["menu_objs"] if m.text == app.AUTOSTART_LABEL
    )
    # The check state mirrors the live autostart state.
    assert autostart_item.checked is not None
    assert autostart_item.checked(autostart_item) is True
    fake.enabled = False
    assert autostart_item.checked(autostart_item) is False

    # Clicking the item flips the state through the injected module.
    autostart_item.action(icon=None, item=autostart_item)
    assert fake.enabled is True  # was False -> enable() ran


# --- Serial persistence -----------------------------------------------------


def test_persist_serial_saves_only_serial_no_token():
    """persist_serial writes the picked serial via settings, preserving region,
    and never includes a token key."""
    saved = {}

    class FakeSettings:
        @staticmethod
        def load_settings():
            return {"region": "eu", "serial": None}

        @staticmethod
        def save_settings(s):
            saved.update(s)

    app.persist_serial("SER123", settings=FakeSettings)

    assert saved["serial"] == "SER123"
    assert saved["region"] == "eu"  # existing region preserved
    assert "access_token" not in saved
    assert "token" not in saved
    assert "password" not in saved


def test_build_app_persists_picked_serial(monkeypatch):
    """build_app persists the serial chosen by pick_serial via settings (no token
    key in the saved payload)."""
    _patch_build_app(monkeypatch)
    saved = {}
    monkeypatch.setattr(
        app.settings_module, "load_settings", lambda: {"region": "global", "serial": None}
    )
    monkeypatch.setattr(app.settings_module, "save_settings", lambda s: saved.update(s))

    app.build_app("HEADER.eyJ1c2VybmFtZSI6InVfMSJ9.SIG")

    assert saved.get("serial") == "SER"  # the pick_serial value from the fake
    assert "access_token" not in saved and "token" not in saved


# --- Single-instance guard in main() ----------------------------------------


def test_main_exits_cleanly_when_already_running(monkeypatch, capsys):
    """If the single-instance guard reports another instance, main() returns 0
    WITHOUT building the app or prompting for credentials (T-04-06)."""

    class AlreadyRunningGuard:
        def acquire(self):
            return False  # another instance owns the mutex

    built = []
    monkeypatch.setattr(app, "build_app", lambda *a, **k: built.append(True))
    # input() / getpass must never be reached on the already-running path.
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("prompted")))

    rc = app.main(argv=[], guard=AlreadyRunningGuard())

    assert rc == 0
    assert built == []  # the app was never built; no tray/MQTT touched


# --- Plan 07-03: v2 flyout/bridge wiring + threading inversion ---------------


class RecordingFlyout:
    """Stand-in for FlyoutWindow recording the call ORDER of every method so the
    destroy-FIRST quit ordering and the show-path theme push are assertable."""

    def __init__(self):
        self.events = []
        self.visible = False
        self.loaded_callbacks = []

    def hide(self):
        self.events.append("hide")
        self.visible = False

    def show(self):
        self.events.append("show")
        self.visible = True

    def toggle(self):
        self.events.append("toggle")
        self.visible = not self.visible

    def destroy(self):
        self.events.append("destroy")

    def resize_to(self, height):
        self.events.append(("resize_to", height))

    def push_theme(self, theme):
        self.events.append(("push_theme", theme))

    def on_loaded(self, callback):
        # Record + store the DOM-loaded callback so tests can fire it to simulate
        # pywebview's window.events.loaded after webview.start().
        self.events.append("on_loaded")
        self.loaded_callbacks.append(callback)

    def fire_loaded(self):
        for cb in list(self.loaded_callbacks):
            cb()


def test_quit_handler_destroys_flyout_before_icon_stop():
    """The Afsluiten handler destroys the flyout BEFORE icon.stop() -- the locked
    deadlock-safe order (webview.start() must return before the tray is torn
    down)."""
    order = []

    class OrderIcon(FakeIcon):
        def stop(self):
            order.append("icon.stop")
            super().stop()

    class OrderFlyout(RecordingFlyout):
        def destroy(self):
            order.append("flyout.destroy")
            super().destroy()

    client = FakeClient()
    event = threading.Event()
    flyout = OrderFlyout()
    icon = OrderIcon()

    handler = app.make_quit_handler(client, event, flyout=flyout)
    handler(icon, item=None)

    assert order.index("flyout.destroy") < order.index("icon.stop")
    assert event.is_set()
    assert client.disconnect_count == 1
    assert icon.stop_count == 1


def test_quit_handler_stops_icon_even_if_flyout_destroy_raises():
    """A destroy() failure must NOT prevent icon.stop() (no orphan; T-07-06)."""

    class RaisingFlyout(RecordingFlyout):
        def destroy(self):
            raise RuntimeError("window already gone")

    event = threading.Event()
    icon = FakeIcon()

    handler = app.make_quit_handler(FakeClient(), event, flyout=RaisingFlyout())
    handler(icon, item=None)

    assert icon.stop_count == 1  # icon still disposed despite a destroy error


def test_flyout_toggle_pushes_theme_on_show_only(monkeypatch):
    """make_flyout_toggle pushes the live theme on the SHOW path (FLY-02) and
    then toggles; hiding pushes no theme."""
    monkeypatch.setattr(app.render, "detect_windows_theme", lambda: "dark")
    flyout = RecordingFlyout()
    toggle = app.make_flyout_toggle(flyout)

    toggle()  # hidden -> show: pushes theme THEN toggles
    assert flyout.events == [("push_theme", "dark"), "toggle"]

    flyout.events.clear()
    toggle()  # now visible -> hide: no theme push
    assert flyout.events == ["toggle"]


def test_flyout_toggle_pushes_current_state_on_show(monkeypatch):
    """On the SHOW path make_flyout_toggle re-pushes the CURRENT serialized state
    (so a freshly opened panel is immediately correct, not stale defaults): it
    pushes the theme FIRST, then the state, then toggles. Hiding pushes neither
    theme nor state (Plan 09-01 Task 2)."""
    monkeypatch.setattr(app.render, "detect_windows_theme", lambda: "dark")
    flyout = RecordingPushFlyout(visible=False)
    state = PrintState()
    state.mc_percent = 73
    state.gcode_state = "RUNNING"
    state.mc_remaining_time = 5

    toggle = app.make_flyout_toggle(flyout, state=state, logged_in=True)

    toggle()  # hidden -> SHOW: push theme, push state, toggle
    assert flyout.themes == ["dark"]
    assert len(flyout.pushes) == 1
    pushed = flyout.pushes[0]
    assert pushed["pct"] == 73
    assert pushed["status"] == "printing"
    assert pushed["etaLabel"] == "nog 5 min"
    # Ordering: theme pushed BEFORE state, both BEFORE the toggle.
    assert flyout.events.index(("push_theme", "dark")) < flyout.events.index("toggle")

    # Now visible -> HIDE: no theme push, no state push.
    flyout.events.clear()
    flyout.themes.clear()
    flyout.pushes.clear()
    toggle()
    assert flyout.events == ["toggle"]
    assert flyout.themes == []
    assert flyout.pushes == []


def test_flyout_toggle_logged_out_show_does_not_push_state(monkeypatch):
    """When LOGGED OUT, the SHOW path pushes the theme but NOT a serialized state:
    a logged-out re-push carries auth_step="login" and would clobber whichever
    auth screen the page is on (e.g. reopening after stepping away to fetch the
    2FA code must keep the code screen, not snap back to login)."""
    monkeypatch.setattr(app.render, "detect_windows_theme", lambda: "dark")
    flyout = RecordingPushFlyout(visible=False)
    state = PrintState()

    toggle = app.make_flyout_toggle(flyout, state=state, logged_in=False)

    toggle()  # hidden -> SHOW
    assert flyout.themes == ["dark"]   # theme still pushed
    assert flyout.pushes == []          # but NO state push -> page keeps its screen
    assert "toggle" in flyout.events


def test_flyout_toggle_without_state_pushes_theme_only(monkeypatch):
    """Backward-compatible: with no state wired the SHOW path still pushes the
    theme and toggles, and never attempts a state push (v1 behavior preserved)."""
    monkeypatch.setattr(app.render, "detect_windows_theme", lambda: "light")
    flyout = RecordingPushFlyout(visible=False)
    toggle = app.make_flyout_toggle(flyout)  # no state

    toggle()
    assert flyout.themes == ["light"]
    assert flyout.pushes == []
    assert "toggle" in flyout.events


def test_build_app_wires_flyout_bridge_and_toggle(monkeypatch):
    """build_app exposes a flyout + Api + bound toggle; the Api's hide handler
    calls flyout.hide and the toggle calls flyout.toggle (all via the FakeWebview
    -- no real GUI)."""
    _patch_build_app(monkeypatch)

    result = app.build_app("HEADER.eyJ1c2VybmFtZSI6InVfMSJ9.SIG")

    assert result["flyout"] is not None
    assert result["api"] is not None
    assert callable(result["flyout_toggle"])

    # The Api.hide action forwards to the real flyout.hide (the window is a
    # FakeWindow built by the injected FakeWebview).
    result["api"].hide()
    assert result["flyout"].visible is False
    # get_initial_state returns a non-secret state dict seeded from live state.
    initial = result["api"].get_initial_state()
    assert isinstance(initial, dict)
    assert "loggedIn" in initial and "theme" in initial


def test_build_app_creates_one_hidden_flyout_window(monkeypatch):
    """build_app creates exactly ONE hidden frameless window via webview."""
    captured = _patch_build_app(monkeypatch)

    app.build_app("HEADER.eyJ1c2VybmFtZSI6InVfMSJ9.SIG")

    fake_webview = captured["webview"]
    assert len(fake_webview.created) == 1
    _title, kwargs = fake_webview.created[0]
    assert kwargs.get("hidden") is True
    assert kwargs.get("frameless") is True


def test_get_initial_state_contains_no_secret(monkeypatch):
    """The bridge state pushed to the page never carries a token/password/email
    (T-07-02)."""
    _patch_build_app(monkeypatch)
    token = "HEADER.eyJ1c2VybmFtZSI6InVfMSJ9.SUPERSECRETSIG"

    result = app.build_app(token, email="user@example.com", password="hunter2")
    initial = result["api"].get_initial_state()

    blob = json.dumps(initial)
    assert "SUPERSECRETSIG" not in blob
    assert token not in blob
    assert "hunter2" not in blob
    assert "user@example.com" not in blob


def test_control_action_never_logs_the_command_payload(monkeypatch, caplog):
    """The real control handler (Plan 09-02) never logs the command value from the
    app logger (T-09-07). publish_command is patched out so no broker is touched;
    the app-side handler must keep the command string out of its own logs."""
    import logging

    _patch_build_app(monkeypatch)
    # Patch the publisher so no real client.publish runs; the point of THIS test is
    # the app handler's logging, not control.py (covered separately).
    monkeypatch.setattr(app.control, "publish_command", lambda *a, **k: None)
    result = app.build_app("HEADER.eyJ1c2VybmFtZSI6InVfMSJ9.SIG")
    api = result["api"]

    with caplog.at_level(logging.DEBUG, logger="app"):
        api.control("pause")

    # The app-side handler must NEVER write the command value into its logs.
    app_records = [r.message for r in caplog.records if r.name == "app"]
    assert all("pause" not in m for m in app_records)


def test_login_submit_handler_forwards_to_session(monkeypatch):
    """build_app's bridge login_submit forwards to the real SessionController
    (which drives auth.login on a worker) -- it is no longer a name-only stub.
    The controller's worker runs inline here via a patched threading.Thread."""
    _patch_build_app(monkeypatch)

    login_calls = []
    monkeypatch.setattr(
        app.auth, "login", lambda e, p: login_calls.append((e, p)) or {"loginType": "verifyCode"}
    )
    monkeypatch.setattr(app.auth, "request_email_code", lambda e: None)

    # Run the session worker bodies inline (no real daemon thread): patch the
    # threading.Thread that src.session._default_run_async uses so .start() runs
    # the target synchronously.
    import src.session as session_mod

    class InlineThread:
        def __init__(self, target=None, daemon=None):
            self._target = target

        def start(self):
            if self._target is not None:
                self._target()

    monkeypatch.setattr(session_mod.threading, "Thread", InlineThread)

    result = app.build_app("HEADER.eyJ1c2VybmFtZSI6InVfMSJ9.SIG")
    result["api"].login_submit("me@example.com", "pw")

    assert login_calls == [("me@example.com", "pw")]


class _BootstrapSession:
    """A FakeSession that also records bootstrap_from_stored + show_login, used by
    the main()/bootstrap tests. ``bootstrap_result`` decides logged-in vs login."""

    def __init__(self, bootstrap_result=False):
        self.calls = []
        self.bootstrap_result = bootstrap_result

    def bootstrap_from_stored(self):
        self.calls.append("bootstrap_from_stored")
        return self.bootstrap_result

    def logout(self):
        self.calls.append("logout")


def _fake_gui(*, icon, flyout, session, controller=None):
    """Build the dict build_gui returns, for stubbing it in main() tests."""
    return {
        "icon": icon,
        "controller": controller if controller is not None else object(),
        "state": PrintState(),
        "shutdown_event": threading.Event(),
        "flyout": flyout,
        "api": object(),
        "flyout_toggle": lambda: None,
        "session": session,
        "start_mqtt": (lambda token, serial: None),
        "stop_session": (lambda: None),
        "relogin_handler": object(),
    }


def test_main_inverts_threading_tray_detached_and_webview_start(monkeypatch):
    """main() runs the tray via run_detached (NOT on the main thread) and calls
    webview.start() on the main thread -- the v2 threading inversion, driven end
    to end with fakes (no real GUI/broker/login). Plan 08-02: main() builds the
    token-less GUI via build_gui (NO console login) and bootstraps from stored."""

    class OkGuard:
        def acquire(self):
            return True

    fake_icon = FakeIcon()
    fake_webview = FakeWebview()
    flyout = RecordingFlyout()
    session = _BootstrapSession(bootstrap_result=False)

    monkeypatch.setattr(
        app.settings_module, "load_settings", lambda: {"region": "global", "serial": None}
    )

    def fake_build_gui(*, webview=None, **kwargs):
        # main() must pass the injected webview through to build_gui.
        assert webview is fake_webview
        return _fake_gui(icon=fake_icon, flyout=flyout, session=session)

    monkeypatch.setattr(app, "build_gui", fake_build_gui)
    # make_setup is exercised elsewhere; neutralize it so run_detached gets a noop.
    monkeypatch.setattr(app, "make_setup", lambda *a, **k: (lambda icon: None))

    rc = app.main(argv=[], guard=OkGuard(), webview=fake_webview)

    assert rc == 0
    # Tray ran DETACHED (its own thread), never on the main thread via icon.run().
    assert fake_icon.run_detached_calls == 1
    assert fake_icon.run_count == 0
    # The MAIN thread entered the GUI loop.
    assert fake_webview.start_calls == 1
    # Bootstrap is DEFERRED to the DOM-loaded event: it must NOT have run before
    # webview.start() (that ordering caused "Main window failed to start").
    assert "bootstrap_from_stored" not in session.calls
    assert "on_loaded" in flyout.events
    # Firing the loaded event (pywebview's window.events.loaded after start)
    # bootstraps from the stored token instead of prompting the console.
    flyout.fire_loaded()
    assert "bootstrap_from_stored" in session.calls


def test_main_has_no_console_login():
    """app.main's source contains NO console-login call: no input(...) and no
    getpass(...) for credentials (T-08-06 -- enforced via inspect.getsource)."""
    import inspect

    src = inspect.getsource(app.main)
    assert "input(" not in src
    assert "getpass" not in src


def test_main_logged_out_start_sets_neutral_glyph_and_does_not_show_flyout(monkeypatch):
    """With no stored token, bootstrap returns False; main() leaves the LOGGED-OUT
    neutral glyph on the icon and does NOT auto-show the flyout (the app lives
    silently in the tray; the user opens the panel by clicking the tray icon)."""

    class OkGuard:
        def acquire(self):
            return True

    fake_icon = FakeIcon()
    fake_webview = FakeWebview()
    flyout = RecordingFlyout()
    session = _BootstrapSession(bootstrap_result=False)

    neutral = object()
    # main() paints the neutral logged-out PRINTER glyph at startup.
    monkeypatch.setattr(app.render, "render_printer_icon", lambda *a, **k: neutral)
    monkeypatch.setattr(
        app.settings_module, "load_settings", lambda: {"region": "global", "serial": None}
    )
    monkeypatch.setattr(app, "build_gui", lambda **k: _fake_gui(icon=fake_icon, flyout=flyout, session=session))
    monkeypatch.setattr(app, "make_setup", lambda *a, **k: (lambda icon: None))

    rc = app.main(argv=[], guard=OkGuard(), webview=fake_webview)

    assert rc == 0
    # The icon shows the neutral logged-out glyph. This is set before start (it
    # does NOT touch the web page, so it is safe pre-start).
    assert fake_icon.icon is neutral
    # bootstrap is DEFERRED to the DOM-loaded event (pushing before webview.start()
    # raised "Main window failed to start").
    assert "show" not in flyout.events
    assert "on_loaded" in flyout.events
    # Firing the loaded event runs bootstrap but must NOT auto-show the flyout --
    # the app stays in the tray until the user clicks the icon.
    flyout.fire_loaded()
    assert "show" not in flyout.events
    assert "bootstrap_from_stored" in session.calls


def test_relogin_menu_drives_panel_not_console(monkeypatch):
    """The 'Opnieuw verbinden / inloggen' menu callback drives the PANEL login:
    it resets the panel to the login step (push_auth_step('login')) + shows the
    flyout and NEVER calls getpass/input/spike.get_access_token (T-08-06/07)."""

    class PanelFlyout(RecordingFlyout):
        def __init__(self):
            super().__init__()
            self.auth_steps = []

        def push_auth_step(self, step):
            self.auth_steps.append(step)

    flyout = PanelFlyout()
    session = _BootstrapSession()
    # A relogin_handler whose controller flags TOKEN_EXPIRED.
    relogin_handler = app.make_relogin_handler(
        None, threading.Event(),
        get_token=lambda *, force_relogin: (_ for _ in ()).throw(AssertionError("console login")),
        controller=FakeController(),
    )

    callback = app.make_panel_relogin(session, flyout, relogin_handler)
    callback(icon=None, item=None)

    # The panel was reset to the login step and shown.
    assert "login" in flyout.auth_steps
    assert "show" in flyout.events
    # The session was logged out (token cleared + session stopped).
    assert "logout" in session.calls
    # TOKEN_EXPIRED flagged for the tray.
    assert ConnectionStatus.TOKEN_EXPIRED in relogin_handler._controller.statuses


# --- Plan 08-02: SessionController wiring + deferred MQTT start --------------


class FakeSession:
    """Stand-in for src.session.SessionController exposing the five page actions
    plus bootstrap_from_stored, each recording that it was called. Used to assert
    the bridge handlers forward to the controller's bound methods (identity)."""

    def __init__(self):
        self.calls = []
        self.bootstrap_result = True

    def login_submit(self, email, password):
        self.calls.append(("login_submit", email, password))

    def submit_code(self, code):
        self.calls.append(("submit_code", code))

    def resend_code(self):
        self.calls.append(("resend_code",))

    def select_printer(self, device_id):
        self.calls.append(("select_printer", device_id))

    def open_printer_select(self):
        self.calls.append(("open_printer_select",))

    def logout(self):
        self.calls.append(("logout",))

    def bootstrap_from_stored(self):
        self.calls.append(("bootstrap_from_stored",))
        return self.bootstrap_result


def test_make_bridge_handlers_wires_session_methods():
    """The five auth/select actions forward to the SessionController's bound
    methods (calling the handler calls the controller), 'hide' stays flyout.hide,
    and 'control' stays a stub."""
    flyout = RecordingFlyout()
    session = FakeSession()

    handlers = app.make_bridge_handlers(flyout, session)

    # __self__ identity proves each handler is the SAME controller's bound method
    # (a fresh bound-method object is created per attribute access, so `is`
    # against session.login_submit would spuriously fail).
    assert handlers["login_submit"].__self__ is session
    assert handlers["login_submit"].__func__ is type(session).login_submit
    assert handlers["submit_code"].__self__ is session
    assert handlers["resend_code"].__self__ is session
    assert handlers["select_printer"].__self__ is session
    assert handlers["open_printer_select"].__self__ is session
    assert handlers["logout"].__self__ is session
    assert handlers["hide"].__self__ is flyout  # flyout.hide bound method
    assert handlers["resize"].__self__ is flyout  # flyout.resize_to bound method

    # Forwarding: calling the bridge handler drives the controller method.
    handlers["login_submit"]("me@example.com", "pw")
    handlers["submit_code"]("123456")
    handlers["select_printer"]("DEV1")
    handlers["logout"]()
    assert ("login_submit", "me@example.com", "pw") in session.calls
    assert ("submit_code", "123456") in session.calls
    assert ("select_printer", "DEV1") in session.calls
    assert ("logout",) in session.calls

    # 'control' is now a REAL handler (Phase 9 wired it to control.publish_command),
    # not a SessionController method and not a name-only stub.
    assert handlers["control"] is not getattr(session, "control", None)
    assert callable(handlers["control"])


def test_stub_actions_empty_after_control_wired():
    """No page action is a name-only stub anymore: Phase 9 replaced the last
    'control' stub with a real handler that publishes pause/resume/stop."""
    assert app._STUB_ACTIONS == ()


# --- Plan 09-02: Api.control -> control.publish_command wiring -------------- #


class _FakeControlModule:
    """A fake stand-in for src.control: records publish_command(client, serial,
    command) calls and enforces the SAME closed allowlist so an out-of-allowlist
    command raises (proving the handler never bypasses validation -- T-09-05)."""

    _ALLOWED = ("pause", "resume", "stop")

    def __init__(self):
        self.calls = []

    def publish_command(self, client, serial, command):
        if command not in self._ALLOWED:
            raise ValueError(f"Unsupported control command: {command!r}")
        self.calls.append((client, serial, command))


def _control_handler_with(start_mqtt_attrs):
    """Build a control handler over a stand-in start_mqtt carrying the given
    attributes (client/serial), with an injected fake control module. Returns
    (handler, fake_control)."""

    class _StartMqtt:
        pass

    start_mqtt = _StartMqtt()
    for name, value in start_mqtt_attrs.items():
        setattr(start_mqtt, name, value)
    fake_control = _FakeControlModule()
    handler = app.make_control_handler(start_mqtt, control=fake_control)
    return handler, fake_control


def test_control_handler_publishes_each_allowlisted_command():
    """Driving the control handler with pause/resume/stop (after a session set
    client+serial) calls control.publish_command(client, serial, command) for
    each -- the panel button -> publish_command mapping (PANEL-02)."""
    client = object()
    for command in ("pause", "resume", "stop"):
        handler, fake_control = _control_handler_with(
            {"client": client, "serial": "SER-OWN"}
        )
        handler(command)
        assert fake_control.calls == [(client, "SER-OWN", command)]


def test_control_handler_targets_the_captured_active_serial():
    """The handler targets the user's OWN selected serial -- the one captured
    when start_mqtt ran (T-09-06), never a foreign/broadcast serial."""
    client = object()
    handler, fake_control = _control_handler_with(
        {"client": client, "serial": "MY-PRINTER"}
    )
    handler("pause")
    assert fake_control.calls == [(client, "MY-PRINTER", "pause")]


def test_control_handler_rejects_out_of_allowlist_command(caplog):
    """An out-of-allowlist command ('home'/'gcode') never reaches the broker:
    publish_command is never recorded with it and the bridge thread does not
    crash (T-09-05). The command value is never logged."""
    import logging

    client = object()
    handler, fake_control = _control_handler_with(
        {"client": client, "serial": "SER-OWN"}
    )
    with caplog.at_level(logging.DEBUG, logger="app"):
        # Must not raise out of the handler (keep the bridge thread alive)...
        handler("home")
        handler("gcode")
    # ...and must NOT have published either arbitrary command.
    assert fake_control.calls == []
    # The arbitrary command value is never logged.
    assert "home" not in caplog.text
    assert "gcode" not in caplog.text


def test_control_handler_is_noop_with_no_session():
    """With no session started (client/serial still None) the control handler is
    a safe no-op: publish_command is never called and nothing is raised."""
    handler, fake_control = _control_handler_with({"client": None, "serial": None})
    assert handler("pause") is None
    assert fake_control.calls == []

    # Also a no-op when the attributes are entirely absent (defensive getattr).
    class _Bare:
        pass

    fake_control2 = _FakeControlModule()
    bare_handler = app.make_control_handler(_Bare(), control=fake_control2)
    assert bare_handler("stop") is None
    assert fake_control2.calls == []


# --- control-command rejection surfacing (firmware-blocked pause/resume) ----


class _FakeMsg:
    def __init__(self, payload):
        self.payload = payload if isinstance(payload, bytes) else payload.encode()


def test_control_rejection_error_flags_rejected_command():
    """A control-command echo with a non-zero err_code yields the user error so a
    firmware-blocked pause/resume is not a silent no-op."""
    msg = _FakeMsg(json.dumps({"print": {"command": "pause", "err_code": 84033543}}))
    assert app._control_rejection_error(msg) == app._ERR_CONTROL_REJECTED


def test_control_rejection_error_none_for_accepted_or_normal_reports():
    """No error for a control echo with err_code 0/absent, a normal status
    report, or malformed input (guarded -- never raises)."""
    assert app._control_rejection_error(
        _FakeMsg(json.dumps({"print": {"command": "pause", "err_code": 0}}))
    ) is None
    assert app._control_rejection_error(
        _FakeMsg(json.dumps({"print": {"command": "resume"}}))
    ) is None
    assert app._control_rejection_error(
        _FakeMsg(json.dumps({"print": {"gcode_state": "RUNNING", "mc_percent": 42}}))
    ) is None
    assert app._control_rejection_error(_FakeMsg(b"not json")) is None


def test_make_start_mqtt_captures_active_serial(monkeypatch):
    """When start_mqtt runs it stores the serial it was started with on
    start_mqtt.serial (alongside start_mqtt.client) so control targets the
    user's OWN device (T-09-06)."""
    gui, _ = _build_gui(monkeypatch)
    start_mqtt = gui["start_mqtt"]

    start_mqtt("TOKEN", "SER-ACTIVE")

    assert start_mqtt.serial == "SER-ACTIVE"
    assert start_mqtt.client is not None


def test_build_app_eager_path_captures_active_serial(monkeypatch):
    """The eager build_app path also records the derived serial on
    start_mqtt.serial so control works without a deferred start (mirrors how
    start_mqtt.client is set eagerly)."""
    _patch_build_app(monkeypatch)
    result = app.build_app("HEADER.eyJ1c2VybmFtZSI6InVfMSJ9.SIG")
    start_mqtt = result["start_mqtt"]
    assert start_mqtt.serial is not None
    assert start_mqtt.serial == start_mqtt.client._serial if hasattr(
        start_mqtt.client, "_serial"
    ) else start_mqtt.serial is not None


def test_bridge_control_handler_publishes_via_build_app(monkeypatch):
    """End-to-end through the bridge: api.control('pause') on a built app calls
    control.publish_command with the captured active client+serial. The control
    module is patched so no real broker is touched."""
    _patch_build_app(monkeypatch)

    published = []
    monkeypatch.setattr(
        app.control,
        "publish_command",
        lambda client, serial, command: published.append((serial, command)),
    )

    result = app.build_app("HEADER.eyJ1c2VybmFtZSI6InVfMSJ9.SIG")
    api = result["api"]
    start_mqtt = result["start_mqtt"]

    api.control("pause")

    assert published == [(start_mqtt.serial, "pause")]


def test_make_session_builds_real_controller():
    """make_session constructs a real src.session.SessionController with the
    injected start_mqtt / stop_session hooks and the real auth/token_store/
    settings modules."""
    from src.session import SessionController

    flyout = RecordingFlyout()
    started = []
    stopped = []

    session = app.make_session(
        flyout,
        start_mqtt=lambda token, serial: started.append((token, serial)),
        stop_session=lambda: stopped.append(True),
        run_async=lambda fn: fn(),  # run worker bodies inline for the test
    )

    assert isinstance(session, SessionController)
    assert session.flyout is flyout


def _build_gui(monkeypatch, **kwargs):
    """Build the token-less GUI seam with the test fakes already patched in.

    Patches threading.Thread to a recording fake (so no real network thread
    runs), then calls app.build_gui with the FakeWebview. Returns
    (gui_dict, thread_starts_list)."""
    _patch_build_app(monkeypatch)
    monkeypatch.setattr(app.auth, "get_device_list", lambda t: [{"dev_id": "SER"}])

    thread_starts = []

    class FakeThread:
        def __init__(self, target=None, name=None, daemon=None):
            self.target = target
            self.name = name
            self.daemon = daemon

        def start(self):
            thread_starts.append(self.name)
            if self.target is not None:
                # Do NOT actually run the network loop (it would block); the
                # name record is enough to assert the start-once guard.
                pass

    monkeypatch.setattr(app.threading, "Thread", FakeThread)

    gui = app.build_gui(webview=FakeWebview(), **kwargs)
    return gui, thread_starts


def test_start_mqtt_hook_starts_network_thread_at_most_once(monkeypatch):
    """The deferred start_mqtt hook starts the MQTT network thread EXACTLY ONCE
    across repeated calls (idempotent guard -- T-08-09)."""
    gui, thread_starts = _build_gui(monkeypatch)
    start_mqtt = gui["start_mqtt"]

    start_mqtt("TOKEN", "SER")
    start_mqtt("TOKEN", "SER")
    start_mqtt("TOKEN", "SER")

    # Only ONE network thread was ever started despite three calls.
    assert thread_starts.count("mqtt-network") == 1


def test_relogin_after_logout_starts_a_fresh_session(monkeypatch):
    """After stop_session (logout), a later start_mqtt starts a NEW session: a
    new client, a new (cleared) session_stop, and a second network thread -- so
    re-login in the same process reconnects (the old idempotent guard is reset)."""
    gui, thread_starts = _build_gui(monkeypatch)
    start_mqtt = gui["start_mqtt"]
    stop_session = gui["stop_session"]

    start_mqtt("T1", "S1")
    first_client = start_mqtt.client
    first_stop = start_mqtt.session_stop

    stop_session()  # logout
    start_mqtt("T2", "S2")  # re-login

    assert start_mqtt.started is True
    assert start_mqtt.client is not first_client
    assert start_mqtt.session_stop is not first_stop
    assert not start_mqtt.session_stop.is_set()
    assert thread_starts.count("mqtt-network") == 2


def test_start_mqtt_not_called_at_build_time(monkeypatch):
    """build_gui NEVER starts the network thread itself -- only the start_mqtt
    hook does, once a token + serial exist (deferred MQTT start)."""
    gui, thread_starts = _build_gui(monkeypatch)
    assert "mqtt-network" not in thread_starts  # nothing started by construction


def test_stop_session_ends_session_without_global_shutdown(monkeypatch):
    """stop_session ends ONLY the current MQTT session: it sets the per-session
    stop event, disconnects the live client, and resets ``started`` so a later
    login starts fresh -- WITHOUT setting the global shutdown event (which also
    drives the tray pump / quit), so logout leaves the app usable."""
    gui, _ = _build_gui(monkeypatch)
    start_mqtt = gui["start_mqtt"]
    stop_session = gui["stop_session"]

    start_mqtt("TOKEN", "SER")  # build the live client
    client = start_mqtt.client
    session_stop = start_mqtt.session_stop
    assert session_stop is not None and not session_stop.is_set()

    stop_session()

    # The session was told to stop and the client was disconnected (best-effort).
    assert session_stop.is_set()
    assert client.disconnect_count == 1
    # A later login may start a fresh session, and the GLOBAL shutdown is untouched.
    assert start_mqtt.started is False
    assert not gui["shutdown_event"].is_set()
    # The tray was reset to the neutral logged-out display (no stale print).
    assert gui["controller"]._status is app.ConnectionStatus.DISCONNECTED


# --- Phase 13-02 Task 3: update-check loop + daemon thread ------------------
#
# run_update_check is driven directly with fakes: a FakeUpdateController records
# signal_update_available, a FakeUpdateFlyout records push_update, a fake `check`
# returns a canned UpdateInfo / None, and a FakePrefsModule backs load/save with
# an in-memory dict. make_update_check's loop exit is proven with a pre-set
# shutdown event (no real waits).


class FakeUpdateController:
    """Records signal_update_available(version, url) calls (the one-time balloon)."""

    def __init__(self):
        self.signals = []

    def signal_update_available(self, version, url):
        self.signals.append((version, url))


class FakeUpdateFlyout:
    """Records push_update(info) calls (the banner / up-to-date feedback)."""

    def __init__(self):
        self.updates = []

    def push_update(self, info):
        self.updates.append(info)


class FakePrefsModule:
    """In-memory update_prefs stand-in: load returns the live dict, save replaces it."""

    def __init__(self, prefs=None):
        from src.update_prefs import DEFAULTS

        self._prefs = dict(DEFAULTS)
        if prefs:
            self._prefs.update(prefs)
        self.saves = 0

    def load_update_prefs(self):
        return dict(self._prefs)

    def save_update_prefs(self, prefs):
        self.saves += 1
        self._prefs = dict(prefs)


def _info(version="2.2.0", etag='W/"e"'):
    from src.updater import UpdateInfo

    return UpdateInfo(
        version=version,
        tag="v" + version,
        html_url="https://github.com/o/r/releases/tag/v" + version,
        asset_name=None,
        asset_url=None,
        asset_size=None,
        sha256_url=None,
        etag=etag,
    )


def test_run_update_check_auto_disabled_skips_check():
    ctrl = FakeUpdateController()
    flyout = FakeUpdateFlyout()
    prefs = FakePrefsModule({"auto_update_enabled": False})
    called = {"n": 0}

    def _check(*, current, etag):
        called["n"] += 1
        return _info()

    result = app.run_update_check(
        ctrl, flyout, check=_check, prefs_module=prefs, current="2.1.0"
    )

    assert result is None
    assert called["n"] == 0  # check was NOT run
    assert flyout.updates == []
    assert ctrl.signals == []


def test_run_update_check_force_bypasses_auto_disabled():
    ctrl = FakeUpdateController()
    flyout = FakeUpdateFlyout()
    prefs = FakePrefsModule({"auto_update_enabled": False})

    result = app.run_update_check(
        ctrl,
        flyout,
        check=lambda *, current, etag: _info(),
        prefs_module=prefs,
        current="2.1.0",
        force=True,
    )

    assert result is not None  # forced through despite auto disabled
    assert flyout.updates  # banner pushed


def test_run_update_check_none_persists_last_check_no_notify():
    ctrl = FakeUpdateController()
    flyout = FakeUpdateFlyout()
    prefs = FakePrefsModule()

    result = app.run_update_check(
        ctrl,
        flyout,
        check=lambda *, current, etag: None,
        prefs_module=prefs,
        current="2.1.0",
    )

    assert result is None
    assert flyout.updates == []  # no banner
    assert ctrl.signals == []  # no balloon
    assert prefs.saves == 1  # last_check still persisted
    assert prefs._prefs["last_check"] is not None


def test_run_update_check_banner_always_balloon_once():
    ctrl = FakeUpdateController()
    flyout = FakeUpdateFlyout()
    prefs = FakePrefsModule()

    # First pass: banner + balloon, last_notified_version persisted.
    app.run_update_check(
        ctrl, flyout, check=lambda *, current, etag: _info("2.2.0"),
        prefs_module=prefs, current="2.1.0",
    )
    assert flyout.updates == [{"version": "2.2.0", "html_url": _info("2.2.0").html_url}]
    assert ctrl.signals == [("2.2.0", _info("2.2.0").html_url)]
    assert prefs._prefs["last_notified_version"] == "2.2.0"

    # Second pass, SAME version: banner AGAIN, but NO second balloon.
    app.run_update_check(
        ctrl, flyout, check=lambda *, current, etag: _info("2.2.0"),
        prefs_module=prefs, current="2.1.0",
    )
    assert len(flyout.updates) == 2  # banner pushed again
    assert len(ctrl.signals) == 1  # balloon NOT fired again


def test_run_update_check_skipped_version_suppressed():
    ctrl = FakeUpdateController()
    flyout = FakeUpdateFlyout()
    prefs = FakePrefsModule({"skipped_version": "2.2.0"})

    result = app.run_update_check(
        ctrl, flyout, check=lambda *, current, etag: _info("2.2.0"),
        prefs_module=prefs, current="2.1.0",
    )

    assert result is not None  # info returned (for the manual-check caller)
    assert flyout.updates == []  # NO banner
    assert ctrl.signals == []  # NO balloon
    assert prefs.saves == 1  # last_check/etag still persisted


def test_run_update_check_persists_etag_from_result():
    ctrl = FakeUpdateController()
    flyout = FakeUpdateFlyout()
    prefs = FakePrefsModule()

    captured = {}

    def _check(*, current, etag):
        captured["etag_in"] = etag
        return _info("2.2.0", etag='W/"fresh"')

    app.run_update_check(
        ctrl, flyout, check=_check, prefs_module=prefs, current="2.1.0"
    )

    assert captured["etag_in"] is None  # first call, no cached etag
    assert prefs._prefs["etag"] == 'W/"fresh"'  # fresh etag persisted


def test_make_update_check_loop_exits_immediately_when_preset():
    ctrl = FakeUpdateController()
    flyout = FakeUpdateFlyout()
    prefs = FakePrefsModule()
    shutdown = threading.Event()
    shutdown.set()  # pre-set: the startup-delay wait returns True -> loop returns

    runs = {"n": 0}

    def _check(*, current, etag):
        runs["n"] += 1
        return None

    loop = app.make_update_check(
        ctrl, flyout, shutdown, check=_check, prefs_module=prefs,
        interval=0.01, startup_delay=0.01,
    )
    loop()  # returns immediately because the startup-delay wait sees the set event

    assert runs["n"] == 0  # no check ran


# --- Phase 13-03 Task 2: the five update bridge handlers --------------------


class FakeBrowser:
    """Records browser.open(url) calls."""

    def __init__(self):
        self.opened = []

    def open(self, url):
        self.opened.append(url)


def test_open_release_page_opens_browser():
    flyout = FakeUpdateFlyout()
    browser = FakeBrowser()
    handlers = app.make_update_handlers(
        lambda: FakeUpdateController(), flyout,
        prefs_module=FakePrefsModule(), browser=browser,
    )
    handlers["open_release_page"]("https://github.com/o/r/releases/tag/v2.2.0")
    assert browser.opened == ["https://github.com/o/r/releases/tag/v2.2.0"]


def test_open_release_page_empty_url_is_noop():
    browser = FakeBrowser()
    handlers = app.make_update_handlers(
        lambda: FakeUpdateController(), FakeUpdateFlyout(),
        prefs_module=FakePrefsModule(), browser=browser,
    )
    handlers["open_release_page"]("")  # falsy -> no launch
    handlers["open_release_page"](None)
    assert browser.opened == []


def test_skip_update_version_persists():
    prefs = FakePrefsModule()
    handlers = app.make_update_handlers(
        lambda: FakeUpdateController(), FakeUpdateFlyout(),
        prefs_module=prefs, browser=FakeBrowser(),
    )
    handlers["skip_update_version"]("2.4.0")
    assert prefs._prefs["skipped_version"] == "2.4.0"
    assert prefs.saves == 1


def test_set_auto_update_persists_bool():
    prefs = FakePrefsModule()
    handlers = app.make_update_handlers(
        lambda: FakeUpdateController(), FakeUpdateFlyout(),
        prefs_module=prefs, browser=FakeBrowser(),
    )
    handlers["set_auto_update"](0)  # truthiness coerced to bool
    assert prefs._prefs["auto_update_enabled"] is False
    handlers["set_auto_update"](1)
    assert prefs._prefs["auto_update_enabled"] is True


def test_dismiss_update_is_noop_returns_none():
    handlers = app.make_update_handlers(
        lambda: FakeUpdateController(), FakeUpdateFlyout(),
        prefs_module=FakePrefsModule(), browser=FakeBrowser(),
    )
    assert handlers["dismiss_update"]() is None


def test_check_for_update_now_uptodate_pushes_inline_status():
    """When the forced check returns None (up to date), the handler pushes the
    inline {upToDate: True} status to the page (no balloon, no pop-up)."""
    flyout = FakeUpdateFlyout()
    # No newer release -> run_update_check returns None.
    prefs = FakePrefsModule()
    handlers = app.make_update_handlers(
        lambda: FakeUpdateController(), flyout,
        prefs_module=prefs, browser=FakeBrowser(),
    )
    # Monkeypatch run_update_check indirectly: use a check returning None via the
    # real run_update_check path. make_update_handlers calls app.run_update_check
    # with force=True and the injected prefs_module; we drive it through the real
    # check_for_update by patching the module-level default is overkill -- instead
    # rely on run_update_check using check_for_update default, which would hit the
    # network. To keep it offline, we assert via a controller resolved at call time
    # and a None-returning check is exercised in the dedicated test below.
    # Here we verify the up-to-date push by forcing skipped/no-newer through prefs.
    # Simplest: call the handler and confirm an {upToDate:True} push when info None.
    import src.app as app_mod

    orig = app_mod.run_update_check
    try:
        app_mod.run_update_check = lambda controller, fl, **kw: None
        handlers["check_for_update_now"]()
    finally:
        app_mod.run_update_check = orig

    assert {"upToDate": True} in flyout.updates


def test_check_for_update_now_resolves_controller_at_call_time():
    """make_update_handlers must resolve the controller via the late-bind holder
    AT CALL TIME -- it is None at build time and only filled in before the call."""
    holder = {"controller": None}
    flyout = FakeUpdateFlyout()
    captured = {}

    import src.app as app_mod

    def _fake_run(controller, fl, **kw):
        captured["controller"] = controller
        return None

    handlers = app.make_update_handlers(
        lambda: holder["controller"], flyout,
        prefs_module=FakePrefsModule(), browser=FakeBrowser(),
    )
    # Fill the holder AFTER the handlers were built (simulating the real late-bind).
    live = FakeUpdateController()
    holder["controller"] = live

    orig = app_mod.run_update_check
    try:
        app_mod.run_update_check = _fake_run
        handlers["check_for_update_now"]()
    finally:
        app_mod.run_update_check = orig

    assert captured["controller"] is live  # the LIVE controller, not None


def test_make_bridge_handlers_merges_update_handlers_with_get_controller(monkeypatch):
    """When build_gui passes get_controller, make_bridge_handlers merges the five
    update handlers into the dispatch map so the bridge can call them."""
    flyout = RecordingFlyout()

    class FakeSession:
        login_submit = submit_code = resend_code = staticmethod(lambda *a: None)
        select_printer = open_printer_select = logout = staticmethod(lambda *a: None)

    handlers = app.make_bridge_handlers(
        flyout, FakeSession(), get_controller=lambda: None
    )
    for name in (
        "check_for_update_now",
        "skip_update_version",
        "dismiss_update",
        "set_auto_update",
        "open_release_page",
    ):
        assert name in handlers


def test_state_provider_seeds_auto_update_enabled(monkeypatch):
    """build_gui's _state_provider seeds autoUpdateEnabled from persisted prefs."""
    gui, _ = _build_gui(monkeypatch)
    # Force the persisted pref to False and re-pull the seed.
    from src import update_prefs
    monkeypatch.setattr(
        update_prefs, "load_update_prefs",
        lambda: {**update_prefs.DEFAULTS, "auto_update_enabled": False},
    )
    seed = gui["api"].get_initial_state()
    assert seed["autoUpdateEnabled"] is False
