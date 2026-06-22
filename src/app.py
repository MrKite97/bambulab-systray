"""app.py: the Phase 2 system-tray entry point.

This is the wiring layer that composes the already-built, unit-tested Phase 1
modules (``spike`` login flow, ``auth``, ``mqtt_client``, ``state``) and the
Phase 2 tray layer (``render``, ``tray.TrayController``) into a runnable,
windowless pystray app. It adds ONLY orchestration -- it reimplements none of
the login, MQTT, merge, render, or marshalling logic.

What it delivers (APP-01 / STAT-02 / STAT-03):

  * Token acquisition reuses ``spike.get_access_token`` + ``auth.*`` exactly as
    ``spike.run_with_relogin`` does -- the first-run verifyCode email-code prompt
    stays a console ``input()`` this phase (full windowless re-auth is Phase 3).
  * A single long-lived MQTT session runs on a daemon NETWORK thread and writes
    the shared ``PrintState`` via the vetted ``mqtt_client.on_message`` merge.
  * The pystray ``Icon`` runs on the UI thread; a single dedicated daemon PUMP
    thread (started in ``TrayController.build_setup``) is the ONLY place icon
    attributes are written.
  * A single "Afsluiten" right-click item cleanly disconnects MQTT, signals the
    network loop to stop, and DISPOSES the icon (``icon.stop()``) -- no orphan.

MARSHALLING BOUNDARY (LOCKED, 02-CONTEXT.md "Behavior & Threading"):
The MQTT callback thread NEVER touches the pystray icon. After the guarded
delta-merge it only calls ``controller.on_state_change()`` (an enqueue). The UI
pump thread drains that queue and is the sole writer of ``icon.icon`` /
``icon.title``. Crossing that boundary would mutate the STA Shell_NotifyIcon
handle off-thread (T-02-03).

SECURITY (threat register T-02-06): the password is read with ``getpass`` (no
echo, in-memory only); the token / password / MQTT username / Authorization
header are NEVER logged. Only ``gcode_state`` / ``mc_percent`` /
``mc_remaining_time`` reach the tooltip via ``render``.
"""

import argparse
import getpass
import logging
import threading
import time
from uuid import uuid4

import pystray

from src import (
    auth,
    autostart,
    mqtt_client,
    render,
    settings as settings_module,
    single_instance,
    spike,
    token_store,
)
from src.state import PrintState
from src.status import ConnectionStatus
from src.tray import TrayController

logger = logging.getLogger("app")

# The single right-click menu label (LOCKED by 02-CONTEXT.md). Exposed as a
# module constant so tests assert the exact text without a heavy pystray import.
QUIT_LABEL = "Afsluiten"

# The "Opnieuw verbinden / inloggen" menu label (LOCKED by 03-CONTEXT.md). It
# sits ALONGSIDE 'Afsluiten' and drives a fresh verifyCode login (REL-02).
RELOGIN_LABEL = "Opnieuw verbinden / inloggen"

# The checkable autostart toggle label (APP-02). The item's check state reflects
# the current HKCU\Run registration; clicking it flips it on/off.
AUTOSTART_LABEL = "Met Windows opstarten"


def make_autostart_toggle(autostart_mod=None):
    """Build the 'Met Windows opstarten' click handler.

    On click: if autostart is currently enabled -> disable it, else enable it,
    via the real ``autostart`` module (``autostart.is_enabled`` /
    ``autostart.enable`` / ``autostart.disable`` against the HKCU\\Run key).
    ``autostart_mod`` is injectable so tests drive the toggle with a fake module
    (no real registry write); the menu item's ``checked=`` lambda separately reads
    the same module's ``is_enabled()`` so the tick mirrors the live Run-key state.
    """
    mod = autostart_mod if autostart_mod is not None else autostart

    def _toggle(icon=None, item=None):
        # Toggle the HKCU\Run registration: autostart.is_enabled ->
        # autostart.disable / autostart.enable (mod is the real module by default,
        # a fake under test).
        if mod.is_enabled():
            mod.disable()
        else:
            mod.enable()

    return _toggle


def persist_serial(serial, *, settings=settings_module):
    """Persist the picked printer ``serial`` to the settings JSON.

    Loads current settings (region/serial) and overlays ONLY the serial, so the
    remembered region is preserved. ``save_settings`` writes an allowlist
    (region/serial) -- the token is structurally never written here (T-04-01). The
    serial is benign and not logged. ``settings`` is injectable for tests.
    """
    settings.save_settings({**settings.load_settings(), "serial": serial})


