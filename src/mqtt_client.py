"""mqtt_client: the single long-lived TLS MQTT session to the Bambu cloud.

This module is a faithful port of the verified pybambu cloud-MQTT flow (RESEARCH
Pattern 4 + 7, 2026-06-20) adapted from paho-mqtt 1.x to **2.x VERSION2**:

  * ``build_client`` constructs ``mqtt.Client(CallbackAPIVersion.VERSION2, ...)``
    with certificate-verified TLS (``tls_set()`` + ``tls_insecure_set(False)``).
  * ``on_connect`` subscribes to ``device/<serial>/report`` and publishes the
    ``pushall`` command to ``device/<serial>/request`` on every (re)connect --
    P1/A1 printers only send a full snapshot in response to pushall.
  * ``on_message`` json-decodes the report and delta-merges the ``print`` object
    into the shared ``state.PrintState`` (missing field == unchanged, never 0).
  * ``on_disconnect`` is intentionally inert: it does NOT reconnect. The backoff
    loop (``run_session``) owns reconnection so we never reconnect-storm.
  * ``backoff_delay`` / ``run_session`` implement the locked exponential backoff
    schedule 5->10->30->60s cap WITH jitter, on a single long-lived session.

Security (threat register T-01-08/10/11):
  * TLS verification is ON and MUST stay on -- never disable certificate
    verification (that exposes the access token, used as the MQTT password, to
    MITM). A sudden TLS failure means Bambu rotated a cert, not a reason to
    disable verification.
  * The access token / MQTT password is never logged.
  * ``on_message`` guards malformed JSON and a missing ``print`` key so untrusted
    broker input cannot crash the paho network thread.
"""

import json
import logging
import random

import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)

# --- Connection constants (RESEARCH Pattern 4, verified vs pybambu) -----------
BROKER_HOST = "us.mqtt.bambulab.com"
PORT = 8883
KEEPALIVE = 30  # seconds; sane balance between chatty pings and drop detection

# Exact pushall command payload (pybambu commands.py). Forces a full status push.
PUSH_ALL = {"pushing": {"sequence_id": "0", "command": "pushall"}}


def build_client(client_id: str, username: str, access_token: str) -> mqtt.Client:
    """Build a paho-mqtt 2.x VERSION2 client with certificate-verified TLS.

    ``username`` is the JWT ``username`` claim (``u_<digits>``); ``access_token``
    is the MQTT password. Neither is logged. Returns an unconnected client; the
    caller wires callbacks/userdata and drives it via ``run_session``.
    """
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=client_id,  # unique + stable, e.g. f"bambu-systray-{uuid4()}"
        protocol=mqtt.MQTTv311,
    )
    client.username_pw_set(username, password=access_token)
    client.tls_set()  # default: system CA store, certificate verification ON
    # Keep verification ON: disabling it would allow a MITM of the access token
    # (the MQTT password). A TLS failure means a rotated cert, not user error.
    client.tls_insecure_set(False)
    return client


def on_connect(client, userdata, flags, reason_code, properties):
    """paho v2 on_connect: subscribe to the report topic, publish pushall.

    Publishing pushall on every (re)connect (at most once per connect) is the
    P1/A1-mandatory way to get a full status snapshot. ``serial`` comes from
    userdata so the single module-level callbacks stay device-agnostic.
    """
    serial = userdata["serial"]
    client.subscribe(f"device/{serial}/report")
    client.publish(f"device/{serial}/request", json.dumps(PUSH_ALL))
    logger.debug("MQTT connected (rc=%s); subscribed + pushall sent", reason_code)


def on_message(client, userdata, msg):
    """paho v2 on_message: delta-merge the report's ``print`` object into state.

    Guards malformed JSON and a missing ``print`` key -- untrusted broker input
    must not crash the network thread (T-01-11).
    """
    try:
        payload = json.loads(msg.payload)
    except (ValueError, TypeError):
        logger.debug("Ignoring non-JSON MQTT payload")
        return
    if not isinstance(payload, dict):
        return
    print_obj = payload.get("print")
    if print_obj:
        userdata["state"].merge(print_obj)


def on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
    """paho v2 on_disconnect: INERT by design.

    Reconnection is owned exclusively by ``run_session``'s backoff loop. Calling
    connect/reconnect here would cause a reconnect storm and risks the
    single-session printer-reboot pitfall (T-01-09). Do not add a reconnect here.
    """
    logger.debug("MQTT disconnected (rc=%s); backoff loop will reconnect", reason_code)
    return


# --- Reconnect: exponential backoff + jitter, single long-lived session -------
BACKOFF_SCHEDULE = [5, 10, 30, 60]  # seconds; last value is the cap


def backoff_delay(attempt: int) -> float:
    """Delay before the next reconnect: schedule base + 0..50% jitter.

    The attempt index is clamped to the schedule length so anything beyond the
    table reuses the 60s cap base. Jitter spreads reconnects to avoid a
    thundering herd against the broker.
    """
    base = BACKOFF_SCHEDULE[min(attempt, len(BACKOFF_SCHEDULE) - 1)]
    return base + random.uniform(0, base * 0.5)


class StopSession(Exception):
    """Raised by an injected ``connect`` to break out of ``run_session``."""


def run_session(client, *, connect, sleep, record_attempt=None):
    """Single-session reconnect loop with exponential backoff + jitter.

    One stable client (one ``client_id`` from ``build_client``) is connected and
    kept alive; on connect-failure/disconnect we sleep ``backoff_delay(attempt)``
    then increment ``attempt`` (clamped). A *successful* connect resets
    ``attempt`` to 0. We never poll-by-reconnect and never reconnect inside
    ``on_disconnect`` (Pitfall 4 -- avoids the concurrent-session reboot risk).

    ``connect`` and ``sleep`` are injected so this is testable without sockets or
    real waits: ``connect(client)`` should establish + run the session (e.g.
    ``client.connect(...); client.loop_forever()``) and return/raise on drop, or
    raise ``StopSession`` to end the loop cleanly. ``record_attempt`` (optional)
    receives each attempt number as the loop runs, for observability/tests.

    Returns the final attempt counter.
    """
    attempt = 0
    while True:
        if record_attempt is not None:
            record_attempt(attempt)
        try:
            connect(client)
        except StopSession:
            return attempt
        except Exception as exc:  # noqa: BLE001 - any drop/failure -> back off
            logger.debug("MQTT session ended (%s); backing off", exc)
            sleep(backoff_delay(attempt))
            attempt = min(attempt + 1, len(BACKOFF_SCHEDULE) - 1)
            continue
        # connect() returned normally => a clean, successful session. Reset.
        attempt = 0
