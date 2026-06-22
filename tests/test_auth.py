"""Tests for src.auth (Bambu cloud REST login + verifyCode + bind/serial + JWT).

All HTTP is mocked -- these tests make NO network calls and contain NO real
credentials or tokens. They assert on request *structure* (URL, body keys,
Authorization header) per RESEARCH Patterns 1-3 and the threat register
(T-01-04: never log/embed real secrets). The fake JWT in the JWT-claim tests is
hand-built from a payload dict, so the only "token" present is a structural
fixture, not a credential.
"""

import base64
import json

import pytest

from src import auth


class FakeResponse:
    """Minimal stand-in for a requests.Response."""

    def __init__(self, json_data=None, status_code=200):
        self._json = json_data if json_data is not None else {}
        self.status_code = status_code
        self.raise_called = False

    def raise_for_status(self):
        self.raise_called = True

    def json(self):
        return self._json


class RequestRecorder:
    """Records the args of the last post/get call and returns a canned response."""

    def __init__(self, response):
        self.response = response
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self.response

    @property
    def last(self):
        return self.calls[-1]


# --------------------------------------------------------------------------- #
# Task 1: login / request_email_code / login_with_code / device list / serial
# --------------------------------------------------------------------------- #


def test_login_posts_login_url_with_account_password_apierror(monkeypatch):
    """Test 1: login() POSTs LOGIN_URL with {account,password,apiError}, returns JSON."""
    resp = FakeResponse({"accessToken": "tok", "loginType": ""})
    recorder = RequestRecorder(resp)
    monkeypatch.setattr(auth.requests, "post", recorder)

    result = auth.login("user@example.com", "secret")

    assert recorder.last["url"] == auth.LOGIN_URL
    assert recorder.last["json"] == {
        "account": "user@example.com",
        "password": "secret",
        "apiError": "",
    }
    assert recorder.last["timeout"] == 30
    assert resp.raise_called is True
    assert result == {"accessToken": "tok", "loginType": ""}


def test_request_email_code_posts_email_url_with_codelogin(monkeypatch):
    """Test 2: request_email_code() POSTs EMAIL_CODE_URL with {email,type:codeLogin}."""
    resp = FakeResponse({})
    recorder = RequestRecorder(resp)
    monkeypatch.setattr(auth.requests, "post", recorder)

    result = auth.request_email_code("user@example.com")

    assert recorder.last["url"] == auth.EMAIL_CODE_URL
    assert recorder.last["json"] == {"email": "user@example.com", "type": "codeLogin"}
    assert resp.raise_called is True
    assert result is None


def test_login_with_code_posts_account_code_no_password(monkeypatch):
    """Test 3: login_with_code() POSTs {account,code} (no password) -> JSON w/ token."""
    resp = FakeResponse({"accessToken": "tok2"})
    recorder = RequestRecorder(resp)
    monkeypatch.setattr(auth.requests, "post", recorder)

    result = auth.login_with_code("user@example.com", "123456")

    assert recorder.last["url"] == auth.LOGIN_URL
    body = recorder.last["json"]
    assert body == {"account": "user@example.com", "code": "123456"}
    assert "password" not in body
    assert result == {"accessToken": "tok2"}


def test_get_device_list_sends_bearer_and_returns_devices(monkeypatch):
    """Test 4: get_device_list() GETs BIND_URL with Bearer header, returns devices[]."""
    devices = [{"dev_id": "00M00A", "name": "x", "online": True}]
    resp = FakeResponse({"devices": devices, "message": "success"})
    recorder = RequestRecorder(resp)
    monkeypatch.setattr(auth.requests, "get", recorder)

    result = auth.get_device_list("ACCESS_TOKEN_PLACEHOLDER")

    assert recorder.last["url"] == auth.BIND_URL
    assert recorder.last["headers"]["Authorization"] == "Bearer ACCESS_TOKEN_PLACEHOLDER"
    # Base headers preserved alongside Authorization.
    assert recorder.last["headers"]["Content-Type"] == "application/json"
    assert resp.raise_called is True
    assert result == devices


def test_pick_serial_returns_first_dev_id():
    """Test 5: pick_serial returns the first device's dev_id."""
    devices = [{"dev_id": "00M00A", "name": "x"}, {"dev_id": "OTHER"}]
    assert auth.pick_serial(devices) == "00M00A"


