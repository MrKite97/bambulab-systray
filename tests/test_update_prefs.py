"""Tests for src.update_prefs: the JSON update-prefs store at
%APPDATA%\\BambuLabSystray\\update.json.

Mirrors tests/test_settings.py. The OS boundary is mocked by redirecting the
``APPDATA`` env var to a pytest ``tmp_path`` so no real user profile is touched.

Threats under test (Phase 13 register):
- T-13-03 (Tampering): a corrupt/edited update.json falls back to DEFAULTS;
  unknown keys are ignored.
- T-13-04 (Information Disclosure): a stray/secret key in the input dict is never
  serialized (allowlist-on-write, mirroring settings.py T-04-01).
"""

import json

import pytest

from src import update_prefs


@pytest.fixture
def appdata_tmp(monkeypatch, tmp_path):
    """Point APPDATA at a tmp dir so update_prefs_path lands under tmp/BambuLabSystray."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    return tmp_path


# --- defaults / missing file ----------------------------------------------


def test_load_missing_file_returns_defaults(appdata_tmp):
    """No file present -> defaults (auto_update_enabled True), no exception."""
    prefs = update_prefs.load_update_prefs()
    assert prefs == {
        "auto_update_enabled": True,
        "skipped_version": None,
        "last_notified_version": None,
        "last_check": None,
        "etag": None,
    }
    assert prefs["auto_update_enabled"] is True


def test_defaults_are_copied_not_shared(appdata_tmp):
    """Mutating a returned dict must not corrupt the module DEFAULTS."""
    prefs = update_prefs.load_update_prefs()
    prefs["auto_update_enabled"] = False
    assert update_prefs.DEFAULTS["auto_update_enabled"] is True


# --- round-trip ------------------------------------------------------------


def test_save_then_load_round_trips_all_five_keys(appdata_tmp):
    """All five allowlisted keys survive a save/load round-trip."""
    update_prefs.save_update_prefs(
        {
            "auto_update_enabled": False,
            "skipped_version": "2.3.0",
            "last_notified_version": "2.3.0",
            "last_check": "2026-06-27T00:00:00+00:00",
            "etag": 'W/"abc"',
        }
    )
    prefs = update_prefs.load_update_prefs()
    assert prefs["auto_update_enabled"] is False
    assert prefs["skipped_version"] == "2.3.0"
    assert prefs["last_notified_version"] == "2.3.0"
    assert prefs["last_check"] == "2026-06-27T00:00:00+00:00"
    assert prefs["etag"] == 'W/"abc"'


def test_save_creates_appdata_dir_if_missing(appdata_tmp):
    """save_update_prefs creates %APPDATA%/BambuLabSystray when absent."""
    target = appdata_tmp / "BambuLabSystray"
    assert not target.exists()
    update_prefs.save_update_prefs({"auto_update_enabled": True})
    assert target.exists()
    assert (target / "update.json").exists()


# --- corrupt / tampered file ----------------------------------------------


def test_load_corrupt_file_returns_defaults(appdata_tmp):
    """A non-JSON file -> defaults, no exception."""
    path = update_prefs.update_prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json {{{", encoding="utf-8")
    prefs = update_prefs.load_update_prefs()
    assert prefs == dict(update_prefs.DEFAULTS)


def test_load_ignores_unknown_keys(appdata_tmp):
    """Injected unknown keys are dropped; only known keys overlay defaults."""
    path = update_prefs.update_prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "auto_update_enabled": False,
                "skipped_version": "2.5.0",
                "evil": "haxx",
                "access_token": "SECRET",
            }
        ),
        encoding="utf-8",
    )
    prefs = update_prefs.load_update_prefs()
    assert prefs["auto_update_enabled"] is False
    assert prefs["skipped_version"] == "2.5.0"
    assert "evil" not in prefs
    assert "access_token" not in prefs


# --- stray/secret key dropped on save (T-13-04) ---------------------------


def test_stray_key_dropped_on_save(appdata_tmp):
    """A stray/secret key in the input dict is never serialized."""
    secret = "SUPERSECRETTOKEN123"
    update_prefs.save_update_prefs(
        {"auto_update_enabled": True, "access_token": secret, "evil": "x"}
    )
    raw = update_prefs.update_prefs_path().read_text(encoding="utf-8")
    assert secret not in raw
    assert "access_token" not in raw
    payload = json.loads(raw)
    assert set(payload.keys()) == {
        "auto_update_enabled",
        "skipped_version",
        "last_notified_version",
        "last_check",
        "etag",
    }
