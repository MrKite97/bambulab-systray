"""app.py: the Phase 2 system-tray entry point.

This is the wiring layer that composes the already-built, unit-tested Phase 1
modules (``spike`` login flow, ``auth``, ``mqtt_client``, ``state``) and the
Phase 2 tray layer (``render``, ``tray.TrayController``) into a runnable,
windowless pystray app. It adds ONLY orchestration -- it reimplements none of
the login, MQTT, merge, render, or marshalling logic.

What it delivers (APP-01 / STAT-02 / STAT-03 / Phase 8 LOGIN/SEL):

  * Login, 2FA, and printer-select happen ENTIRELY in the panel via the real
    :class:`~src.session.SessionController` -- there is NO console
    ``input()``/``getpass`` here (Plan 08-02 removed it). On start ``main()``
    bootstraps from a stored token + serial (validated; 401 -> login screen) or
    opens the panel on the login screen with a LOGGED-OUT tray glyph.
  * A single long-lived MQTT session runs on a daemon NETWORK thread and writes
    the shared ``PrintState`` via the vetted ``mqtt_client.on_message`` merge --
    started ONLY post-login/post-select via the deferred ``start_mqtt`` hook.
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

SECURITY (threat register T-02-06 / T-08-06): no credential is read from the
console (the panel collects email/password/code); the token / password / MQTT
username / Authorization header are NEVER logged. Only ``gcode_state`` /
``mc_percent`` / ``mc_remaining_time`` reach the tooltip via ``render``.
"""

import argparse
import logging
import threading
import time
from uuid import uuid4

import pystray

from src import (
    auth,
    autostart,
    bridge,
    control,
    mqtt_client,
    render,
    settings as settings_module,
    single_instance,
    spike,
    token_store,
)
from src.flyout import FlyoutWindow
from src.state import PrintState
from src.status import ConnectionStatus
from src.tray import TrayController, make_open_item

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


# The set of serialized ``status`` values that are TERMINAL: a transition into
# either must ALWAYS be pushed to the panel, bypassing the throttle, so a
# finished/failed print is never silently dropped (T-09-04).
_TERMINAL_STATUSES = ("done", "error")

# The serialized keys that constitute a "displayed-field change": when none of
# these changed since the last push the report is throttled (T-09-03). Chosen so
# every value the panel actually renders is covered.
_DISPLAYED_KEYS = (
    "status", "pct", "etaLabel", "layer", "totalLayer", "file",
    "nozzle", "nozzleTarget", "bed", "bedTarget",
)


def _display_signature(serialized: dict) -> tuple:
    """A hashable tuple of the panel-displayed fields used to throttle pushes."""
    return tuple(serialized.get(k) for k in _DISPLAYED_KEYS)


def make_on_message(
    controller,
    *,
    flyout=None,
    state=None,
    connection_provider=None,
    printer_name="",
    session_stop=None,
):
    """Wrap ``mqtt_client.on_message`` so each report BOTH merges the delta into
    the shared PrintState AND signals the controller to (eventually) repaint --
    and, when a ``flyout`` is wired (Plan 09-01), ALSO pushes the freshly-merged,
    serialized state to the panel (THROTTLED, terminal-state-safe).

    The wrapper runs on the NETWORK (paho) callback thread. It therefore only
    ENQUEUES a repaint via ``controller.on_state_change()`` -- it never mutates
    the icon. ``mqtt_client.on_message`` is already guarded against malformed
    JSON / a missing ``print`` key, and ``on_state_change`` merely puts a key on
    a queue, so a bad broker payload cannot crash this thread (T-02-09).

    Live-push (Plan 09-01, PANEL-01): after the merge + tray signal, the SHARED
    ``state`` is serialized via :func:`bridge.serialize_state` (secret-free by
    construction -- T-09-01) and pushed to the panel via ``flyout.push_state``.
    The push is:

    - THROTTLED (T-09-03): only pushed when a panel-displayed field changed since
      the last push (compared via :func:`_display_signature`), so a burst of
      identical reports does not flood ``evaluate_js``.
    - TERMINAL-SAFE (T-09-04): a transition whose serialized ``status`` is
      ``done``/``error`` is ALWAYS pushed, even if the throttle would otherwise
      suppress it, so a FINISH/FAILED is never silently dropped.
    - FIRE-AND-FORGET: the push never gates the network thread on the page JS
      return value, and any push failure is swallowed (the merge + tray signal
      must always complete -- the panel is best-effort).

    ``flyout``/``state`` are optional so the v1 console/tray-only path (no panel)
    keeps the original merge-and-signal behavior unchanged. ``connection_provider``
    is an optional zero-arg callable yielding the current
    :class:`~src.status.ConnectionStatus` to serialize with (defaults to
    CONNECTED, since a report only arrives over a live session); ``printer_name``
    is the display name passed through to the serialized dict (default "").
    """
    # Per-wrapper throttle memory (closed over). Lives on the network thread only,
    # so no lock is needed -- paho delivers messages serially per client.
    _last_signature = {"sig": None}

    def _push_live_state():
        """Serialize the merged shared state and push it to the panel (throttled,
        terminal-safe, fire-and-forget). Never raises into the callback thread."""
        # Skip once the session is being torn down (logout): a late in-flight
        # report must not push a stale logged_in=True state that would flip the
        # panel back off the login screen the logout just landed on.
        if session_stop is not None and session_stop.is_set():
            return
        try:
            connection = (
                connection_provider()
                if connection_provider is not None
                else ConnectionStatus.CONNECTED
            )
            serialized = bridge.serialize_state(
                state,
                connection,
                logged_in=True,
                printer_name=printer_name,
                theme=render.detect_windows_theme(),
            )
            signature = _display_signature(serialized)
            terminal = serialized.get("status") in _TERMINAL_STATUSES
            # Throttle: skip when nothing the panel shows changed -- UNLESS this is
            # a terminal transition, which is always pushed (T-09-04).
            if signature == _last_signature["sig"] and not terminal:
                return
            _last_signature["sig"] = signature
            flyout.push_state(serialized)  # fire-and-forget; no evaluate_js wait
        except Exception:  # noqa: BLE001 - panel push is best-effort; never crash net thread
            logger.debug("flyout push_state failed on report; continuing", exc_info=False)

    def _on_message(client, userdata, msg):
        mqtt_client.on_message(client, userdata, msg)  # guarded delta-merge
        controller.on_state_change()  # enqueue a repaint (UI thread applies it)
        if flyout is not None and state is not None:
            _push_live_state()  # live panel update (throttled, terminal-safe)

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


