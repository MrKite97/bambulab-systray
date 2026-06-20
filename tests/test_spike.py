"""Tests for src.spike (the CLI orchestration: token reuse, verifyCode login,
401 re-login, and the structured MINUTES-annotated status line).

All collaborators (``auth``, ``token_store``, ``mqtt_client``) are monkeypatched
-- these tests make NO network calls, perform NO real login, and contain NO real
credentials or tokens. The 6-digit code prompt is injected so stdin is never
read. Assertions are on call *sequence* and the formatter output, per RESEARCH
Patterns 1/3/5 and the threat register (T-01-12..16).
"""

import logging

import pytest

from src import spike
from src.state import PrintState


class CallRecorder:
    """Records calls to a patched function and returns a canned value."""

    def __init__(self, return_value=None):
        self.return_value = return_value
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.return_value


# --- get_access_token -------------------------------------------------------


def test_returns_stored_token_without_login(monkeypatch):
    """Test 1: a stored token is reused and login() is never called."""
    monkeypatch.setattr(spike.token_store, "load_token", lambda: "STORED")
    login = CallRecorder()
    monkeypatch.setattr(spike.auth, "login", login)

    tok = spike.get_access_token("e@x.com", "pw")

    assert tok == "STORED"
    assert login.calls == []


def test_password_login_saves_token(monkeypatch):
    """Test 2: no stored token + accessToken response -> returns + saves it."""
    monkeypatch.setattr(spike.token_store, "load_token", lambda: None)
    monkeypatch.setattr(spike.auth, "login", lambda e, p: {"accessToken": "X"})
    save = CallRecorder()
    monkeypatch.setattr(spike.token_store, "save_token", save)

    tok = spike.get_access_token("e@x.com", "pw")

    assert tok == "X"
    assert save.calls == [(("X",), {})]


def test_verifycode_branch(monkeypatch):
    """Test 3: verifyCode -> request code, prompt, exchange, save."""
    monkeypatch.setattr(spike.token_store, "load_token", lambda: None)
    monkeypatch.setattr(spike.auth, "login", lambda e, p: {"loginType": "verifyCode"})
    req = CallRecorder()
    monkeypatch.setattr(spike.auth, "request_email_code", req)
    exchange = CallRecorder(return_value={"accessToken": "FROMCODE"})
    monkeypatch.setattr(spike.auth, "login_with_code", exchange)
    save = CallRecorder()
    monkeypatch.setattr(spike.token_store, "save_token", save)

    tok = spike.get_access_token("e@x.com", "pw", prompt=lambda _: "123456")

    assert tok == "FROMCODE"
    assert req.calls == [(("e@x.com",), {})]
    assert exchange.calls == [(("e@x.com", "123456"), {})]
    assert save.calls == [(("FROMCODE",), {})]


def test_tfa_raises_systemexit(monkeypatch):
    """Test 4: loginType tfa -> SystemExit with a clear out-of-scope message."""
    monkeypatch.setattr(spike.token_store, "load_token", lambda: None)
    monkeypatch.setattr(spike.auth, "login", lambda e, p: {"loginType": "tfa"})

    with pytest.raises(SystemExit) as excinfo:
        spike.get_access_token("e@x.com", "pw")

    assert "2fa" in str(excinfo.value).lower()


def test_unexpected_response_raises_systemexit(monkeypatch):
    """Unexpected login response (no token, unknown branch) -> SystemExit."""
    monkeypatch.setattr(spike.token_store, "load_token", lambda: None)
    monkeypatch.setattr(spike.auth, "login", lambda e, p: {"loginType": "mystery"})

    with pytest.raises(SystemExit):
        spike.get_access_token("e@x.com", "pw")


def test_force_relogin_skips_stored_token(monkeypatch):
    """Test 5: force_relogin does a fresh login even when a token is stored.

    This proves the 401 fallback re-runs the flow rather than reusing the
    (rejected) stored token.
    """
    monkeypatch.setattr(spike.token_store, "load_token", lambda: "STALE")
    login = CallRecorder(return_value={"accessToken": "FRESH"})
    monkeypatch.setattr(spike.auth, "login", login)
    monkeypatch.setattr(spike.token_store, "save_token", CallRecorder())

    tok = spike.get_access_token("e@x.com", "pw", force_relogin=True)

    assert tok == "FRESH"
    assert len(login.calls) == 1  # login WAS called despite a stored token


# --- 6-digit code validation ------------------------------------------------


def test_code_validation_reprompts_then_accepts(monkeypatch):
    """Test 7: a non-6-digit / non-numeric entry is rejected and re-prompted
    before login_with_code is ever called."""
    monkeypatch.setattr(spike.token_store, "load_token", lambda: None)
    monkeypatch.setattr(spike.auth, "login", lambda e, p: {"loginType": "verifyCode"})
    monkeypatch.setattr(spike.auth, "request_email_code", CallRecorder())
    exchange = CallRecorder(return_value={"accessToken": "OK"})
    monkeypatch.setattr(spike.auth, "login_with_code", exchange)
    monkeypatch.setattr(spike.token_store, "save_token", CallRecorder())

    entries = iter(["abc", "12345", "1234567", "12ab56", "654321"])
    spike.get_access_token("e@x.com", "pw", prompt=lambda _: next(entries))

    # Only the final valid 6-digit code reaches the network.
    assert exchange.calls == [(("e@x.com", "654321"), {})]


