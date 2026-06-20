"""spike.py: the CLI entry point that wires auth -> token store -> MQTT -> console.

This is the orchestration layer for Phase 1's cloud-connection spike. It glues
together the four already-built foundation modules (``auth``, ``token_store``,
``state``, ``mqtt_client``) into a runnable console program and is the only place
the verifyCode email-code branch, the token reuse/re-login flow, and the live
status line live. Everything it orchestrates is unit-tested here with the
network fully mocked; the actual *empirical* validation (real login + a live
multi-hour print to confirm ``mc_remaining_time`` is in MINUTES) is the
human-verify gate that follows -- see the plan SUMMARY.

Login flow (RESEARCH Code Examples > spike.py sketch):

    tok = token_store.load_token()
    if tok and not force_relogin: reuse it
    else:
        resp = auth.login(email, password)
        accessToken non-empty            -> use it
        loginType == "verifyCode"        -> request_email_code -> prompt 6-digit
                                            code (validated) -> login_with_code
        loginType == "tfa"               -> SystemExit (out of spike scope)
        anything else                    -> SystemExit (unexpected response)
        token_store.save_token(tok)

401/auth-failed on the MQTT connect clears the stored token and re-runs
``get_access_token(force_relogin=True)`` -- there is NO silent refresh, because
the Bambu refresh endpoint is dead (RESEARCH Pitfall 5).

Security (threat register T-01-12..16): the password is read with ``getpass``
(no echo) and held only in memory; the token, password, MQTT password and
``Authorization`` header are NEVER logged. Only ``gcode_state`` / ``mc_percent``
/ ``mc_remaining_time`` appear in the status line.
"""

import argparse
import getpass
import logging
import uuid

from src import auth, mqtt_client, token_store
from src.state import PrintState, hmm

logger = logging.getLogger("spike")

# MQTT reason codes that mean "your token was rejected" -> clear + re-login.
# paho v2 surfaces "Not authorized" / "Bad user name or password" on auth fail.
_AUTH_FAILED_HINTS = ("not authorized", "bad user name or password", "unauthorized")


def get_access_token(
    email: str,
    password: str,
    *,
    prompt=input,
    force_relogin: bool = False,
) -> str:
    """Return a usable access token, logging in only when necessary.

    Reuses the DPAPI-stored token unless ``force_relogin`` is set (the 401
    fallback forces a fresh login). On a fresh login it branches on the login
    response: a non-empty ``accessToken`` is used directly; ``loginType ==
    "verifyCode"`` triggers the email-code branch (request the code, then prompt
    for a 6-digit code -- validated and re-prompted on bad input -- and exchange
    it); ``loginType == "tfa"`` and any unexpected response raise ``SystemExit``.
    The obtained token is persisted via ``token_store.save_token``.

    ``prompt`` is injectable so the 6-digit-code entry can be driven in tests
    without real stdin. The token is never logged.
    """
    if not force_relogin:
        tok = token_store.load_token()
        if tok:
            logger.info("Reusing stored access token (no login needed).")
            return tok

    logger.info("Logging in to the Bambu cloud as %s ...", email)
    resp = auth.login(email, password)

    if resp.get("accessToken"):
        tok = resp["accessToken"]
    elif resp.get("loginType") == "verifyCode":
        logger.info("Account requires an email verification code; requesting it now.")
        auth.request_email_code(email)
        code = _prompt_for_code(prompt)
        tok = login_with_validated_code(email, code)
    elif resp.get("loginType") == "tfa":
        raise SystemExit(
            "2FA account detected (loginType=tfa) -- out of spike scope. "
            "Disable 2FA on the account or extend the tfa branch."
        )
    else:
        raise SystemExit(
            f"Unexpected login response (loginType={resp.get('loginType')!r}); "
            "no accessToken and not a known branch."
        )

    token_store.save_token(tok)
    logger.info("Login succeeded; token stored in the OS credential manager.")
    return tok


def login_with_validated_code(email: str, code: str) -> str:
    """Exchange an already-validated 6-digit ``code`` for an access token."""
    resp = auth.login_with_code(email, code)
    tok = resp.get("accessToken")
    if not tok:
        raise SystemExit("verifyCode login returned no accessToken.")
    return tok


def _is_valid_code(code: str) -> bool:
    """A login code is exactly 6 ASCII digits (RESEARCH Security V5 / T-01-16)."""
    return len(code) == 6 and code.isdigit()


def _prompt_for_code(prompt) -> str:
    """Prompt for the 6-digit email code, re-prompting until it is valid.

    Validation happens BEFORE the code is sent to ``login_with_code`` so a
    malformed entry never hits the network.
    """
    while True:
        code = prompt("Enter the 6-digit code emailed to you: ").strip()
        if _is_valid_code(code):
            return code
        logger.warning("Code must be exactly 6 digits. Please try again.")


def log_status(state: PrintState) -> None:
    """Emit the structured, MINUTES-annotated status line for one update.

    Shows the raw ``mc_remaining_time`` AND its ``h:mm`` interpretation so the
    user can compare against the printer's on-screen estimate and confirm the
    unit is MINUTES (a 60x error would otherwise be invisible -- Pitfall 1).
    No secret is referenced here.
    """
    logger.info(
        "state=%-8s percent=%3d%% remaining_raw=%s -> %s  "
        "[compare h:mm to printer's on-screen estimate to confirm MINUTES]",
        state.gcode_state,
        state.mc_percent,
        state.mc_remaining_time,
        hmm(state.mc_remaining_time),
    )


