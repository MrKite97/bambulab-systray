"""Bambu cloud REST authentication + device-list lookup.

A faithful port of the verified pybambu cloud flow (RESEARCH Patterns 1-3,
re-verified against live ``pybambu/bambu_cloud.py`` on 2026-06-20) into small,
HTTP-mockable pure functions. The REST flow is:

    login(email, password)
        -> accessToken non-empty                       -> done
        -> loginType == "verifyCode":
               request_email_code(email)
               login_with_code(email, code)            -> accessToken
    get_device_list(accessToken) -> devices[]
    pick_serial(devices)         -> devices[0]["dev_id"]   (the MQTT serial)
    mqtt_username_from_token(accessToken) -> "u_<digits>"  (JWT username claim)

Security (RESEARCH Security V7 / threat T-01-04): this module NEVER logs the
access token, the account password, or the Authorization header. The MQTT
password is the raw access token (returned to the caller, never logged here).
TLS cert verification stays ON (we never pass ``verify=False``) -- threat
T-01-05. Region is NOT read from any response: there is no ``region`` key in the
login or bind payload (Pitfall 3); the global endpoint is hardcoded below.
"""

import base64
import json

import requests

# Verified endpoint strings (RESEARCH Pattern 1/2; pybambu const.py BambuUrl).
LOGIN_URL = "https://api.bambulab.com/v1/user-service/user/login"
EMAIL_CODE_URL = "https://api.bambulab.com/v1/user-service/user/sendemail/code"
BIND_URL = "https://api.bambulab.com/v1/iot-service/api/user/bind"

# Headers pybambu sends (mimics OrcaSlicer). Content-Type/accept are essential.
HEADERS = {
    "User-Agent": "bambu_network_agent/01.09.05.01",
    "X-BBL-Client-Name": "OrcaSlicer",
    "X-BBL-Client-Type": "slicer",
    "X-BBL-Language": "en-US",
    "Content-Type": "application/json",
    "accept": "application/json",
}

_TIMEOUT = 30  # seconds, every REST call


def login(email: str, password: str) -> dict:
    """POST the login endpoint; return the parsed response.

    The caller inspects ``loginType`` / ``accessToken`` to decide whether the
    verifyCode email-code branch is needed. Response keys: accessToken,
    refreshToken, loginType, expiresIn (7776000 == 90 days).
    """
    body = {"account": email, "password": password, "apiError": ""}
    r = requests.post(LOGIN_URL, json=body, headers=HEADERS, timeout=_TIMEOUT)
    r.raise_for_status()
    return r.json()


def request_email_code(email: str) -> None:
    """Ask Bambu to email a 6-digit login code (the verifyCode branch)."""
    body = {"email": email, "type": "codeLogin"}
    r = requests.post(EMAIL_CODE_URL, json=body, headers=HEADERS, timeout=_TIMEOUT)
    r.raise_for_status()


def login_with_code(email: str, code: str) -> dict:
    """Complete the verifyCode login; ``code`` REPLACES the password field.

    The 6-digit format is validated by the caller (spike.py, plan 04) before we
    send it (threat T-01-07); auth.py forwards it verbatim.
    """
    body = {"account": email, "code": code}  # NOTE: code replaces password
    r = requests.post(LOGIN_URL, json=body, headers=HEADERS, timeout=_TIMEOUT)
    r.raise_for_status()
    return r.json()


def get_device_list(access_token: str) -> list[dict]:
    """GET the account's bound devices. Bearer auth; never log the header."""
    headers = {**HEADERS, "Authorization": f"Bearer {access_token}"}
    r = requests.get(BIND_URL, headers=headers, timeout=_TIMEOUT)
    r.raise_for_status()
    return r.json()["devices"]


def pick_serial(devices: list[dict]) -> str:
    """Return the MQTT serial = the first device's ``dev_id``.

    The serial is the ``dev_id`` key, NOT a ``serial`` key, and there is NO
    ``region`` key in the bind response (RESEARCH Pitfall 3). If multiple
    devices are bound the caller logs the full list and we take the first.
    """
    if not devices:
        raise ValueError("No bound devices on this account")
    return devices[0]["dev_id"]


def enrich_devices(devices: list[dict]) -> list[dict]:
    """Map raw bind devices to the {dev_id, name, dev_model_name, online} rows
    the Phase 8 printer-select screen needs.

    Extra bind fields (e.g. ``dev_access_code``) are intentionally dropped --
    only the four display fields cross into the UI. Every optional key is read
    with ``.get(..., default)`` so a sparse row (only ``dev_id`` present) yields
    sane defaults (``name``/``dev_model_name`` == "", ``online`` == False) and
    never raises. Input order is preserved.

    There is NO region key in the bind payload (RESEARCH Pitfall 3) -- region
    stays hardcoded global/EU; none is read or introduced here. Device contents
    are not logged.
    """
    return [
        {
            "dev_id": d.get("dev_id"),
            "name": d.get("name", ""),
            "dev_model_name": d.get("dev_model_name", ""),
            "online": d.get("online", False),
        }
        for d in devices
    ]


def mqtt_username_from_token(access_token: str) -> str:
    """Derive the MQTT username from the access-token JWT (RESEARCH Pattern 3).

    A JWT is ``header.payload.signature``; the payload's ``username`` claim is
    the MQTT username, formatted ``u_<digits>``. We base64-decode segment[1]
    only -- the header and signature are ignored, and the signature is NOT
    verified (we only read a claim; the server enforces token validity, threat
    T-01-06). stdlib ``base64`` + ``json`` only -- no PyJWT.

    The base64 payload may lack ``=`` padding, so we re-pad to a multiple of 4
    before decoding. The MQTT *password* is the raw ``access_token`` itself
    (handled by the caller; never logged -- threat T-01-04).
    """
    payload_b64 = access_token.split(".")[1]
    payload_b64 += "=" * (-len(payload_b64) % 4)  # fix base64 padding
    payload = json.loads(base64.b64decode(payload_b64))
    return payload["username"]  # e.g. "u_1234567890"