def make_on_message(controller):
    """Wrap ``mqtt_client.on_message`` so each report BOTH merges the delta into
    the shared PrintState AND signals the controller to (eventually) repaint.

    The wrapper runs on the NETWORK (paho) callback thread. It therefore only
    ENQUEUES a repaint via ``controller.on_state_change()`` -- it never mutates
    the icon. ``mqtt_client.on_message`` is already guarded against malformed
    JSON / a missing ``print`` key, and ``on_state_change`` merely puts a key on
    a queue, so a bad broker payload cannot crash this thread (T-02-09).
    """

    def _on_message(client, userdata, msg):
        mqtt_client.on_message(client, userdata, msg)  # guarded delta-merge
        controller.on_state_change()  # enqueue a repaint (UI thread applies it)

    return _on_message


def make_on_connect(controller):
    """Wrap ``mqtt_client.on_connect`` so each (re)connect ALSO publishes a
    CONNECTED status transition to the controller.

    Runs on the NETWORK (paho) callback thread. After the vetted module-level
    on_connect (subscribe + pushall) it only ENQUEUES the status via
    ``set_connection_status`` -- it NEVER mutates the icon. The UI pump derives
    the 5-state display from the latest status + PrintState (REL-01/REL-03).
    """

    def _on_connect(client, userdata, flags, reason_code, properties):
        mqtt_client.on_connect(client, userdata, flags, reason_code, properties)
        controller.set_connection_status(ConnectionStatus.CONNECTED)

    return _on_connect


def make_on_disconnect(controller):
    """Wrap the (inert) ``mqtt_client.on_disconnect`` so a drop ALSO publishes a
    DISCONNECTED status transition ("Verbinden…") to the controller.

    Reconnection stays owned solely by ``run_session``'s backoff -- this wrapper
    adds NO reconnect; it only enqueues the status so the tray reflects the drop
    while the existing single-session backoff recovers (REL-01/REL-03).
    """

    def _on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
        mqtt_client.on_disconnect(client, userdata, disconnect_flags, reason_code, properties)
        controller.set_connection_status(ConnectionStatus.DISCONNECTED)

    return _on_disconnect


def make_status_connect(controller, inner_connect, *, on_auth_fail):
    """Wrap an injected ``connect`` so a token/auth rejection flips the tray to
    TOKEN_EXPIRED and triggers re-login, WITHOUT adding a second reconnect loop.

    ``inner_connect`` is the real connect callable handed to
    ``mqtt_client.run_session`` (e.g. the one from :func:`make_connect`). On any
    failure we inspect it with ``spike._auth_failed`` (the SAME 401 detector the
    Phase 1 spike uses): a token rejection enqueues
    ``ConnectionStatus.TOKEN_EXPIRED`` and runs ``on_auth_fail`` (the shared
    re-login routine), then RE-RAISES so ``run_session`` keeps owning the
    backoff/loop. A non-auth (transient) failure is just re-raised so the
    existing backoff retries -- no TOKEN_EXPIRED, no new loop.
    """

    def _connect(client):
        try:
            inner_connect(client)
        except mqtt_client.StopSession:
            raise  # clean quit -> let run_session end the loop
        except Exception as exc:  # noqa: BLE001 - classify auth vs transient
            if spike._auth_failed(exc):
                controller.set_connection_status(ConnectionStatus.TOKEN_EXPIRED)
                on_auth_fail()
            raise  # re-raise either way: run_session owns the backoff/retry

    return _connect


def make_quit_handler(client, shutdown_event):
    """Build the 'Afsluiten' menu callback that tears down BOTH threads and
    disposes the icon.

    The handler (1) sets the shutdown event so the network loop stops looping,
    (2) disconnects the MQTT client -- which makes ``loop_forever()`` return so
    ``run_session`` ends cleanly, and (3) calls ``icon.stop()`` to dispose the
    tray icon and unblock ``icon.run()``. ``icon.stop()`` is called LAST and
    unconditionally so NO orphaned tray icon is ever left behind (T-02-07).
    """

    def _on_quit(icon, item):
        shutdown_event.set()
        try:
            client.disconnect()  # ends loop_forever -> run_session sees a clean end
        except Exception:  # noqa: BLE001 - teardown must never block icon disposal
            logger.debug("client.disconnect() raised during quit; continuing to icon.stop()")
        icon.stop()  # disposes the tray icon (NO orphan); always reached

    return _on_quit