class _StatusReporter:
    """on_message wrapper that delta-merges, logs each update, and records
    every DISTINCT raw ``gcode_state`` string seen.

    Phase 3's state machine must match the printer's exact emitted casing, so we
    capture the live set of raw ``gcode_state`` values here (CONTEXT idea).
    """

    def __init__(self, state: PrintState):
        self.state = state
        self.seen_gcode_states: set[str] = set()

    def on_message(self, client, userdata, msg):
        # Reuse the vetted, JSON/`print`-guarded merge from mqtt_client, then
        # log the merged state and capture the raw gcode_state.
        mqtt_client.on_message(client, userdata, msg)
        gcode_state = self.state.gcode_state
        if gcode_state not in self.seen_gcode_states:
            self.seen_gcode_states.add(gcode_state)
            logger.info("New raw gcode_state observed: %r", gcode_state)
        log_status(self.state)


def _auth_failed(exc: Exception) -> bool:
    """True if an exception looks like a token/auth rejection (401-equivalent)."""
    text = str(exc).lower()
    return any(hint in text for hint in _AUTH_FAILED_HINTS)


def run_with_relogin(email, password, *, prompt=input, connect=None, sleep=None):
    """Drive a single MQTT session, re-logging in once on an auth failure.

    Builds the client from a fresh-or-stored token and runs the single
    long-lived session via ``mqtt_client.run_session``. If the session fails
    because the token was rejected, the stored token is cleared and a full
    re-login is performed (``force_relogin=True``) exactly once, then the client
    is rebuilt and the session retried. No silent refresh (Pitfall 5).

    ``connect``/``sleep`` are injected straight through to ``run_session`` so the
    whole path is testable without sockets or real waits.
    """
    import time as _time

    if sleep is None:
        sleep = _time.sleep

    force_relogin = False
    while True:
        token = get_access_token(email, password, prompt=prompt, force_relogin=force_relogin)
        username = auth.mqtt_username_from_token(token)
        devices = auth.get_device_list(token)
        logger.info("Bound devices on this account: %s", _redact_devices(devices))
        serial = auth.pick_serial(devices)
        logger.info("Using printer serial (dev_id): %s", serial)

        state = PrintState()
        reporter = _StatusReporter(state)
        client = mqtt_client.build_client(
            client_id=f"bambu-systray-{uuid.uuid4()}",
            username=username,
            access_token=token,
        )
        client.user_data_set({"serial": serial, "state": state})
        client.on_connect = mqtt_client.on_connect
        client.on_message = reporter.on_message
        client.on_disconnect = mqtt_client.on_disconnect

        if connect is None:
            connect = _default_connect

        try:
            mqtt_client.run_session(client, connect=connect, sleep=sleep)
            return  # clean exit (StopSession) -> done
        except _AuthFailed:
            logger.warning("Token rejected by the broker; clearing it and re-logging in.")
            token_store.clear_token()
            force_relogin = True
            continue


class _AuthFailed(Exception):
    """Internal signal that the broker rejected the token (drives re-login)."""


def _default_connect(client):
    """Connect + run the live session; raise ``_AuthFailed`` on auth rejection."""
    try:
        client.connect(mqtt_client.BROKER_HOST, mqtt_client.PORT, keepalive=mqtt_client.KEEPALIVE)
    except Exception as exc:  # noqa: BLE001 - inspect for auth failure vs transient
        if _auth_failed(exc):
            raise _AuthFailed(str(exc)) from exc
        raise
    client.loop_forever()


def _redact_devices(devices: list[dict]) -> list[dict]:
    """Log-safe device view: drop the LAN access code, keep id/name/model.

    The full list is shown (CONTEXT) but the ``dev_access_code`` LAN secret is
    stripped so it never lands in a log.
    """
    safe = []
    for dev in devices:
        safe.append(
            {
                "dev_id": dev.get("dev_id"),
                "name": dev.get("name"),
                "online": dev.get("online"),
                "dev_product_name": dev.get("dev_product_name"),
            }
        )
    return safe


def _configure_logging(debug: bool) -> None:
    """Console logging with a clean formatter; ``--debug`` toggles DEBUG."""
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


def main(argv=None) -> int:
    """CLI: prompt for credentials, then run the live cloud stream to console."""
    parser = argparse.ArgumentParser(description="Bambu cloud connection spike (console).")
    parser.add_argument("--debug", action="store_true", help="enable DEBUG logging")
    args = parser.parse_args(argv)
    _configure_logging(args.debug)

    logger.info("Bambu Lab cloud spike -- console status stream. Ctrl-C to stop.")
    email = input("Bambu account email: ").strip()
    # getpass: no echo, in-memory only, never persisted or logged (T-01-12).
    password = getpass.getpass("Bambu account password: ")

    try:
        run_with_relogin(email, password)
    except KeyboardInterrupt:
        logger.info("Stopped by user (Ctrl-C).")
        return 0
    except SystemExit as exc:
        logger.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