def make_quit_handler(client, shutdown_event, *, flyout=None):
    """Build the 'Afsluiten' menu callback that tears down the GUI + both threads.

    CANONICAL ORDER (LOCKED, deadlock-safety -- 07-CONTEXT.md "Threading model"):
    because ``webview.start()`` blocks the MAIN thread until every window is
    destroyed, the flyout window MUST be destroyed FIRST -- otherwise the main
    thread stays parked in ``webview.start()`` forever (deadlock) and the process
    never exits. The exact sequence:

      a. ``flyout.destroy()``    -- FIRST: unblocks the main thread's webview.start()
      b. ``shutdown_event.set()``
      c. ``client.disconnect()`` -- ends loop_forever -> run_session sees a clean end
      d. ``icon.stop()``         -- disposes the tray icon (NO orphan)

    Every teardown step is wrapped so the quit path NEVER raises and NEVER leaves
    an orphan icon: ``icon.stop()`` is reached even if destroy/disconnect fail
    (T-02-07 / T-07-06). ``flyout`` is optional so the v1 console/tray-only path
    (no window) keeps working; when None the sequence is just b->c->d.
    """

    def _on_quit(icon, item):
        if flyout is not None:
            try:
                flyout.destroy()  # FIRST: unblocks the main thread's webview.start()
            except Exception:  # noqa: BLE001 - teardown must never block the rest
                logger.debug("flyout.destroy() raised during quit; continuing")
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

    def bind_client(self, client):
        """Late-bind the live MQTT client (Plan 08-02 deferred-MQTT path builds
        the handler before any client exists; start_mqtt binds it once a session
        starts so the menu/401 disconnect targets the running client)."""
        self._client = client

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
        #     new token. Never raise out of a menu callback / teardown path. The
        #     client may be None on the deferred path (no session started yet).
        if self._client is not None:
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


# --- Phase 8 flyout/bridge wiring (real SessionController handlers) --------- #
#
# Phase 8 wires the panel auth/select actions to the REAL SessionController
# (src.session). The five page actions (login_submit/submit_code/resend_code/
# select_printer/logout) now drive the real auth/token_store/settings flow; Phase 9
# (Plan 09-02) replaces the last ``control`` stub with the real control handler
# (Api.control -> control.publish_command). The SessionController is the single
# brain: it logs nothing sensitive and pushes only secret-free state to the page
# (T-08-08). ``serialize_state`` (Plan 07-02) guarantees the pushed state carries
# no secret.

# The page-action method names still served by a NAME-only stub. After Phase 9
# (Plan 09-02) wired ``control`` to ``control.publish_command``, NO action remains
# a stub -- this stays as an (empty) extension point only.
_STUB_ACTIONS = ()


def _make_stub_action(name):
    """Build a harmless page-action handler that logs only the action NAME.

    Phase 9 replaces ``control`` with the real control handler. The returned
    callable accepts (and ignores) any args so the panel can pass a payload
    (command) WITHOUT it ever being logged (T-07-02)."""

    def _stub(*_args, **_kwargs):
        # NEVER log *_args -- they may carry a command value.
        logger.debug("bridge action: %s", name)
        return None

    return _stub