class _ReloginHandler:
    """The single re-login routine BOTH the menu item and the automatic 401 path
    converge on (REL-02). Implemented as a callable object so it works as a
    pystray menu callback (``handler(icon, item)``) AND as a zero-arg
    ``on_auth_fail()`` hook from the connect wrapper.

    Each invocation, in this exact order (threat T-03-06/T-03-07):
      1. flag ConnectionStatus.TOKEN_EXPIRED so the tray shows
         "Opnieuw inloggen vereist" immediately,
      2. clear the stale token via ``token_store.clear_token()`` BEFORE any
         fresh login -- a rejected/stale token is never reused,
      3. drive a FRESH verifyCode login via ``get_token(force_relogin=True)``
         (no silent refresh -- the Bambu refresh endpoint is dead),
      4. tear down the current MQTT session (``client.disconnect()``) so the
         EXISTING ``run_session`` backoff reconnects with the new token -- no
         second reconnect mechanism is introduced.

    The token/password are never logged. ``get_token`` is injected so tests drive
    it without real stdin/network.
    """

    def __init__(self, client, shutdown_event, *, get_token, controller=None):
        self._client = client
        self._shutdown_event = shutdown_event
        self._get_token = get_token
        self._controller = controller

    def bind_controller(self, controller):
        """Late-bind the controller (build_app builds the handler before the
        TrayController exists so the menu can reference it)."""
        self._controller = controller

    def __call__(self, icon=None, item=None):
        if self._controller is not None:
            # (1) visible "Opnieuw inloggen vereist" -- enqueue only, never icon.
            self._controller.set_connection_status(ConnectionStatus.TOKEN_EXPIRED)
        # (2) clear the stale token FIRST (no stale-credential reuse).
        token_store.clear_token()
        # (3) FRESH verifyCode login (force_relogin=True; no silent refresh).
        #     The new token is persisted inside spike.get_access_token.
        self._get_token(force_relogin=True)
        # (4) drop the live session so the existing backoff reconnects with the
        #     new token. Never raise out of a menu callback / teardown path.
        try:
            self._client.disconnect()
        except Exception:  # noqa: BLE001 - teardown must not break the UI/auth path
            logger.debug("client.disconnect() raised during re-login; continuing")


def make_relogin_handler(client, shutdown_event, *, get_token, controller=None):
    """Build the shared re-login routine (see :class:`_ReloginHandler`)."""
    return _ReloginHandler(
        client, shutdown_event, get_token=get_token, controller=controller
    )


def make_setup(controller, shutdown_event, *, pump_interval=1.0):
    """Wrap ``controller.build_setup()`` so that, in addition to painting the
    first frame on the UI thread, it starts the ONE dedicated daemon PUMP thread.

    That pump thread is the single place icon attributes are written after
    startup: it loops ``controller.pump_once()`` (which drains the network
    thread's enqueued repaint requests and applies AT MOST one debounced repaint)
    every ``pump_interval`` seconds until the shutdown event is set. Keeping the
    pump on its own thread -- never on the MQTT callback thread -- is the locked
    marshalling boundary. pystray's Windows backend re-reads ``icon.icon`` /
    ``icon.title`` on its own message loop, so writing them here is UI-safe.
    """
    base_setup = controller.build_setup()

    def _pump_loop():
        while not shutdown_event.is_set():
            controller.pump_once()
            time.sleep(pump_interval)

    def _setup(icon):
        base_setup(icon)  # first paint + make visible (UI thread)
        threading.Thread(target=_pump_loop, name="tray-pump", daemon=True).start()

    return _setup


def make_connect(shutdown_event):
    """Build the injected ``connect`` for ``run_session``.

    On entry it checks the shutdown event and raises ``StopSession`` if quit has
    already been requested (so a quit during backoff ends the loop). Otherwise it
    connects and runs the live session via ``loop_forever()`` -- which returns
    when the quit handler calls ``client.disconnect()``.
    """

    def _connect(client):
        if shutdown_event.is_set():
            raise mqtt_client.StopSession()
        client.connect(
            mqtt_client.BROKER_HOST,
            mqtt_client.PORT,
            keepalive=mqtt_client.KEEPALIVE,
        )
        client.loop_forever()
        # loop_forever returned (disconnect). If quit asked, end the session.
        if shutdown_event.is_set():
            raise mqtt_client.StopSession()

    return _connect


