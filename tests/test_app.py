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
        def __init__(self, text, action, checked=None):
            self.text = text
            self.action = action
            self.checked = checked
            captured["menu_items"].append((text, action))
            captured.setdefault("menu_objs", []).append(self)

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


def test_401_and_menu_share_relogin(monkeypatch):
    """The automatic 401 path and the menu item reach the SAME re-login routine:
    build_app wires the menu callback AND the connect wrapper's on_auth_fail to
    one _ReloginHandler instance."""
    captured = _patch_build_app(monkeypatch)
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

    # The menu's re-login callback IS a _ReloginHandler instance.
    menu_handler = None
    for text, action in captured["menu_items"]:
        if text == app.RELOGIN_LABEL:
            menu_handler = action
    assert isinstance(menu_handler, app._ReloginHandler)

    # Drive the network runner. First connect raises an auth error; the wrapper
    # flags TOKEN_EXPIRED and runs the SAME routine. run_session then backs off
    # (sleep sets the shutdown event) and retries; the second attempt raises
    # StopSession so the loop ends cleanly.
    result["network_runner"]()

    # The shared routine ran the fresh login (force_relogin=True).
    assert calls and calls[0] is True
    # The auto-401 path flagged TOKEN_EXPIRED on the same controller the menu uses.
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