def test_pick_serial_empty_list_raises_clear_error():
    """Test 6: pick_serial raises a clear error on an empty device list."""
    with pytest.raises(ValueError, match="No bound devices"):
        auth.pick_serial([])


# --------------------------------------------------------------------------- #
# Task 1b: enrich_devices -- structured {dev_id,name,dev_model_name,online} rows
# --------------------------------------------------------------------------- #


def test_enrich_devices_maps_exactly_four_keys():
    """A full bind row is mapped to EXACTLY {dev_id,name,dev_model_name,online};
    extra keys (e.g. dev_access_code) are dropped."""
    devices = [
        {
            "dev_id": "00M00A",
            "name": "Studio P1S",
            "dev_model_name": "P1S",
            "online": True,
            "dev_access_code": "SECRET_DROP_ME",
        }
    ]
    rows = auth.enrich_devices(devices)
    assert rows == [
        {
            "dev_id": "00M00A",
            "name": "Studio P1S",
            "dev_model_name": "P1S",
            "online": True,
        }
    ]
    # Extra bind fields are not carried through.
    assert set(rows[0].keys()) == {"dev_id", "name", "dev_model_name", "online"}
    assert "dev_access_code" not in rows[0]


def test_enrich_devices_sparse_row_uses_safe_defaults():
    """A row with only dev_id yields safe defaults (no KeyError)."""
    rows = auth.enrich_devices([{"dev_id": "ONLY_ID"}])
    assert rows == [
        {
            "dev_id": "ONLY_ID",
            "name": "",
            "dev_model_name": "",
            "online": False,
        }
    ]


def test_enrich_devices_preserves_order():
    """Output order matches input order."""
    devices = [{"dev_id": "A"}, {"dev_id": "B"}, {"dev_id": "C"}]
    rows = auth.enrich_devices(devices)
    assert [r["dev_id"] for r in rows] == ["A", "B", "C"]


def test_enrich_devices_empty_list_returns_empty():
    """Empty input -> empty output, no raise."""
    assert auth.enrich_devices([]) == []


def test_enrich_devices_does_not_introduce_region_key():
    """There is no region key in the bind payload (Pitfall 3); none is added."""
    rows = auth.enrich_devices([{"dev_id": "A", "region": "should_be_ignored"}])
    assert "region" not in rows[0]


def test_pick_serial_back_compat_after_enrich(monkeypatch):
    """Regression: pick_serial still returns devices[0]['dev_id'] unchanged."""
    devices = [{"dev_id": "FIRST", "name": "x"}, {"dev_id": "SECOND"}]
    assert auth.pick_serial(devices) == "FIRST"


# --------------------------------------------------------------------------- #
# Task 2: mqtt_username_from_token -- JWT username claim
# --------------------------------------------------------------------------- #


def _fake_jwt(payload: dict, *, strip_padding: bool = False) -> str:
    """Build a structurally-valid 'header.payload.signature' fake JWT.

    Not a real signed token -- only the middle segment carries meaning. The
    signature is a dummy. Used to exercise the claim-read + base64 padding fix.
    """
    payload_b64 = base64.b64encode(json.dumps(payload).encode()).decode()
    if strip_padding:
        payload_b64 = payload_b64.rstrip("=")
    return f"HEADER.{payload_b64}.SIGNATURE"


def test_mqtt_username_from_token_reads_username_claim():
    """Test 1: a well-formed fake JWT yields its username claim."""
    token = _fake_jwt({"username": "u_1234567890"})
    assert auth.mqtt_username_from_token(token) == "u_1234567890"


def test_mqtt_username_from_token_handles_missing_padding():
    """Test 2: payload b64 with stripped '=' padding still decodes."""
    token = _fake_jwt({"username": "u_1234567890"}, strip_padding=True)
    # Sanity-check the fixture actually has bad padding (len not multiple of 4).
    payload_b64 = token.split(".")[1]
    assert len(payload_b64) % 4 != 0
    assert auth.mqtt_username_from_token(token) == "u_1234567890"


def test_mqtt_username_from_token_reads_segment_one_not_header_or_sig():
    """Test 3: header/signature are ignored; only segment[1] is decoded."""
    # Put a different (bogus) payload in the header position to prove it's skipped.
    middle = base64.b64encode(json.dumps({"username": "u_999"}).encode()).decode()
    bogus = base64.b64encode(json.dumps({"username": "u_WRONG"}).encode()).decode()
    token = f"{bogus}.{middle}.{bogus}"
    assert auth.mqtt_username_from_token(token) == "u_999"