def build_app(
    token,
    *,
    connect=None,
    sleep=None,
    now=time.monotonic,
    get_token=None,
    email=None,
    password=None,
    autostart_mod=autostart,
    settings=settings_module,
):
    """Compose the whole app from an already-acquired ``token`` and return the
    wired pieces WITHOUT starting any thread or launching a real tray.

    Returns a dict with ``icon``, ``controller``, ``client``, ``state``,
    ``network_runner`` (a zero-arg callable that runs the MQTT session) and
    ``shutdown_event``. This is the injectable seam the tests drive with fakes:
    they monkeypatch ``auth.*`` / ``mqtt_client.build_client`` so no real broker,
    login, or tray is ever touched. The token is never logged.

    ``now`` is the injectable monotonic clock threaded into the TrayController so
    the freshness/offline watcher is driveable in tests with no real waits.
    ``get_token`` is the injectable re-login closure (defaults to a
    ``spike.get_access_token`` binding) so the menu/401 re-login path runs
    without real stdin/network in tests.
    """
    if sleep is None:
        sleep = time.sleep

    # Derive MQTT identity + serial exactly as spike.run_with_relogin does.
    username = auth.mqtt_username_from_token(token)
    devices = auth.get_device_list(token)
    serial = auth.pick_serial(devices)
    logger.info("Using printer serial (dev_id): %s", serial)

    # Remember the picked serial across restarts (APP-03). save_settings writes an
    # allowlist (region/serial) so the token is structurally never persisted here
    # (T-04-01); the existing region is preserved by loading first.
    persist_serial(serial, settings=settings)

    # Single source of truth shared across the network and UI threads.
    state = PrintState()

    # Build the pystray Icon FIRST with a neutral initial frame so something is
    # visible immediately; the controller then owns all later repaints.
    shutdown_event = threading.Event()
    client = mqtt_client.build_client(
        client_id=f"bambu-systray-{uuid4()}",
        username=username,
        access_token=token,
    )
    quit_handler = make_quit_handler(client, shutdown_event)

    # Default the re-login token source to a spike.get_access_token closure so the
    # menu/401 path drives a FRESH verifyCode login (console verifyCode prompt is
    # reused from Phase 1). Injected in tests so it runs with no real stdin.
    if get_token is None:
        def get_token(*, force_relogin):
            return spike.get_access_token(
                email or "", password or "", force_relogin=force_relogin
            )

    # The re-login routine BOTH the menu item and the automatic 401 path converge
    # on. Clears the stale token first, then forces a fresh login (no silent
    # refresh) and tears down the live session so run_session reconnects with the
    # new token. Built before the menu so the menu item can reference it.
    relogin_handler = make_relogin_handler(
        client, shutdown_event, get_token=get_token, controller=None
    )

    icon = pystray.Icon(
        "bambu-systray",
        icon=render.render_icon(None),  # neutral icon until the first report
        title=render.tooltip_text(state),  # "Geen actieve print"
        # Items, in order: the checkable autostart toggle (APP-02), then the
        # re-login item (REL-02), then Afsluiten. The autostart tick mirrors the
        # live HKCU\Run state via the checked= lambda.
        menu=pystray.Menu(
            pystray.MenuItem(
                AUTOSTART_LABEL,
                make_autostart_toggle(autostart_mod),
                # The tick reflects the live HKCU\Run state. Default reads the real
                # autostart.is_enabled(); a fake module is honored under test.
                checked=lambda item: (
                    autostart.is_enabled()
                    if autostart_mod is autostart
                    else autostart_mod.is_enabled()
                ),
            ),
            pystray.MenuItem(RELOGIN_LABEL, relogin_handler),
            pystray.MenuItem(QUIT_LABEL, quit_handler),
        ),
    )

    # Inject the monotonic clock so the freshness/offline watcher is driveable.
    controller = TrayController(icon, state, now=now)
    # The re-login routine needs the controller to flag TOKEN_EXPIRED; wire it now
    # that the controller exists (the menu callback closes over this same object).
    relogin_handler.bind_controller(controller)

    # Wire the client: userdata carries serial+state for the module callbacks;
    # the on_message wrapper both merges AND signals the controller. on_connect /
    # on_disconnect now ALSO publish the CONNECTED / DISCONNECTED status.
    client.user_data_set({"serial": serial, "state": state})
    client.on_connect = make_on_connect(controller)
    client.on_message = make_on_message(controller)
    client.on_disconnect = make_on_disconnect(controller)

    if connect is None:
        connect = make_connect(shutdown_event)
    # Wrap the connect so a 401/auth rejection flips the tray to TOKEN_EXPIRED and
    # drives the SAME re-login routine as the menu -- without adding a second
    # reconnect loop (run_session keeps owning the backoff).
    connect = make_status_connect(controller, connect, on_auth_fail=relogin_handler)

    def network_runner():
        """Run the single long-lived MQTT session (on the network thread)."""
        try:
            mqtt_client.run_session(client, connect=connect, sleep=sleep)
        except Exception:  # noqa: BLE001 - never let the daemon thread crash loudly
            logger.debug("network session ended with an exception", exc_info=False)

    return {
        "icon": icon,
        "controller": controller,
        "client": client,
        "state": state,
        "network_runner": network_runner,
        "shutdown_event": shutdown_event,
    }


