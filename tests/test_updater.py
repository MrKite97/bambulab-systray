"""Tests for src.updater: the pure, injectable check_for_update.

No real network: ``get`` is injected as a fake callable returning a small
``_FakeResp`` (or raising). Covers the version compare (v-prefix + 2.10>2.9),
soft-fail branches (raise / 304 / 403 / 404), the If-None-Match + Accept headers,
the Setup-asset + .sha256 parse, and the response ETag round-trip.

Threats under test (Phase 13 register):
- T-13-01 (Tampering): non-dict / missing asset fields yield None, never raise.
- T-13-02 (DoS): any exception (incl. timeout) soft-fails to None.
"""

from src import updater
from src.updater import UpdateInfo, check_for_update


class _FakeResp:
    """Minimal stand-in for requests.Response touching only what updater reads."""

    def __init__(self, status_code=200, json_data=None, headers=None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.headers = headers if headers is not None else {}

    def json(self):
        return self._json


def _newer_json(tag="v2.2.0", with_sha=True):
    assets = [
        {
            "name": "BambuLabSystray-Setup-2.2.0.exe",
            "browser_download_url": "https://example/BambuLabSystray-Setup-2.2.0.exe",
            "size": 8123456,
        }
    ]
    if with_sha:
        assets.append(
            {
                "name": "BambuLabSystray-Setup-2.2.0.exe.sha256",
                "browser_download_url": "https://example/BambuLabSystray-Setup-2.2.0.exe.sha256",
                "size": 80,
            }
        )
    return {
        "tag_name": tag,
        "html_url": "https://github.com/o/bambulab-systray/releases/tag/" + tag,
        "assets": assets,
    }


def _make_get(resp, recorder=None):
    """Return a fake get(url, headers=..., timeout=...) capturing its call kwargs."""

    def _get(url, headers=None, timeout=None):
        if recorder is not None:
            recorder["url"] = url
            recorder["headers"] = headers or {}
            recorder["timeout"] = timeout
        return resp

    return _get


# --- newer release -> UpdateInfo ------------------------------------------


def test_newer_release_returns_update_info():
    resp = _FakeResp(200, _newer_json(), headers={"ETag": 'W/"xyz"'})
    info = check_for_update("2.1.0", get=_make_get(resp))
    assert isinstance(info, UpdateInfo)
    assert info.version == "2.2.0"
    assert info.tag == "v2.2.0"
    assert info.html_url.endswith("v2.2.0")
    assert info.asset_name == "BambuLabSystray-Setup-2.2.0.exe"
    assert info.asset_url == "https://example/BambuLabSystray-Setup-2.2.0.exe"
    assert info.asset_size == 8123456
    assert info.sha256_url == "https://example/BambuLabSystray-Setup-2.2.0.exe.sha256"
    assert info.etag == 'W/"xyz"'


def test_missing_sha256_still_returns_update_info():
    resp = _FakeResp(200, _newer_json(with_sha=False))
    info = check_for_update("2.1.0", get=_make_get(resp))
    assert isinstance(info, UpdateInfo)
    assert info.asset_name == "BambuLabSystray-Setup-2.2.0.exe"
    assert info.sha256_url is None


# --- compare: equal / older / v-prefix / 2.10 > 2.9 ----------------------


def test_equal_version_returns_none():
    resp = _FakeResp(200, _newer_json(tag="v2.1.0"))
    assert check_for_update("2.1.0", get=_make_get(resp)) is None


def test_older_release_returns_none():
    resp = _FakeResp(200, _newer_json(tag="v2.0.0"))
    assert check_for_update("2.1.0", get=_make_get(resp)) is None


def test_v_prefix_tolerated_as_newer():
    resp = _FakeResp(200, _newer_json(tag="v2.2.0"))
    info = check_for_update("2.1.0", get=_make_get(resp))
    assert info is not None
    assert info.version == "2.2.0"


def test_2_10_is_newer_than_2_9():
    resp = _FakeResp(200, _newer_json(tag="2.10.0"))
    info = check_for_update("2.9.0", get=_make_get(resp))
    assert info is not None
    assert info.version == "2.10.0"


# --- soft-fail: raise / 304 / 403 / 404 -----------------------------------


def test_get_raises_returns_none_no_exception():
    def _boom(url, headers=None, timeout=None):
        raise ConnectionError("offline")

    # Must not raise.
    assert check_for_update("2.1.0", get=_boom) is None


def test_status_304_returns_none():
    resp = _FakeResp(304, {})
    assert check_for_update("2.1.0", get=_make_get(resp)) is None


def test_status_403_returns_none():
    resp = _FakeResp(403, {})
    assert check_for_update("2.1.0", get=_make_get(resp)) is None


def test_status_404_returns_none():
    resp = _FakeResp(404, {})
    assert check_for_update("2.1.0", get=_make_get(resp)) is None


# --- headers: Accept always, If-None-Match when etag passed ----------------


def test_accept_header_always_sent():
    rec = {}
    resp = _FakeResp(200, _newer_json())
    check_for_update("2.1.0", get=_make_get(resp, rec))
    assert rec["headers"].get("Accept") == "application/vnd.github+json"
    assert "If-None-Match" not in rec["headers"]


def test_if_none_match_sent_when_etag_passed():
    rec = {}
    resp = _FakeResp(304, {})
    check_for_update("2.1.0", get=_make_get(resp, rec), etag='W/"cached"')
    assert rec["headers"].get("If-None-Match") == 'W/"cached"'


def test_timeout_is_passed():
    rec = {}
    resp = _FakeResp(200, _newer_json())
    check_for_update("2.1.0", get=_make_get(resp, rec))
    assert rec["timeout"] == 10


# --- tampering: non-dict assets / missing tag -----------------------------


def test_missing_tag_returns_none():
    resp = _FakeResp(200, {"html_url": "x", "assets": []})
    assert check_for_update("2.1.0", get=_make_get(resp)) is None


def test_non_dict_asset_entries_do_not_raise():
    data = _newer_json()
    data["assets"] = ["junk", None, 42]  # tampered: not dicts
    resp = _FakeResp(200, data)
    info = check_for_update("2.1.0", get=_make_get(resp))
    # Newer tag, but no parseable Setup asset -> still an UpdateInfo, assets None.
    assert info is not None
    assert info.asset_name is None
    assert info.asset_url is None
    assert info.sha256_url is None


def test_default_current_uses_module_version():
    # No explicit current: defaults to __version__ (2.1.0); 2.0.0 is older -> None.
    resp = _FakeResp(200, _newer_json(tag="2.0.0"))
    assert check_for_update(get=_make_get(resp)) is None
