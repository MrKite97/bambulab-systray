"""SessionController: the panel-driven auth + printer-select orchestration brain.

This is the dependency-injected controller Phase 8 needs and the seam Phase 9
binds live progress onto. It drives the whole flow -- login -> emailed 2FA code
(with resend) -> enriched device list -> select -> start session -> logout -- by
delegating to INJECTED ``auth``, ``token_store``, ``settings``, ``flyout``, and
two callables ``start_mqtt(token, serial)`` / ``stop_session()``. Every blocking
auth/device network call runs on a WORKER thread (``run_async``, injectable so
tests run the body inline); results reach the page ONLY through ``flyout.push_*``
(08-CONTEXT.md "Threading" + "Testability" LOCKED). No GUI, no stdin here.

Security (threat register T-08-01..05):
- The password, the 6-digit code, and the access token are NEVER logged and NEVER
  placed into any ``push_state`` / ``push_error`` / ``push_devices`` /
  ``push_auth_step`` payload. Only ``_device_rows`` (4 display fields) and
  ``serialize_state`` output (secret-free by construction) cross to the page.
- ``submit_code`` validates 6 ASCII digits BEFORE any network call (T-08-03).
- Worker bodies catch and surface errors via ``push_error`` -- an auth failure is
  never silently dropped (T-08-05).
- ``logout`` and the 401 path both ``clear_token`` and reset to login so a stale
  credential is never reused (T-08-04).
"""

import logging
import threading

import requests

from src.bridge import serialize_state
from src.state import PrintState
from src.status import ConnectionStatus

logger = logging.getLogger("session")

# Dutch panel-consistent error copy (no secret ever interpolated).
_ERR_LOGIN = "Inloggen mislukt. Controleer je e-mailadres en wachtwoord."
_ERR_CODE = "Verificatie mislukt. Controleer de code en probeer opnieuw."
_ERR_CODE_FORMAT = "Voer een 6-cijferige code in."
_ERR_CONNECT = "Verbinden met Bambu is mislukt. Probeer het opnieuw."
_ERR_SESSION_EXPIRED = "Je sessie is verlopen. Log opnieuw in."

# Panel row status presentation (panel.html consumes statusColor/statusLabel).
_STATUS_ONLINE = ("Online", "var(--status-done)")
_STATUS_OFFLINE = ("Offline", "var(--status-offline)")


def _default_run_async(fn):
    """Run a worker body on a daemon thread (production default)."""
    threading.Thread(target=fn, daemon=True).start()


def _is_valid_code(code: str) -> bool:
    """A login code is exactly 6 ASCII digits (mirrors spike._is_valid_code)."""
    return isinstance(code, str) and len(code) == 6 and code.isdigit()


def _is_unauthorized(exc: Exception) -> bool:
    """True if ``exc`` looks like a 401 token rejection.

    Checks for an HTTP 401 status on a ``requests`` error and falls back to a
    text heuristic (the spike ``_auth_failed`` style) so a non-requests caller's
    "unauthorized" error is still recognized.
    """
    response = getattr(exc, "response", None)
    if response is not None and getattr(response, "status_code", None) == 401:
        return True
    text = str(exc).lower()
    return "401" in text or "unauthorized" in text


