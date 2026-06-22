"""Unit tests for src.session: the SessionController orchestration brain.

The whole login -> emailed-code -> enriched-device-list -> select -> start ->
logout flow is driven here with INJECTED FAKES -- no real network, no GUI, no
stdin. ``run_async=lambda fn: fn()`` runs every worker body inline so the
blocking auth/device calls are deterministic in tests.

Two load-bearing security invariants are asserted throughout (T-08-01/02):
- The password, the 6-digit code, and the access token are NEVER present in any
  ``push_state`` / ``push_error`` / ``push_devices`` / ``push_auth_step`` payload.
- Defensive 6-digit validation rejects a malformed code BEFORE any network call.
"""

import json

import pytest

from src.session import SessionController


# --------------------------------------------------------------------------- #
# Fakes (mirror the CallRecorder/fake style in test_app.py / test_bridge.py)   #
# --------------------------------------------------------------------------- #


class FakeAuth:
    """Records auth calls and returns canned login / device responses."""

    def __init__(self, *, login_result=None, code_result=None, devices=None,
                 enriched=None, login_exc=None, code_exc=None, devices_exc=None):
        self._login_result = login_result or {}
        self._code_result = code_result or {}
        self._devices = devices if devices is not None else []
        self._enriched = enriched
        self._login_exc = login_exc
        self._code_exc = code_exc
        self._devices_exc = devices_exc
        self.calls = []  # list of (method, args) tuples

    def login(self, email, password):
        self.calls.append(("login", (email, password)))
        if self._login_exc:
            raise self._login_exc
        return self._login_result

    def request_email_code(self, email):
        self.calls.append(("request_email_code", (email,)))

    def login_with_code(self, email, code):
        self.calls.append(("login_with_code", (email, code)))
        if self._code_exc:
            raise self._code_exc
        return self._code_result

    def get_device_list(self, token):
        self.calls.append(("get_device_list", (token,)))
        if self._devices_exc:
            raise self._devices_exc
        return self._devices

    def enrich_devices(self, devices):
        self.calls.append(("enrich_devices", (devices,)))
        if self._enriched is not None:
            return self._enriched
        # default passthrough enrich shape
        return [
            {
                "dev_id": d.get("dev_id"),
                "name": d.get("name", ""),
                "dev_model_name": d.get("dev_model_name", ""),
                "online": d.get("online", False),
            }
            for d in devices
        ]

    def method_calls(self, name):
        return [args for (m, args) in self.calls if m == name]

    def called(self, name):
        return any(m == name for (m, _) in self.calls)


class FakeTokenStore:
    def __init__(self, token=None):
        self._token = token
        self.saved = []
        self.cleared = 0

    def save_token(self, token):
        self.saved.append(token)
        self._token = token

    def load_token(self):
        return self._token

    def clear_token(self):
        self.cleared += 1
        self._token = None


class FakeSettings:
    def __init__(self, settings=None):
        self._settings = settings if settings is not None else {"region": "global", "serial": None}
        self.saved = []

    def load_settings(self):
        return dict(self._settings)

    def save_settings(self, settings):
        self.saved.append(dict(settings))
        self._settings = dict(settings)


class FakeFlyout:
    """Records every push_* call and its payload for inspection."""

    def __init__(self):
        self.calls = []  # list of (method, payload)

    def push_state(self, state):
        self.calls.append(("push_state", state))

    def push_auth_step(self, step):
        self.calls.append(("push_auth_step", step))

    def push_devices(self, devices):
        self.calls.append(("push_devices", devices))

    def push_error(self, msg):
        self.calls.append(("push_error", msg))

    def payloads(self, method):
        return [p for (m, p) in self.calls if m == method]

    def steps(self):
        return self.payloads("push_auth_step")

    def errors(self):
        return self.payloads("push_error")

    def all_payloads_json(self):
        """Every payload serialized to JSON, for secret-leak scanning."""
        return json.dumps([p for (_, p) in self.calls], default=str)


