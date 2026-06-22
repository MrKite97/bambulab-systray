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
    """Stand-in for a pystray Icon: records icon/title assignments and stop()."""

    def __init__(self):
        self.icon = None
        self.title = None
        self.visible = False
        self.stop_count = 0

    def stop(self):
        self.stop_count += 1


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


# --- Menu label -------------------------------------------------------------


def test_quit_label_is_afsluiten():
    """The single right-click menu label is exactly 'Afsluiten' (LOCKED)."""
    assert app.QUIT_LABEL == "Afsluiten"


def test_built_menu_item_text_is_afsluiten(monkeypatch):
    """The pystray MenuItem constructed by build_app carries the text 'Afsluiten'."""
    icon = _patch_build_app(monkeypatch)
    app.build_app("HEADER.eyJ1c2VybmFtZSI6InVfMSJ9.SIG")
    # The fake pystray.MenuItem records its (text, callback); assert the label.
    assert icon["menu_items"], "a menu item should have been constructed"
    assert icon["menu_items"][0][0] == "Afsluiten"


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


# --- build_app with no network ----------------------------------------------


def _patch_build_app(monkeypatch):
    """Monkeypatch auth.* + pystray so build_app runs with no network/tray.

    Returns a shared dict capturing the constructed FakeIcon and the menu items
    so tests can inspect the wiring.
    """
    captured = {"menu_items": [], "icon_obj": None}

    monkeypatch.setattr(app.auth, "mqtt_username_from_token", lambda t: "u_1")
    monkeypatch.setattr(app.auth, "get_device_list", lambda t: [{"dev_id": "SER"}])
    monkeypatch.setattr(app.auth, "pick_serial", lambda devs: "SER")
    monkeypatch.setattr(
        app.mqtt_client,
        "build_client",
        lambda client_id, username, access_token: FakeClient(
            client_id=client_id, username=username, access_token=access_token
        ),
    )

    # Fake pystray Icon/Menu/MenuItem so no real tray is constructed.
    class FakeMenuItem:
        def __init__(self, text, action):
            self.text = text
            self.action = action
            captured["menu_items"].append((text, action))

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
    assert result["client"].on_connect is app.mqtt_client.on_connect


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