class SessionController:
    """Orchestrates the panel login/code/select/logout flow with injected deps.

    All public methods are bridge-handler targets (exact names match the
    ``bridge.Api`` actions): ``login_submit``, ``submit_code``, ``resend_code``,
    ``select_printer``, ``logout``, plus ``bootstrap_from_stored`` for app start.
    The pending account email and the in-flight token are held in memory only --
    never persisted, never logged.
    """

    def __init__(
        self,
        *,
        auth,
        token_store,
        settings,
        flyout,
        start_mqtt,
        stop_session,
        run_async=_default_run_async,
    ):
        self.auth = auth
        self.token_store = token_store
        self.settings = settings
        self.flyout = flyout
        self.start_mqtt = start_mqtt
        self.stop_session = stop_session
        self._run_async = run_async
        # In-memory only (never persisted, never logged).
        self._account = None
        self._token = None
        self._devices = []  # last enriched rows, for printer_name lookup

    # --- page actions (page -> controller) -------------------------------- #

    def login_submit(self, email, password):
        """Drive ``auth.login`` on a worker; branch verifyCode vs direct token.

        verifyCode -> request the email code + advance to the "code" step,
        remembering the pending account. A direct ``accessToken`` -> straight to
        the logged-in bootstrap. Any other shape or exception -> push_error,
        stay on login. The password is never logged or pushed.
        """

        def work():
            try:
                resp = self.auth.login(email, password)
            except Exception:  # noqa: BLE001 - surface, never swallow (T-08-05)
                logger.warning("Login request failed.")
                self.flyout.push_error(_ERR_LOGIN)
                return
            token = resp.get("accessToken")
            if token:
                self._enter_logged_in_sync(token)
                return
            if resp.get("loginType") == "verifyCode":
                try:
                    self.auth.request_email_code(email)
                except Exception:  # noqa: BLE001
                    logger.warning("Requesting the email code failed.")
                    self.flyout.push_error(_ERR_CONNECT)
                    return
                self._account = email  # remember pending account (memory only)
                self.flyout.push_auth_step("code")
                return
            logger.warning("Unexpected login response shape.")
            self.flyout.push_error(_ERR_LOGIN)

        self._run_async(work)

    def submit_code(self, code):
        """Validate 6 digits, then exchange the code for a token + save it.

        Invalid format is rejected BEFORE any network call (T-08-03). On success
        the token is stored and the logged-in bootstrap runs. The code is never
        logged or pushed.
        """
        if not _is_valid_code(code):
            self.flyout.push_error(_ERR_CODE_FORMAT)
            return

        def work():
            try:
                resp = self.auth.login_with_code(self._account, code)
            except Exception:  # noqa: BLE001
                logger.warning("Code verification failed.")
                self.flyout.push_error(_ERR_CODE)
                return
            token = resp.get("accessToken")
            if not token:
                logger.warning("Code verification returned no token.")
                self.flyout.push_error(_ERR_CODE)
                return
            self.token_store.save_token(token)
            self._enter_logged_in_sync(token)

        self._run_async(work)

    def resend_code(self):
        """Re-trigger the verifyCode email for the remembered account."""

        def work():
            if not self._account:
                self.flyout.push_error(_ERR_CONNECT)
                return
            try:
                self.auth.request_email_code(self._account)
            except Exception:  # noqa: BLE001
                logger.warning("Resending the email code failed.")
                self.flyout.push_error(_ERR_CONNECT)

        self._run_async(work)

    def logout(self):
        """Clear the token, stop the running session, reset the panel to login.

        No stale credential is left behind (T-08-04). Synchronous: these are
        local/in-memory operations, not network calls.

        Pushes a fully logged-OUT state (``logged_in=False``, ``auth_step=login``)
        rather than only ``push_auth_step("login")``: the page keeps its own
        ``loggedIn`` flag and, while it is true, forces the progress screen and
        ignores a bare auth-step push -- so logout from the progress/select
        screen appeared to "do nothing". Pushing the state resets that flag and
        lands the panel on the login screen.
        """
        self.token_store.clear_token()
        self._account = None
        self._token = None
        self._devices = []
        self.stop_session()
        self.flyout.push_state(
            serialize_state(
                PrintState(),
                ConnectionStatus.DISCONNECTED,
                logged_in=False,
                auth_step="login",
            )
        )

    def select_printer(self, device_id):
        """Persist the serial (region preserved), then start the live session.

        Guards a missing token (-> back to login). Persists via
        ``settings.save_settings`` preserving the existing region, invokes the
        injected ``start_mqtt(token, serial)``, then pushes the logged-in
        progress state. The token is never placed into a push payload.
        """
        if not self._token:
            self.flyout.push_auth_step("login")
            return

        def work():
            name = self._printer_name_for(device_id)
            try:
                current = self.settings.load_settings()
                self.settings.save_settings({**current, "serial": device_id})
                # Set the live printer name BEFORE start_mqtt so the ongoing
                # report pushes (on_message) carry it -- otherwise the progress
                # header reverts to the generic "Printer" on the next report.
                self._set_active_printer_name(name)
                self.start_mqtt(self._token, device_id)
            except Exception:  # noqa: BLE001
                logger.warning("Starting the session failed.")
                self.flyout.push_error(_ERR_CONNECT)
                return
            self.flyout.push_state(
                serialize_state(
                    PrintState(),
                    ConnectionStatus.DISCONNECTED,
                    logged_in=True,
                    auth_step="select",
                    printer_name=name,
                )
            )

        self._run_async(work)

    def open_printer_select(self):
        """Gear button: re-fetch the bound device list and show the select screen.

        Reuses the held token to GET the device list on a WORKER thread, enriches
        it to display rows, pushes them (``push_devices``) and advances the panel
        to the select step (``push_auth_step("select")``) -- the same pushes the
        login flow uses, so the page renders the printer list identically. A 401
        clears the token and resets to login (T-08-04); a non-401 error surfaces
        via ``push_error``. No token is missing-guarded -> back to login."""
        if not self._token:
            self.flyout.push_auth_step("login")
            return

        def work():
            try:
                devices = self.auth.get_device_list(self._token)
            except Exception as exc:  # noqa: BLE001
                if _is_unauthorized(exc):
                    self.token_store.clear_token()
                    self._token = None
                    self.flyout.push_error(_ERR_SESSION_EXPIRED)
                    self.flyout.push_auth_step("login")
                    return
                logger.warning("Fetching the device list failed.")
                self.flyout.push_error(_ERR_CONNECT)
                return
            enriched = self.auth.enrich_devices(devices)
            self._devices = self._device_rows(enriched)
            self.flyout.push_devices(self._devices)
            self.flyout.push_auth_step("select")

        self._run_async(work)

    # --- app-start bootstrap ---------------------------------------------- #

    def bootstrap_from_stored(self):
        """App-start decision tree: validate a stored token+serial or go to login.

        Returns ``True`` if a stored session was found and a (worker) validation
        was scheduled (caller may render the logged-in tray glyph), ``False`` if
        no token/serial was stored (caller renders logged-out). The token is
        never returned or exposed. Consumed by ``app.main`` in Plan 02.
        """
        token = self.token_store.load_token()
        serial = self.settings.load_settings().get("serial")
        if not token or not serial:
            self.flyout.push_auth_step("login")
            return False

        def work():
            try:
                devices = self.auth.get_device_list(token)
            except Exception as exc:  # noqa: BLE001
                if _is_unauthorized(exc):
                    self.token_store.clear_token()
                    self.flyout.push_auth_step("login")
                    return
                logger.warning("Validating the stored session failed.")
                self.flyout.push_error(_ERR_CONNECT)
                return
            self._token = token
            # Resolve the stored serial's display name so the tray + progress
            # header show the real printer name, not the generic "Printer".
            self._devices = self._device_rows(self.auth.enrich_devices(devices))
            name = self._printer_name_for(serial)
            self._set_active_printer_name(name)
            self.start_mqtt(token, serial)
            self.flyout.push_state(
                serialize_state(
                    PrintState(),
                    ConnectionStatus.DISCONNECTED,
                    logged_in=True,
                    auth_step="select",
                    printer_name=name,
                )
            )

        self._run_async(work)
        return True

    # --- internal helpers (run on the worker, no extra threading) --------- #

    def _enter_logged_in_sync(self, token):
        """Hold the token, fetch+enrich devices, push the select screen.

        Already runs inside a worker body (login_submit / submit_code), so it
        does NOT spawn another thread. A 401 clears the token and resets to login
        (T-08-04); a non-401 error surfaces via push_error. The token is held in
        memory only and never pushed.
        """
        self._token = token
        try:
            devices = self.auth.get_device_list(token)
        except Exception as exc:  # noqa: BLE001
            if _is_unauthorized(exc):
                self.token_store.clear_token()
                self._token = None
                self.flyout.push_error(_ERR_SESSION_EXPIRED)
                self.flyout.push_auth_step("login")
                return
            logger.warning("Fetching the device list failed.")
            self.flyout.push_error(_ERR_CONNECT)
            return
        enriched = self.auth.enrich_devices(devices)
        self._devices = self._device_rows(enriched)
        self.flyout.push_devices(self._devices)
        self.flyout.push_auth_step("select")

    def _device_rows(self, enriched):
        """Map enriched ``{dev_id,name,dev_model_name,online}`` -> panel rows.

        Output keys match panel.html's ``applyDevices`` contract:
        ``{id, name, model, statusLabel, statusColor}``. No secret crosses here --
        only the four display fields. Input order is preserved.
        """
        rows = []
        for d in enriched:
            label, color = _STATUS_ONLINE if d.get("online") else _STATUS_OFFLINE
            rows.append(
                {
                    "id": d.get("dev_id"),
                    "name": d.get("name", ""),
                    "model": d.get("dev_model_name", ""),
                    "statusLabel": label,
                    "statusColor": color,
                }
            )
        return rows

    def _printer_name_for(self, device_id):
        """Look up a display name for ``device_id`` from the last device rows."""
        for row in self._devices:
            if row.get("id") == device_id:
                return row.get("name") or ""
        return ""

    def _set_active_printer_name(self, name):
        """Record the active printer's display name on the start_mqtt hook so the
        live report pushes (on_message) and the tray-toggle re-push render the
        real name instead of the generic "Printer". Best-effort: the hook is a
        plain attribute holder; never raise into the auth flow."""
        try:
            self.start_mqtt.printer_name = name or ""
        except Exception:  # noqa: BLE001 - cosmetic; must not break login/select
            pass
