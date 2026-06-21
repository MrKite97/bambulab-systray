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

from src import auth, mqtt_client, render, spike
from src.state import PrintState
from src.tray import TrayController

logger = logging.getLogger("app")

# The single right-click menu label (LOCKED by 02-CONTEXT.md). Exposed as a
# module constant so tests assert the exact text without a heavy pystray import.
QUIT_LABEL = "Afsluiten"


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


def build_app(token, *, connect=None, sleep=None):
    """Compose the whole app from an already-acquired ``token`` and return the
    wired pieces WITHOUT starting any thread or launching a real tray.

    Returns a dict with ``icon``, ``controller``, ``client``, ``state``,
    ``network_runner`` (a zero-arg callable that runs the MQTT session) and
    ``shutdown_event``. This is the injectable seam the tests drive with fakes:
    they monkeypatch ``auth.*`` / ``mqtt_client.build_client`` so no real broker,
    login, or tray is ever touched. The token is never logged.
    """
    if sleep is None:
        sleep = time.sleep

    # Derive MQTT identity + serial exactly as spike.run_with_relogin does.
    username = auth.mqtt_username_from_token(token)
    devices = auth.get_device_list(token)
    serial = auth.pick_serial(devices)
    logger.info("Using printer serial (dev_id): %s", serial)

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
    icon = pystray.Icon(
        "bambu-systray",
        icon=render.render_icon(None),  # neutral icon until the first report
        title=render.tooltip_text(state),  # "Geen actieve print"
        menu=pystray.Menu(pystray.MenuItem(QUIT_LABEL, quit_handler)),
    )

    controller = TrayController(icon, state)

    # Wire the client: userdata carries serial+state for the module callbacks;
    # the on_message wrapper both merges AND signals the controller.
    client.user_data_set({"serial": serial, "state": state})
    client.on_connect = mqtt_client.on_connect
    client.on_message = make_on_message(controller)
    client.on_disconnect = mqtt_client.on_disconnect

    if connect is None:
        connect = make_connect(shutdown_event)

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


def main(argv=None) -> int:
    """Prompt for credentials, acquire a token via the reused Phase 1 flow, then
    run the tray until 'Afsluiten'.

    The network session runs on a daemon thread; pystray's ``icon.run`` blocks
    the UI thread (the pump thread is started inside ``build_setup``). On
    'Afsluiten' the icon stops, ``run`` returns, and we briefly join the network
    thread. No secret is ever logged.
    """
    parser = argparse.ArgumentParser(description="Bambu Lab system-tray app.")
    parser.add_argument("--debug", action="store_true", help="enable DEBUG logging")
    args = parser.parse_args(argv)
    _configure_logging(args.debug)

    logger.info("Bambu Lab systray -- starting. Right-click the tray icon -> Afsluiten to quit.")
    email = input("Bambu account email: ").strip()
    # getpass: no echo, in-memory only, never persisted or logged (T-02-06).
    password = getpass.getpass("Bambu account password: ")

    try:
        token = spike.get_access_token(email, password)
    except SystemExit as exc:
        logger.error("%s", exc)
        return 1

    app = build_app(token)
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