def _configure_logging(debug: bool) -> None:
    """Console logging with a clean formatter; ``--debug`` toggles DEBUG."""
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


def main(argv=None, *, guard=None) -> int:
    """Prompt for credentials, acquire a token via the reused Phase 1 flow, then
    run the tray until 'Afsluiten'.

    Before doing anything else a single-instance guard runs: if another instance
    already holds the named mutex, log one line and ``return 0`` cleanly WITHOUT
    touching the tray or MQTT (threat T-04-06). ``guard`` is injectable so tests
    drive the already-running path without a real OS mutex.

    The network session runs on a daemon thread; pystray's ``icon.run`` blocks
    the UI thread (the pump thread is started inside ``build_setup``). On
    'Afsluiten' the icon stops, ``run`` returns, and we briefly join the network
    thread. No secret is ever logged.
    """
    parser = argparse.ArgumentParser(description="Bambu Lab system-tray app.")
    parser.add_argument("--debug", action="store_true", help="enable DEBUG logging")
    args = parser.parse_args(argv)
    _configure_logging(args.debug)

    # Single-instance guard FIRST: a second launch exits cleanly without
    # disturbing the running one (no tray, no MQTT). Fails open if the OS call
    # itself errors, so the guard can never block a legitimate first launch.
    if guard is None:
        guard = single_instance.InstanceGuard()
    if not guard.acquire():
        logger.info("Bambu Lab systray draait al; deze tweede instantie wordt afgesloten.")
        return 0

    logger.info("Bambu Lab systray -- starting. Right-click the tray icon -> Afsluiten to quit.")

    # Load persisted settings (region/serial) so a remembered serial is available;
    # defaults are used on a missing/corrupt file (no regression to the login flow).
    settings = settings_module.load_settings()
    logger.debug("Loaded settings (serial remembered: %s)", settings.get("serial") is not None)

    email = input("Bambu account email: ").strip()
    # getpass: no echo, in-memory only, never persisted or logged (T-02-06).
    password = getpass.getpass("Bambu account password: ")

    try:
        token = spike.get_access_token(email, password)
    except SystemExit as exc:
        logger.error("%s", exc)
        return 1

    # Pass the credentials so the menu/401 re-login can drive a fresh login. They
    # are held only in memory and never logged (T-03-06).
    app = build_app(token, email=email, password=password)
    icon = app["icon"]
    controller = app["controller"]

    network_thread = threading.Thread(
        target=app["network_runner"], name="mqtt-network", daemon=True
    )
    network_thread.start()

    # icon.run blocks the UI thread until 'Afsluiten' calls icon.stop().
    # make_setup paints the first frame AND starts the single UI pump thread.
    icon.run(setup=make_setup(controller, app["shutdown_event"]))

    # 'Afsluiten' was clicked: the shutdown event is set and disconnect requested.
    app["shutdown_event"].set()
    network_thread.join(timeout=5)
    logger.info("Stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