def make_control_handler(start_mqtt, *, control=control):
    """Build the REAL ``control(command)`` bridge handler (Plan 09-02, PANEL-02).

    The returned callable is the Python side of the panel's pause/resume/cancel
    buttons: it publishes the command to the printer via
    ``control.publish_command(active_client, active_serial, command)`` using the
    live MQTT client + serial captured when the session started (``start_mqtt``
    stores them on ``.client`` / ``.serial``). The handler therefore ALWAYS
    targets the user's OWN selected device -- never a broadcast or third-party
    serial (T-09-06).

    Security / robustness invariants:

    - The closed allowlist (pause/resume/stop) is enforced inside
      ``control.publish_command`` BEFORE any publish, so an out-of-allowlist
      command never reaches ``device/<serial>/request`` (T-09-05). The handler
      does NOT bypass it.
    - A rejected command (``ValueError``) is swallowed here so a bad page payload
      can never crash the bridge thread -- it is NOT published either way.
    - When no session is active yet (no client/serial captured) the handler is a
      safe no-op: it returns ``None`` WITHOUT publishing and WITHOUT logging the
      command value (T-09-07).
    - The command value is never logged here; ``control.py`` logs only the
      command name + topic at debug. The client/token internals are never logged.

    ``control`` is injectable so tests drive the mapping with a fake module (no
    real broker)."""

    def _control(command):
        client = getattr(start_mqtt, "client", None)
        serial = getattr(start_mqtt, "serial", None)
        # No live session yet -> safe no-op (never log the command value).
        if client is None or serial is None:
            return None
        try:
            control.publish_command(client, serial, command)
        except ValueError:
            # Out-of-allowlist command: rejected INSIDE publish_command before any
            # publish (T-09-05). Swallow so a bad page payload never crashes the
            # bridge thread; never log the command value.
            logger.debug("control: rejected non-allowlisted command")
        except Exception:  # noqa: BLE001 - a publish failure must not crash the bridge
            logger.debug("control: publish_command failed; continuing")
        return None

    return _control


def make_session(
    flyout,
    *,
    start_mqtt,
    stop_session,
    auth=auth,
    token_store=token_store,
    settings=settings_module,
    run_async=None,
):
    """Construct the Plan 01 :class:`~src.session.SessionController` with the real
    modules injected (the seam the bridge handlers + bootstrap call into).

    ``start_mqtt(token, serial)`` and ``stop_session()`` are the deferred-MQTT
    hooks (see :func:`make_start_mqtt` / :func:`make_stop_session`). ``auth`` /
    ``token_store`` / ``settings`` default to the real modules but are injectable
    for tests. ``run_async`` is forwarded only when provided so tests can run the
    controller's worker bodies inline (its production default is a daemon thread).
    Imported lazily so ``import src.app`` stays cheap and free of cycles."""
    from src.session import SessionController

    kwargs = {
        "auth": auth,
        "token_store": token_store,
        "settings": settings,
        "flyout": flyout,
        "start_mqtt": start_mqtt,
        "stop_session": stop_session,
    }
    if run_async is not None:
        kwargs["run_async"] = run_async
    return SessionController(**kwargs)


def make_bridge_handlers(flyout, session, *, start_mqtt=None):
    """Build the js_api handler mapping wiring the panel to the SessionController.

    ``hide`` stays the real ``flyout.hide`` (click-away / tray toggle); the five
    auth/select actions forward to the injected ``session``'s BOUND methods
    (login_submit/submit_code/resend_code/select_printer/logout) so the panel
    drives the real auth flow; ``control`` is the REAL control handler (Plan
    09-02) publishing pause/resume/stop to the captured active client+serial via
    :func:`make_control_handler`. Arguments (email/password/code/command) pass
    straight through -- nothing sensitive is logged here (T-08-08 / T-09-07).

    ``start_mqtt`` carries the live client + active serial captured when the
    session started; it is the source the control handler reads. It is optional
    so a bare ``make_bridge_handlers(flyout, session)`` still yields a control
    handler that is a safe no-op until a session exists."""
    handlers = {
        "hide": flyout.hide,
        "login_submit": session.login_submit,
        "submit_code": session.submit_code,
        "resend_code": session.resend_code,
        "select_printer": session.select_printer,
        "open_printer_select": session.open_printer_select,
        "logout": session.logout,
        "control": make_control_handler(start_mqtt),
        "resize": flyout.resize_to,
    }
    for name in _STUB_ACTIONS:
        handlers[name] = _make_stub_action(name)
    return handlers


def make_flyout_toggle(
    flyout,
    *,
    state=None,
    connection_provider=None,
    logged_in=False,
    printer_name="",
):
    """Build the zero-arg tray LEFT-click toggle.

    On the SHOW path it pushes the current Windows theme via
    ``flyout.push_theme(detect_windows_theme())`` so the panel always opens themed
    to the live light/dark setting (FLY-02), THEN (Plan 09-01 Task 2) re-pushes
    the CURRENT serialized state via ``flyout.push_state`` so a freshly opened
    panel is immediately correct rather than showing stale page defaults, THEN
    toggles visibility. Hiding pushes neither theme nor state. ``flyout.visible``
    distinguishes the two paths.

    ``state`` is the SHARED :class:`~src.state.PrintState` (optional: when None
    the SHOW path keeps the v1 theme-only behavior). ``connection_provider`` is an
    optional zero-arg callable yielding the current
    :class:`~src.status.ConnectionStatus` to serialize with (defaults to
    DISCONNECTED -- the panel's logged-in/content gating is driven by
    ``logged_in``). ``logged_in`` / ``printer_name`` are passed through to the
    serialized dict; ``logged_in`` may be a bool OR a zero-arg callable so the
    toggle reflects the LIVE session state at click time, not at build time.

    SECURITY: the pushed dict is :func:`bridge.serialize_state` output, which is
    secret-free by construction (T-09-01)."""

    def _toggle():
        # If the window is currently hidden we are about to SHOW it -> push theme
        # first so the panel renders in the right theme as it appears (FLY-02),
        # then push the current state so the panel opens current (not stale).
        if not getattr(flyout, "visible", False):
            theme = render.detect_windows_theme()
            flyout.push_theme(theme)
            is_logged_in = logged_in() if callable(logged_in) else logged_in
            # Re-push the live state ONLY when logged in, so a freshly opened
            # progress panel is current (not stale). When LOGGED OUT we must NOT
            # re-push: serialize_state would carry auth_step="login" and clobber
            # whichever auth screen the page is on -- e.g. reopening the flyout
            # after stepping away to fetch the 2FA code would throw the user back
            # to the login screen and lose the code entry. The page already holds
            # the correct auth screen, so leave it untouched.
            if state is not None and is_logged_in:
                connection = (
                    connection_provider()
                    if connection_provider is not None
                    else ConnectionStatus.DISCONNECTED
                )
                pname = printer_name() if callable(printer_name) else printer_name
                flyout.push_state(
                    bridge.serialize_state(
                        state,
                        connection,
                        logged_in=True,
                        printer_name=pname or "",
                        theme=theme,
                    )
                )
        flyout.toggle()

    return _toggle