def _make_controller(**kw):
    """Build a SessionController with fakes; run_async inline by default."""
    auth = kw.pop("auth", FakeAuth())
    token_store = kw.pop("token_store", FakeTokenStore())
    settings = kw.pop("settings", FakeSettings())
    flyout = kw.pop("flyout", FakeFlyout())
    start_calls = []
    stop_calls = []
    start_mqtt = kw.pop("start_mqtt", lambda token, serial: start_calls.append((token, serial)))
    stop_session = kw.pop("stop_session", lambda: stop_calls.append(True))
    controller = SessionController(
        auth=auth,
        token_store=token_store,
        settings=settings,
        flyout=flyout,
        start_mqtt=start_mqtt,
        stop_session=stop_session,
        run_async=lambda fn: fn(),
    )
    controller._test_start_calls = start_calls
    controller._test_stop_calls = stop_calls
    return controller, auth, token_store, settings, flyout


# --------------------------------------------------------------------------- #
# login_submit                                                                #
# --------------------------------------------------------------------------- #


def test_login_submit_verifycode_advances_to_code_step():
    auth = FakeAuth(login_result={"loginType": "verifyCode"})
    c, auth, ts, st, fly = _make_controller(auth=auth)
    c.login_submit("a@b.c", "pw")
    assert auth.method_calls("request_email_code") == [("a@b.c",)]
    assert fly.steps() == ["code"]


def test_login_submit_direct_token_skips_code_and_fetches_devices():
    auth = FakeAuth(
        login_result={"accessToken": "tok"},
        devices=[{"dev_id": "D1", "name": "X1", "dev_model_name": "X1C", "online": True}],
    )
    c, auth, ts, st, fly = _make_controller(auth=auth)
    c.login_submit("a@b.c", "pw")
    # no code step
    assert "code" not in fly.steps()
    # device fetch ran -> select step
    assert auth.called("get_device_list")
    assert "select" in fly.steps()


def test_login_submit_failure_surfaces_error_and_stays_on_login():
    auth = FakeAuth(login_exc=RuntimeError("boom"))
    c, auth, ts, st, fly = _make_controller(auth=auth)
    c.login_submit("a@b.c", "pw")
    assert fly.errors(), "an error must be pushed"
    assert "code" not in fly.steps()
    assert not auth.called("request_email_code")


def test_login_submit_remembers_account_for_code_step():
    auth = FakeAuth(login_result={"loginType": "verifyCode"}, code_result={"accessToken": "tok"})
    c, auth, ts, st, fly = _make_controller(auth=auth)
    c.login_submit("user@x.io", "pw")
    c.submit_code("123456")
    assert ("user@x.io", "123456") in auth.method_calls("login_with_code")


# --------------------------------------------------------------------------- #
# submit_code                                                                 #
# --------------------------------------------------------------------------- #


def test_submit_code_valid_saves_token_then_fetches_devices():
    auth = FakeAuth(
        login_result={"loginType": "verifyCode"},
        code_result={"accessToken": "tok"},
        devices=[{"dev_id": "D1", "online": True}],
    )
    c, auth, ts, st, fly = _make_controller(auth=auth)
    c.login_submit("a@b.c", "pw")
    c.submit_code("123456")
    assert ts.saved == ["tok"]
    assert auth.called("get_device_list")
    assert "select" in fly.steps()


@pytest.mark.parametrize("bad", ["12a456", "", "1234567", "12345", "abcdef", "  1234"])
def test_submit_code_invalid_rejected_before_network(bad):
    auth = FakeAuth(login_result={"loginType": "verifyCode"})
    c, auth, ts, st, fly = _make_controller(auth=auth)
    c.login_submit("a@b.c", "pw")
    auth.calls.clear()
    c.submit_code(bad)
    assert not auth.called("login_with_code")
    assert fly.errors(), "invalid code must push an error"


def test_submit_code_failure_stays_on_code():
    auth = FakeAuth(
        login_result={"loginType": "verifyCode"},
        code_exc=RuntimeError("bad code"),
    )
    c, auth, ts, st, fly = _make_controller(auth=auth)
    c.login_submit("a@b.c", "pw")
    c.submit_code("123456")
    assert fly.errors()
    assert ts.saved == []
    assert "select" not in fly.steps()


def test_submit_code_no_access_token_pushes_error():
    auth = FakeAuth(
        login_result={"loginType": "verifyCode"},
        code_result={"someOtherKey": "x"},
    )
    c, auth, ts, st, fly = _make_controller(auth=auth)
    c.login_submit("a@b.c", "pw")
    c.submit_code("123456")
    assert fly.errors()
    assert ts.saved == []