@pytest.mark.parametrize(
    "code,valid",
    [
        ("123456", True),
        ("000000", True),
        ("12345", False),
        ("1234567", False),
        ("12ab56", False),
        ("", False),
        ("12 456", False),
    ],
)
def test_is_valid_code(code, valid):
    assert spike._is_valid_code(code) is valid


# --- structured status line -------------------------------------------------


def test_log_status_formats_minutes(caplog):
    """Test 6: the status line renders state/percent/raw -> h:mm with the
    MINUTES-compare note (83 -> '1:23')."""
    state = PrintState(gcode_state="running", mc_percent=42, mc_remaining_time=83)
    with caplog.at_level(logging.INFO, logger="spike"):
        spike.log_status(state)

    line = caplog.text
    assert "state=running" in line
    assert "percent= 42%" in line
    assert "remaining_raw=83" in line
    assert "1:23" in line
    assert "confirm MINUTES" in line


def test_status_reporter_captures_distinct_gcode_states():
    """The reporter records each DISTINCT raw gcode_state for Phase 3."""
    state = PrintState()
    reporter = spike._StatusReporter(state)

    class FakeMsg:
        def __init__(self, gcode):
            import json

            self.payload = json.dumps({"print": {"gcode_state": gcode, "mc_percent": 10}})

    reporter.on_message(None, {"state": state}, FakeMsg("RUNNING"))
    reporter.on_message(None, {"state": state}, FakeMsg("RUNNING"))
    reporter.on_message(None, {"state": state}, FakeMsg("PAUSE"))

    assert reporter.seen_gcode_states == {"RUNNING", "PAUSE"}


# --- 401 re-login orchestration ---------------------------------------------


def test_run_with_relogin_clears_and_relogins_on_auth_failure(monkeypatch):
    """A token-rejected session clears the token and re-logs in once, then
    succeeds on the second session."""
    # First get_access_token call returns STALE (stored), second returns FRESH.
    tokens = iter(["STALE", "FRESH"])
    monkeypatch.setattr(spike, "get_access_token", lambda *a, **k: next(tokens))
    monkeypatch.setattr(spike.auth, "mqtt_username_from_token", lambda t: "u_1")
    monkeypatch.setattr(spike.auth, "get_device_list", lambda t: [{"dev_id": "SER"}])
    monkeypatch.setattr(spike.auth, "pick_serial", lambda d: "SER")

    built = []

    class FakeClient:
        def __init__(self, token):
            self.token = token

        def user_data_set(self, ud):
            self.userdata = ud

    def fake_build_client(client_id, username, access_token):
        c = FakeClient(access_token)
        built.append(c)
        return c

    monkeypatch.setattr(spike.mqtt_client, "build_client", fake_build_client)

    clear = CallRecorder()
    monkeypatch.setattr(spike.token_store, "clear_token", clear)

    # run_session: first call raises _AuthFailed, second returns cleanly.
    sessions = iter([spike._AuthFailed("Not authorized"), None])

    def fake_run_session(client, *, connect, sleep):
        result = next(sessions)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(spike.mqtt_client, "run_session", fake_run_session)

    spike.run_with_relogin("e@x.com", "pw", connect=lambda c: None, sleep=lambda s: None)

    assert len(clear.calls) == 1  # token cleared exactly once
    assert [c.token for c in built] == ["STALE", "FRESH"]  # rebuilt with fresh token


def test_default_connect_maps_auth_error_to_authfailed():
    """An auth-rejection on connect surfaces as _AuthFailed (drives re-login)."""

    class FakeClient:
        def connect(self, host, port, keepalive):
            raise RuntimeError("Connection refused: Not authorized")

        def loop_forever(self):
            raise AssertionError("should not reach loop_forever on auth failure")

    with pytest.raises(spike._AuthFailed):
        spike._default_connect(FakeClient())


def test_default_connect_propagates_transient_errors():
    """A non-auth connect error is NOT treated as an auth failure."""

    class FakeClient:
        def connect(self, host, port, keepalive):
            raise OSError("network unreachable")

        def loop_forever(self):  # pragma: no cover
            pass

    with pytest.raises(OSError):
        spike._default_connect(FakeClient())


def test_redact_devices_strips_lan_access_code():
    """The LAN access code is never included in the logged device view."""
    devices = [{"dev_id": "SER", "name": "P1", "dev_access_code": "secret\n", "online": True}]
    safe = spike._redact_devices(devices)
    assert "dev_access_code" not in safe[0]
    assert safe[0]["dev_id"] == "SER"