def _wire_client(
    token,
    serial,
    *,
    state,
    controller,
    shutdown_event,
    relogin_handler,
    connect=None,
    sleep=None,
    flyout=None,
    printer_name="",
    session_stop=None,
):
    """Build + wire the MQTT client for ``(token, serial)`` and return
    ``(client, network_runner)`` WITHOUT starting any thread.

    This is the single place the client identity, userdata, callbacks, and the
    status/401 connect wrapper are wired -- reused by both the eager
    :func:`build_app` path and the deferred :func:`make_start_mqtt` hook so the
    WHEN of starting the network thread can move without duplicating the HOW.
    The token is never logged; only the serial (benign) is.

    Plan 09-01: ``flyout`` (+ ``printer_name``) are threaded into the on_message
    wrapper so each merged report also pushes the serialized state to the panel
    (throttled, terminal-safe). When ``flyout`` is None the wrapper keeps the v1
    merge-and-signal-only behavior. The connection status the panel serializes
    with is read live off the controller (the same value the tray derives from),
    so the panel shows CONNECTED/DISCONNECTED consistently with the tray."""
    if sleep is None:
        sleep = time.sleep

    username = auth.mqtt_username_from_token(token)
    client = mqtt_client.build_client(
        client_id=f"bambu-systray-{uuid4()}",
        username=username,
        access_token=token,
    )
    # userdata carries serial+state for the module callbacks; the on_message
    # wrapper both merges AND signals the controller.
    client.user_data_set({"serial": serial, "state": state})
    client.on_connect = make_on_connect(controller)
    # The panel serializes with the controller's live connection status (the same
    # source the tray uses); a report only flows over a live session so CONNECTED
    # is the natural default the controller already holds post-connect.
    def _connection_provider():
        return getattr(controller, "_status", ConnectionStatus.CONNECTED)

    client.on_message = make_on_message(
        controller,
        flyout=flyout,
        state=state,
        connection_provider=_connection_provider,
        printer_name=printer_name,
        session_stop=session_stop,
    )
    client.on_disconnect = make_on_disconnect(controller)

    if connect is None:
        connect = make_connect(shutdown_event)
    # Session-scoped stop (logout): ``session_stop`` ends THIS session's run loop
    # WITHOUT setting the global shutdown_event (which also drives the tray pump),
    # so logout leaves the app usable and a later login starts a fresh session.
    # We check it both before connecting and after the inner connect returns (a
    # disconnect from stop_session makes loop_forever return cleanly) so the loop
    # ends instead of immediately reconnecting.
    if session_stop is not None:
        _inner_connect = connect

        def connect(client, _inner=_inner_connect):
            if session_stop.is_set():
                raise mqtt_client.StopSession()
            _inner(client)
            if session_stop.is_set():
                raise mqtt_client.StopSession()

    # Wrap connect so a 401/auth rejection flips the tray to TOKEN_EXPIRED and
    # drives the SAME re-login routine as the menu -- run_session keeps owning
    # the backoff (no second reconnect loop).
    connect = make_status_connect(controller, connect, on_auth_fail=relogin_handler)

    def network_runner():
        """Run the single long-lived MQTT session (on the network thread)."""
        try:
            mqtt_client.run_session(client, connect=connect, sleep=sleep)
        except Exception:  # noqa: BLE001 - never let the daemon thread crash loudly
            logger.debug("network session ended with an exception", exc_info=False)

    return client, network_runner