# --------------------------------------------------------------------------- #
# resend_code                                                                 #
# --------------------------------------------------------------------------- #


def test_resend_code_retriggers_email_for_pending_account():
    auth = FakeAuth(login_result={"loginType": "verifyCode"})
    c, auth, ts, st, fly = _make_controller(auth=auth)
    c.login_submit("pending@x.io", "pw")
    auth.calls.clear()
    c.resend_code()
    assert auth.method_calls("request_email_code") == [("pending@x.io",)]


# --------------------------------------------------------------------------- #
# logout                                                                      #
# --------------------------------------------------------------------------- #


def test_logout_clears_token_stops_session_resets_to_login():
    c, auth, ts, st, fly = _make_controller()
    c.logout()
    assert ts.cleared == 1
    assert c._test_stop_calls == [True]
    assert fly.steps()[-1] == "login"


# --------------------------------------------------------------------------- #
# _device_rows mapping                                                        #
# --------------------------------------------------------------------------- #


def test_device_rows_maps_enriched_to_panel_contract():
    c, auth, ts, st, fly = _make_controller()
    rows = c._device_rows([
        {"dev_id": "D1", "name": "Bench", "dev_model_name": "X1C", "online": True},
        {"dev_id": "D2", "name": "Old", "dev_model_name": "P1S", "online": False},
    ])
    assert rows[0] == {
        "id": "D1",
        "name": "Bench",
        "model": "X1C",
        "statusLabel": "Online",
        "statusColor": "var(--status-done)",
    }
    assert rows[1]["id"] == "D2"
    assert rows[1]["statusLabel"] == "Offline"
    assert rows[1]["statusColor"] == "var(--status-offline)"


# --------------------------------------------------------------------------- #
# Security: secrets never logged / never pushed                              #
# --------------------------------------------------------------------------- #


def test_no_push_payload_contains_password_code_or_token():
    auth = FakeAuth(
        login_result={"loginType": "verifyCode"},
        code_result={"accessToken": "SECRET_TOKEN_XYZ"},
        devices=[{"dev_id": "D1", "name": "X1", "online": True}],
    )
    c, auth, ts, st, fly = _make_controller(auth=auth)
    c.login_submit("a@b.c", "SECRET_PASSWORD_123")
    c.submit_code("987654")
    blob = fly.all_payloads_json()
    assert "SECRET_PASSWORD_123" not in blob
    assert "SECRET_TOKEN_XYZ" not in blob
    assert "987654" not in blob


