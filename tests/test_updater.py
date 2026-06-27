"""Tests for src.updater: the pure, injectable check_for_update.

No real network: ``get`` is injected as a fake callable returning a small
``_FakeResp`` (or raising). Covers the version compare (v-prefix + 2.10>2.9),
soft-fail branches (raise / 304 / 403 / 404), the If-None-Match + Accept headers,
the Setup-asset + .sha256 parse, and the response ETag round-trip.

Threats under test (Phase 13 register):
- T-13-01 (Tampering): non-dict / missing asset fields yield None, never raise.
- T-13-02 (DoS): any exception (incl. timeout) soft-fails to None.
"""

import hashlib

import pytest

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


# --- Phase 14: download_installer (stream + verify size + SHA-256) ---------- #


class _FakeStreamResp:
    """Stand-in for a streaming requests.Response: .iter_content + .text.

    The asset response yields ``chunks`` from .iter_content; the sidecar response
    exposes ``text``. Either may be used for a given fake (whichever the code path
    reads). No real network.
    """

    def __init__(self, *, chunks=None, text=None):
        self._chunks = list(chunks or [])
        self.text = text or ""

    def iter_content(self, chunk_size=None):
        for c in self._chunks:
            yield c


def _info(
    *,
    asset_url="https://example/BambuLabSystray-Setup-2.2.0.exe",
    asset_name="BambuLabSystray-Setup-2.2.0.exe",
    asset_size=None,
    sha256_url="https://example/BambuLabSystray-Setup-2.2.0.exe.sha256",
):
    """Build a minimal UpdateInfo for the download tests."""
    return UpdateInfo(
        version="2.2.0",
        tag="v2.2.0",
        html_url="https://example/releases/tag/v2.2.0",
        asset_name=asset_name,
        asset_url=asset_url,
        asset_size=asset_size,
        sha256_url=sha256_url,
        etag=None,
    )


def _make_download_get(asset_chunks, sidecar_text):
    """Return a fake get(url, ...) that serves asset bytes (stream=True) vs sidecar.

    The streaming asset fetch is identified by ``stream=True``; the sidecar fetch
    (no stream kwarg) returns the .sha256 body via .text.
    """

    def _get(url, *, stream=False, timeout=None):
        if stream:
            return _FakeStreamResp(chunks=asset_chunks)
        return _FakeStreamResp(text=sidecar_text)

    return _get


def test_download_installer_returns_path_when_size_and_sha_match(tmp_path):
    body = b"the installer bytes" * 100
    digest = hashlib.sha256(body).hexdigest()
    info = _info(asset_size=len(body))
    get = _make_download_get([body], f"{digest}  Setup.exe\n")

    path = updater.download_installer(info, get=get, dest_dir=str(tmp_path))

    assert path.exists()
    assert path.read_bytes() == body
    assert path.name == "BambuLabSystray-Setup-2.2.0.exe"


def test_download_installer_sha_mismatch_raises_and_deletes(tmp_path):
    body = b"genuine bytes"
    wrong = hashlib.sha256(b"tampered").hexdigest()
    info = _info()
    get = _make_download_get([body], f"{wrong}  Setup.exe")

    with pytest.raises(updater.UpdateError):
        updater.download_installer(info, get=get, dest_dir=str(tmp_path))

    # The partial/failed download is deleted -- never left on disk.
    assert not (tmp_path / info.asset_name).exists()


def test_download_installer_size_mismatch_raises(tmp_path):
    body = b"0123456789"
    digest = hashlib.sha256(body).hexdigest()
    info = _info(asset_size=999)  # advertised size != actual streamed bytes
    get = _make_download_get([body], f"{digest}  Setup.exe")

    with pytest.raises(updater.UpdateError):
        updater.download_installer(info, get=get, dest_dir=str(tmp_path))
    assert not (tmp_path / info.asset_name).exists()


def test_download_installer_get_raises_no_partial(tmp_path):
    info = _info()

    def _boom(url, *, stream=False, timeout=None):
        raise ConnectionError("offline")

    with pytest.raises(updater.UpdateError):
        updater.download_installer(info, get=_boom, dest_dir=str(tmp_path))
    assert not (tmp_path / info.asset_name).exists()


def test_download_installer_no_sha_url_raises_without_get(tmp_path):
    info = _info(sha256_url=None)
    called = {"n": 0}

    def _get(url, *, stream=False, timeout=None):
        called["n"] += 1
        return _FakeStreamResp(chunks=[b"x"])

    with pytest.raises(updater.UpdateError):
        updater.download_installer(info, get=_get, dest_dir=str(tmp_path))
    assert called["n"] == 0  # never trust -> never even fetch


def test_download_installer_no_asset_url_raises_without_get(tmp_path):
    info = _info(asset_url=None)
    called = {"n": 0}

    def _get(url, *, stream=False, timeout=None):
        called["n"] += 1
        return _FakeStreamResp(chunks=[b"x"])

    with pytest.raises(updater.UpdateError):
        updater.download_installer(info, get=_get, dest_dir=str(tmp_path))
    assert called["n"] == 0


def test_download_installer_sha_is_case_insensitive(tmp_path):
    body = b"case insensitive hash check"
    digest = hashlib.sha256(body).hexdigest().upper()  # sidecar in UPPER case
    info = _info(asset_size=len(body))
    get = _make_download_get([body], f"{digest}  Setup.exe")

    path = updater.download_installer(info, get=get, dest_dir=str(tmp_path))
    assert path.exists()


# --- Phase 14: spawn_installer (detached, silent, relaunch) ----------------- #


class _SpawnRecorder:
    """Records the single spawn call (args + kwargs) and a no-wait handle."""

    def __init__(self):
        self.calls = []
        self.handle = _FakeProcHandle()

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        return self.handle


class _FakeProcHandle:
    """A fake Popen return value whose .wait increments a counter (asserted 0)."""

    def __init__(self):
        self.wait_count = 0

    def wait(self, *a, **k):
        self.wait_count += 1


def test_spawn_installer_exact_args_and_flags():
    rec = _SpawnRecorder()
    result = updater.spawn_installer(r"C:\Temp\Setup.exe", spawn=rec)

    assert result is None
    assert len(rec.calls) == 1
    args, kwargs = rec.calls[0]
    assert args == [
        r"C:\Temp\Setup.exe",
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/CLOSEAPPLICATIONS",
        "/RESTARTAPPLICATIONS",
    ]
    assert kwargs["creationflags"] == 0x208
    assert kwargs["close_fds"] is True


def test_spawn_installer_does_not_wait():
    rec = _SpawnRecorder()
    updater.spawn_installer(r"C:\Temp\Setup.exe", spawn=rec)
    assert rec.handle.wait_count == 0  # returns immediately, never waits