def make_start_mqtt(
    *,
    state,
    controller,
    shutdown_event,
    relogin_handler,
    connect=None,
    sleep=None,
    flyout=None,
    printer_name="",
):
    """Build the deferred ``start_mqtt(token, serial)`` hook (Plan 08-02).

    The returned callable lazily builds the MQTT client + network_runner from
    ``(token, serial)`` via :func:`_wire_client` and starts ONE daemon network
    thread named "mqtt-network" -- the SAME thread main() used to start eagerly,
    now started only once a token + serial exist (post-login/post-select). It is
    IDEMPOTENT (T-08-09): repeated calls (login retries) never spawn a second
    session. The built client is exposed as ``start_mqtt.client`` so
    :func:`make_stop_session` can disconnect it on logout. The token is never
    logged."""

    def start_mqtt(token, serial):
        if start_mqtt.started:
            return  # idempotent: at most one network thread (T-08-09)
        start_mqtt.started = True
        # Capture the ACTIVE serial (the user's OWN selected device) so the
        # control handler can target it (Plan 09-02, T-09-06). Stored alongside
        # start_mqtt.client below; the serial is benign and not a secret.
        start_mqtt.serial = serial
        # Fresh per-session stop event: logout (stop_session) sets it to end THIS
        # session's run loop without touching the global shutdown_event, and a
        # later login gets a new (cleared) event so it reconnects cleanly.
        start_mqtt.session_stop = threading.Event()
        # ``controller`` is read off the attribute so build_gui can late-bind it
        # after the TrayController is constructed (avoids a construction cycle).
        client, network_runner = _wire_client(
            token,
            serial,
            state=state,
            controller=start_mqtt.controller,
            shutdown_event=shutdown_event,
            relogin_handler=relogin_handler,
            connect=connect,
            sleep=sleep,
            flyout=start_mqtt.flyout,
            printer_name=start_mqtt.printer_name,
            session_stop=start_mqtt.session_stop,
        )
        start_mqtt.client = client
        # Bind the live client into the relogin routine so the menu/401 path can
        # disconnect it (run_session then reconnects with the fresh token).
        if hasattr(relogin_handler, "bind_client"):
            relogin_handler.bind_client(client)
        threading.Thread(
            target=network_runner, name="mqtt-network", daemon=True
        ).start()

    start_mqtt.started = False
    start_mqtt.client = None
    # Per-session stop event (set by stop_session/logout); None until a session
    # starts. Kept distinct from the global shutdown_event (which drives quit).
    start_mqtt.session_stop = None
    # The ACTIVE serial captured when a session starts (Plan 09-02): the control
    # handler reads it to target the user's OWN device. None until start_mqtt runs.
    start_mqtt.serial = None
    start_mqtt.controller = controller  # late-bindable (build_gui sets it later)
    # Late-bindable panel wiring (Plan 09-01): the on_message wrapper pushes the
    # serialized state to this flyout on each report. build_gui passes the flyout
    # at construction; both stay overridable before the session starts.
    start_mqtt.flyout = flyout
    start_mqtt.printer_name = printer_name
    return start_mqtt


def make_stop_session(shutdown_event, start_mqtt):
    """Build the ``stop_session()`` hook the SessionController calls on logout.

    Ends ONLY the current MQTT session: it sets the per-session ``session_stop``
    event (so ``run_session`` breaks its loop instead of reconnecting), best-effort
    disconnects the live client, and resets ``start_mqtt.started`` so a LATER login
    starts a fresh session. It deliberately does NOT set the global
    ``shutdown_event`` (that also drives the tray pump + signals app quit), so
    logout leaves the app fully usable. Never raises -- teardown must not break the
    UI/auth path. ``shutdown_event`` stays in the signature for symmetry with the
    quit path but is not touched here."""

    def stop_session():
        session_stop = getattr(start_mqtt, "session_stop", None)
        if session_stop is not None:
            session_stop.set()  # end THIS session's run loop (no global shutdown)
        client = getattr(start_mqtt, "client", None)
        if client is not None:
            try:
                client.disconnect()
            except Exception:  # noqa: BLE001 - teardown must never raise
                logger.debug("client.disconnect() raised during stop_session; ignoring")
        # Allow a later login to start a fresh session (the old thread has ended).
        start_mqtt.started = False
        start_mqtt.client = None
        # Drop the tray back to the neutral logged-out glyph so it stops showing
        # the last print (enqueue-only; safe from this worker thread).
        controller = getattr(start_mqtt, "controller", None)
        if controller is not None and hasattr(controller, "reset_to_logged_out"):
            try:
                controller.reset_to_logged_out()
            except Exception:  # noqa: BLE001 - tray reset must never break logout
                logger.debug("tray reset_to_logged_out raised during stop_session; ignoring")

    return stop_session


class _LazyClientProxy:
    """A stand-in passed to :func:`make_quit_handler` on the token-less path.

    The live MQTT client does not exist at GUI-build time (it is built later by
    the deferred ``start_mqtt`` hook), so the Afsluiten handler is given this
    proxy: its :meth:`disconnect` forwards to ``start_mqtt.client`` if a session
    has started, and is a harmless no-op otherwise. This keeps the locked
    deadlock-safe quit order intact whether or not a session is live."""

    def __init__(self, start_mqtt):
        self._start_mqtt = start_mqtt

    def disconnect(self):
        client = getattr(self._start_mqtt, "client", None)
        if client is not None:
            client.disconnect()