def test_controller_logs_no_secret(caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    auth = FakeAuth(
        login_result={"loginType": "verifyCode"},
        code_result={"accessToken": "SECRET_TOKEN_XYZ"},
        devices=[{"dev_id": "D1", "online": True}],
    )
    c, auth, ts, st, fly = _make_controller(auth=auth)
    c.login_submit("a@b.c", "SECRET_PASSWORD_123")
    c.submit_code("987654")
    text = caplog.text
    assert "SECRET_PASSWORD_123" not in text
    assert "SECRET_TOKEN_XYZ" not in text
    assert "987654" not in text


# --------------------------------------------------------------------------- #
# Task 2: _enter_logged_in device fetch / 401 / select_printer / bootstrap     #
# --------------------------------------------------------------------------- #


def _http_error(status):
    """Build a requests.HTTPError carrying a response with ``status`` code."""
    import requests

    resp = requests.Response()
    resp.status_code = status
    err = requests.HTTPError(f"{status} error")
    err.response = resp
    return err


def test_enter_logged_in_fetches_enriches_and_pushes_select():
    auth = FakeAuth(
        login_result={"accessToken": "tok"},
        devices=[{"dev_id": "D1", "name": "X1", "dev_model_name": "X1C", "online": True}],
    )
    c, auth, ts, st, fly = _make_controller(auth=auth)
    c.login_submit("a@b.c", "pw")
    assert auth.method_calls("get_device_list") == [("tok",)]
    assert auth.called("enrich_devices")
    pushed = fly.payloads("push_devices")
    assert pushed and pushed[0][0]["id"] == "D1"
    assert fly.steps()[-1] == "select"


def test_enter_logged_in_401_clears_token_and_returns_to_login():
    auth = FakeAuth(
        login_result={"accessToken": "tok"},
        devices_exc=_http_error(401),
    )
    c, auth, ts, st, fly = _make_controller(auth=auth)
    c.login_submit("a@b.c", "pw")
    assert ts.cleared == 1
    assert fly.steps()[-1] == "login"
    assert fly.errors()


def test_enter_logged_in_non_401_pushes_error_keeps_token():
    auth = FakeAuth(
        login_result={"accessToken": "tok"},
        devices_exc=_http_error(500),
    )
    c, auth, ts, st, fly = _make_controller(auth=auth)
    c.login_submit("a@b.c", "pw")
    assert ts.cleared == 0
    assert fly.errors()
    assert "select" not in fly.steps()


def test_select_printer_persists_serial_region_preserved_and_starts_mqtt():
    auth = FakeAuth(
        login_result={"accessToken": "tok"},
        devices=[{"dev_id": "DEV123", "name": "X1", "online": True}],
    )
    settings = FakeSettings({"region": "eu", "serial": None})
    c, auth, ts, st, fly = _make_controller(auth=auth, settings=settings)
    c.login_submit("a@b.c", "pw")
    c.select_printer("DEV123")
    assert settings.saved[-1] == {"region": "eu", "serial": "DEV123"}
    assert c._test_start_calls == [("tok", "DEV123")]
    # progress state pushed, logged in
    states = fly.payloads("push_state")
    assert states and states[-1]["loggedIn"] is True


def test_select_printer_without_token_returns_to_login_no_mqtt():
    c, auth, ts, st, fly = _make_controller()
    c.select_printer("DEV123")
    assert c._test_start_calls == []
    assert fly.steps()[-1] == "login"


def test_bootstrap_from_stored_valid_token_starts_mqtt():
    auth = FakeAuth(login_result={}, devices=[{"dev_id": "D1", "online": True}])
    ts = FakeTokenStore(token="stored-tok")
    settings = FakeSettings({"region": "global", "serial": "D1"})
    c, auth, ts, st, fly = _make_controller(auth=auth, token_store=ts, settings=settings)
    result = c.bootstrap_from_stored()
    assert result is True
    assert c._test_start_calls == [("stored-tok", "D1")]
    states = fly.payloads("push_state")
    assert states and states[-1]["loggedIn"] is True


def test_bootstrap_from_stored_401_returns_to_login():
    auth = FakeAuth(devices_exc=_http_error(401))
    ts = FakeTokenStore(token="stale-tok")
    settings = FakeSettings({"region": "global", "serial": "D1"})
    c, auth, ts, st, fly = _make_controller(auth=auth, token_store=ts, settings=settings)
    result = c.bootstrap_from_stored()
    assert result is True
    assert ts.cleared == 1
    assert fly.steps()[-1] == "login"
    assert c._test_start_calls == []


def test_bootstrap_from_stored_no_token_goes_to_login():
    ts = FakeTokenStore(token=None)
    settings = FakeSettings({"region": "global", "serial": "D1"})
    c, auth, ts, st, fly = _make_controller(token_store=ts, settings=settings)
    result = c.bootstrap_from_stored()
    assert result is False
    assert fly.steps() == ["login"]
    assert c._test_start_calls == []


def test_bootstrap_from_stored_no_serial_goes_to_login():
    ts = FakeTokenStore(token="tok")
    settings = FakeSettings({"region": "global", "serial": None})
    c, auth, ts, st, fly = _make_controller(token_store=ts, settings=settings)
    result = c.bootstrap_from_stored()
    assert result is False
    assert fly.steps() == ["login"]


def test_no_push_payload_contains_token_in_select_or_bootstrap():
    auth = FakeAuth(
        login_result={"accessToken": "SECRET_TOK_999"},
        devices=[{"dev_id": "D1", "name": "X1", "online": True}],
    )
    c, auth, ts, st, fly = _make_controller(auth=auth)
    c.login_submit("a@b.c", "pw")
    c.select_printer("D1")
    assert "SECRET_TOK_999" not in fly.all_payloads_json()