def build_gui(
    *,
    now=time.monotonic,
    get_token=None,
    email=None,
    password=None,
    autostart_mod=autostart,
    settings=settings_module,
    webview=None,
    connect=None,
    sleep=None,
):
    """Compose the GUI/bridge/menu/controller WITHOUT a token (Plan 08-02).

    This is the token-less seam ``main()`` builds first: it wires the flyout, the
    real :class:`~src.session.SessionController`-backed bridge handlers, the
    deferred ``start_mqtt``/``stop_session`` hooks, the menu (autostart toggle +
    panel-driven re-login + Afsluiten), and the tray icon with the LOGGED-OUT
    neutral glyph. NO MQTT client is built and NO network thread is started here
    -- the network thread starts only once a token + serial exist, via the
    returned ``start_mqtt`` hook (deferred MQTT start). Returns the wired pieces
    incl. ``session``, ``start_mqtt``, ``stop_session``, ``relogin_handler``."""
    if sleep is None:
        sleep = time.sleep

    # Single source of truth shared across the network and UI threads.
    state = PrintState()
    shutdown_event = threading.Event()

    # --- bridge Api + single hidden flyout window (built token-less) ---------
    # The Api's state_provider needs the controller (built after the icon); a
    # late-bound holder fills it in once the controller exists.
    _holder = {"controller": None}

    def _state_provider():
        # The page seed (get_initial_state) opens LOGGED-OUT on start; the
        # SessionController's bootstrap_from_stored / login flow drives the real
        # logged-in state to the page via push_state/push_auth_step afterwards.
        ctrl = _holder["controller"]
        connection = (
            getattr(ctrl, "_status", ConnectionStatus.DISCONNECTED)
            if ctrl is not None
            else ConnectionStatus.DISCONNECTED
        )
        return bridge.serialize_state(
            state,
            connection,
            logged_in=False,
            theme=render.detect_windows_theme(),
        )

    flyout = FlyoutWindow(None, webview=webview)

    # The re-login routine the menu + the automatic 401 path converge on. On the
    # token-less path it is built before any client and before the session; the
    # client is late-bound by start_mqtt, the controller after the icon exists.
    if get_token is None:
        def get_token(*, force_relogin):
            return spike.get_access_token(
                email or "", password or "", force_relogin=force_relogin
            )
    relogin_handler = make_relogin_handler(
        None, shutdown_event, get_token=get_token, controller=None
    )

    # Deferred MQTT hooks: start_mqtt builds the client + network thread once a
    # token+serial exist; stop_session tears it down on logout.
    start_mqtt = make_start_mqtt(
        state=state,
        controller=None,  # late-bound below once the controller exists
        shutdown_event=shutdown_event,
        relogin_handler=relogin_handler,
        connect=connect,
        sleep=sleep,
        flyout=flyout,  # Plan 09-01: on_message pushes serialized state here
    )
    stop_session = make_stop_session(shutdown_event, start_mqtt)

    # The real SessionController behind the panel auth/select actions.
    session = make_session(
        flyout,
        start_mqtt=start_mqtt,
        stop_session=stop_session,
        auth=auth,
        token_store=token_store,
        settings=settings,
    )

    api = bridge.Api(
        handlers=make_bridge_handlers(flyout, session, start_mqtt=start_mqtt),
        state_provider=_state_provider,
    )
    flyout._api = api  # the js_api the window is created with (create() reads it)
    flyout.create()  # create the single hidden window (no-op start; just builds it)
    # Plan 09-01 Task 2: the SHOW path re-pushes the CURRENT serialized state so a
    # freshly opened panel is immediately correct. ``state`` is the shared
    # PrintState; the connection is read live off the late-bound controller; the
    # panel is "logged in" once an MQTT session has started (start_mqtt.started).
    def _toggle_connection():
        ctrl = _holder["controller"]
        return (
            getattr(ctrl, "_status", ConnectionStatus.DISCONNECTED)
            if ctrl is not None
            else ConnectionStatus.DISCONNECTED
        )

    flyout_toggle = make_flyout_toggle(
        flyout,
        state=state,
        connection_provider=_toggle_connection,
        logged_in=lambda: bool(getattr(start_mqtt, "started", False)),
        # LIVE printer name (set on the hook by select/bootstrap) so a reopened
        # progress panel shows the real name, not the generic "Printer".
        printer_name=lambda: getattr(start_mqtt, "printer_name", "") or "",
    )

    # The relogin MENU item now drives the PANEL login (no console prompt): it
    # clears the token + resets the panel to the login screen and shows it.
    menu_relogin = make_panel_relogin(session, flyout, relogin_handler)

    # Afsluiten destroys the window FIRST (unblocks webview.start on the main
    # thread) THEN tears down network + tray -- the locked deadlock-safe order.
    # The live client is built later, so the quit handler holds a lazy proxy.
    quit_handler = make_quit_handler(
        _LazyClientProxy(start_mqtt), shutdown_event, flyout=flyout
    )

    icon = pystray.Icon(
        "bambu-systray",
        # LOGGED-OUT neutral printer glyph until login completes (matches the
        # painted glyph so there's no brief legacy-dot flash before first paint).
        icon=render.render_printer_icon(0, "neutral", logged_out=True),
        title=render.tooltip_text(state),  # "Geen actieve print"
        menu=pystray.Menu(
            make_open_item(flyout_toggle),
            pystray.MenuItem(
                AUTOSTART_LABEL,
                make_autostart_toggle(autostart_mod),
                checked=lambda item: (
                    autostart.is_enabled()
                    if autostart_mod is autostart
                    else autostart_mod.is_enabled()
                ),
            ),
            pystray.MenuItem(RELOGIN_LABEL, menu_relogin),
            pystray.MenuItem(QUIT_LABEL, quit_handler),
        ),
    )

    controller = TrayController(icon, state, now=now)
    _holder["controller"] = controller
    relogin_handler.bind_controller(controller)
    # Late-bind the controller into the deferred MQTT hook (it needs the
    # controller for the on_message/on_connect wrappers + 401 status).
    start_mqtt.controller = controller

    return {
        "icon": icon,
        "controller": controller,
        "state": state,
        "shutdown_event": shutdown_event,
        "flyout": flyout,
        "api": api,
        "flyout_toggle": flyout_toggle,
        "session": session,
        "start_mqtt": start_mqtt,
        "stop_session": stop_session,
        "relogin_handler": relogin_handler,
    }


def make_panel_relogin(session, flyout, relogin_handler):
    """Build the 'Opnieuw verbinden / inloggen' menu callback (Plan 08-02).

    The re-login item now drives the PANEL login instead of a console prompt: it
    flags TOKEN_EXPIRED (via the shared relogin_handler's controller) so the tray
    shows "Opnieuw inloggen vereist", logs the session out (clears the token +
    stops any running session + resets the panel to the login step), and shows
    the flyout so the user can re-authenticate IN THE PANEL. It NEVER calls
    ``input()``/``getpass``/``spike.get_access_token`` (T-08-06/T-08-07)."""

    def _relogin(icon=None, item=None):
        # logout's stop_session resets the tray to DISCONNECTED; set TOKEN_EXPIRED
        # AFTER it so THIS explicit re-login entry point shows "Opnieuw inloggen
        # vereist" rather than "Verbinden…" (the pump coalesces to the latest).
        session.logout()  # clear_token + stop_session + logged-out push
        controller = getattr(relogin_handler, "_controller", None)
        if controller is not None:
            controller.set_connection_status(ConnectionStatus.TOKEN_EXPIRED)
        flyout.push_auth_step("login")
        flyout.show()

    return _relogin


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
    webview=None,
):
    """Compose the whole app from an already-acquired ``token`` and return the
    wired pieces WITHOUT starting any thread or launching a real tray.

    Returns a dict with ``icon``, ``controller``, ``client``, ``state``,
    ``network_runner`` (a zero-arg callable that runs the MQTT session) and
    ``shutdown_event``. This is the injectable seam the tests drive with fakes:
    they monkeypatch ``auth.*`` / ``mqtt_client.build_client`` so no real broker,
    login, or tray is ever touched. The token is never logged.

    Plan 08-02: ``build_app`` is now the EAGER (token-known) convenience path on
    top of the token-less :func:`build_gui` + deferred :func:`make_start_mqtt`
    seam. It builds the GUI/bridge/menu via ``build_gui`` (real
    SessionController-backed handlers), derives + persists the serial from the
    token, and eagerly wires the MQTT client + ``network_runner`` (still WITHOUT
    starting a thread). The menu's re-login item drives the PANEL login (no
    console prompt); the automatic 401 path still uses the shared
    ``_ReloginHandler``. The token is never logged.

    ``now`` is the injectable monotonic clock threaded into the TrayController so
    the freshness/offline watcher is driveable in tests with no real waits.
    ``get_token`` is the injectable re-login closure (defaults to a
    ``spike.get_access_token`` binding) so the automatic 401 re-login path runs
    without real stdin/network in tests.
    """
    if sleep is None:
        sleep = time.sleep

    # Build the token-less GUI/bridge/menu/controller + deferred MQTT hooks.
    gui = build_gui(
        now=now,
        get_token=get_token,
        email=email,
        password=password,
        autostart_mod=autostart_mod,
        settings=settings,
        webview=webview,
        connect=connect,
        sleep=sleep,
    )
    controller = gui["controller"]
    state = gui["state"]
    shutdown_event = gui["shutdown_event"]
    relogin_handler = gui["relogin_handler"]

    # Derive MQTT identity + serial exactly as spike.run_with_relogin does.
    devices = auth.get_device_list(token)
    serial = auth.pick_serial(devices)
    logger.info("Using printer serial (dev_id): %s", serial)

    # Remember the picked serial across restarts (APP-03). save_settings writes an
    # allowlist (region/serial) so the token is structurally never persisted here
    # (T-04-01); the existing region is preserved by loading first.
    persist_serial(serial, settings=settings)

    # Eagerly wire the MQTT client + network_runner from (token, serial) using the
    # SAME helper the deferred start_mqtt hook uses -- but DO NOT start a thread.
    client, network_runner = _wire_client(
        token,
        serial,
        state=state,
        controller=controller,
        shutdown_event=shutdown_event,
        relogin_handler=relogin_handler,
        connect=connect,
        sleep=sleep,
        flyout=gui["flyout"],  # Plan 09-01: live-push serialized state to panel
    )
    # Bind the live client into the relogin routine + the deferred start_mqtt hook
    # so the menu/401 disconnect targets it and start_mqtt won't build a second.
    relogin_handler.bind_client(client)
    gui["start_mqtt"].client = client
    gui["start_mqtt"].serial = serial  # capture active serial for control (09-02)
    gui["start_mqtt"].started = True  # eager path owns the session already

    return {
        "icon": gui["icon"],
        "controller": controller,
        "client": client,
        "state": state,
        "network_runner": network_runner,
        "shutdown_event": shutdown_event,
        "flyout": gui["flyout"],
        "api": gui["api"],
        "flyout_toggle": gui["flyout_toggle"],
        "session": gui["session"],
        "start_mqtt": gui["start_mqtt"],
        "stop_session": gui["stop_session"],
        "relogin_handler": relogin_handler,
    }


def _configure_logging(debug: bool) -> None:
    """Console logging with a clean formatter; ``--debug`` toggles DEBUG."""
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


def _import_webview():
    """Lazily import the real pywebview module.

    Kept out of module import so ``import src.app`` needs no GUI backend (the test
    suite stays importable headless). Only ``main()`` calls this, so a missing
    backend never breaks importing the package."""
    import webview

    return webview


def main(argv=None, *, guard=None, webview=None) -> int:
    """Build the panel-driven app and run the v2 flyout shell until 'Afsluiten'.

    Plan 08-02 (LOCKED, 08-CONTEXT.md "App bootstrap refactor"): main() NO LONGER
    prompts the console for credentials -- no stdin credential read remains here.
    Instead it builds the token-less GUI + the real SessionController-backed
    bridge (:func:`build_gui`), paints the LOGGED-OUT neutral glyph, then runs
    ``session.bootstrap_from_stored()``: a stored token + remembered serial that
    validates goes straight to logged-in (MQTT started via the deferred
    ``start_mqtt`` hook); a rejected/absent token falls to the login screen IN
    THE PANEL. The flyout is shown on start so the login screen is visible when
    logged out, and the right-click "Opnieuw verbinden / inloggen" item drives the
    panel login (not a console prompt).

    Before anything else a single-instance guard runs: a second launch logs one
    line and ``return 0`` cleanly WITHOUT touching the tray or MQTT (threat
    T-04-06). ``guard`` is injectable so tests drive the already-running path.

    THREADING (v2, LOCKED -- 07-CONTEXT.md): any MQTT session runs on a daemon
    thread (started ONLY post-login/post-select by ``start_mqtt``); the pystray
    tray runs via ``icon.run_detached()`` on its OWN thread; and the MAIN thread
    runs ``webview.start()`` (the GUI loop), which blocks until the flyout is
    destroyed. 'Afsluiten' destroys the window FIRST so ``webview.start()``
    returns; we then signal shutdown and stop the tray icon -- no orphan icon.
    ``webview`` is injectable so tests drive the whole loop with a FakeWebview (no
    real GUI). No secret is logged.
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

    logger.info("Bambu Lab systray -- starting. Left-click the tray icon to open; right-click -> Afsluiten to quit.")

    # Load persisted settings (region/serial) so a remembered serial is available;
    # defaults are used on a missing/corrupt file. The SessionController's
    # bootstrap reads these to decide logged-in vs login.
    settings = settings_module.load_settings()
    logger.debug("Loaded settings (serial remembered: %s)", settings.get("serial") is not None)

    # The real pywebview backend is imported lazily (the test suite injects a
    # FakeWebview). No login happens before this -- the panel drives auth now.
    if webview is None:
        webview = _import_webview()

    # Build the token-less GUI/bridge/menu + the real SessionController-backed
    # handlers and deferred MQTT hooks. NO token is required to construct this.
    gui = build_gui(webview=webview)
    icon = gui["icon"]
    controller = gui["controller"]
    shutdown_event = gui["shutdown_event"]
    flyout = gui["flyout"]
    session = gui["session"]

    # Start LOGGED-OUT: the tray shows the neutral printer glyph until a login
    # completes (bootstrap flips it when a stored session validates). This is the
    # SAME glyph the pump paints, so there's no brief legacy-dot flash.
    icon.icon = render.render_printer_icon(0, "neutral", logged_out=True)

    # Defer the bootstrap + initial show to the window's DOM-loaded event. Both
    # push to the page (push_auth_step / evaluate_js) and SHOW the window, which
    # is only safe AFTER webview.start() is running and the page DOM is ready --
    # calling them eagerly here raises "Main window failed to start" (the v2
    # startup crash). pywebview fires window.events.loaded at exactly that point.
    def _on_window_loaded():
        # Runs after the GUI loop is up and the page DOM is loaded, so
        # bootstrap_from_stored's push_* and flyout.show() are now safe.
        # Decide logged-in vs login screen from the stored token + serial: a
        # validated stored session starts MQTT (via start_mqtt) and opens on
        # progress; a rejected/absent token resets the panel to the login screen.
        session.bootstrap_from_stored()
        # Show the flyout so the login screen is visible when logged out (the
        # user logs in entirely in the panel -- no console prompt).
        flyout.show()

    flyout.on_loaded(_on_window_loaded)

    # v2 threading inversion: the tray runs on its OWN thread (run_detached) so
    # the MAIN thread is free to run webview.start(). make_setup paints the first
    # frame AND starts the single UI pump thread when the icon becomes visible.
    icon.run_detached(setup=make_setup(controller, shutdown_event))

    # The MAIN thread now enters the GUI loop and blocks until the flyout window
    # is destroyed (the Afsluiten handler calls flyout.destroy() FIRST).
    webview.start()

    # webview.start() returned -> the window was destroyed (Afsluiten). Tear down
    # the rest defensively so a teardown error never leaves an orphan tray icon.
    shutdown_event.set()
    try:
        icon.stop()  # idempotent: if Afsluiten already stopped it this is a no-op
    except Exception:  # noqa: BLE001 - never raise out of the clean-exit path
        logger.debug("icon.stop() raised during final teardown; ignoring")
    logger.info("Stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
