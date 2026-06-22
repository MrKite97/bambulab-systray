"""Tests for src.settings: the JSON region+serial store at
%APPDATA%\\BambuLabSystray\\settings.json.

The OS boundary is mocked by redirecting the ``APPDATA`` env var to a pytest
``tmp_path`` so no real user profile is touched. Threats under test:

- T-04-01 (Information Disclosure): an access token must never reach the file.
- T-04-02 (Tampering): a corrupt/edited file falls back to defaults; unknown keys
  are ignored.
- T-04-03 (DoS): a missing %APPDATA% dir is created on save; absence -> defaults.
"""

import json

import pytest

from src import settings


@pytest.fixture
def appdata_tmp(monkeypatch, tmp_path):
    """Point APPDATA at a tmp dir so settings_path lands under tmp/BambuLabSystray."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    return tmp_path


# --- defaults / missing file ----------------------------------------------


def test_load_settings_missing_file_returns_defaults(appdata_tmp):
    """No file present -> defaults, no exception."""
    s = settings.load_settings()
    assert s == {"region": "global", "serial": None}


def test_defaults_are_copied_not_shared(appdata_tmp):
    """Mutating a returned dict must not corrupt the module DEFAULTS."""
    s = settings.load_settings()
    s["region"] = "china"
    assert settings.DEFAULTS["region"] == "global"


# --- round-trip ------------------------------------------------------------


def test_save_then_load_round_trips_region_and_serial(appdata_tmp):
    """region + serial survive a save/load round-trip."""
    settings.save_settings({"region": "global", "serial": "01ABC"})
    s = settings.load_settings()
    assert s["region"] == "global"
    assert s["serial"] == "01ABC"


def test_save_creates_appdata_dir_if_missing(appdata_tmp):
    """save_settings creates %APPDATA%/BambuLabSystray when absent."""
    target = appdata_tmp / "BambuLabSystray"
    assert not target.exists()
    settings.save_settings({"region": "global", "serial": "01ABC"})
    assert target.exists()
    assert (target / "settings.json").exists()


# --- corrupt / tampered file ----------------------------------------------


def test_load_settings_corrupt_file_returns_defaults(appdata_tmp):
    """A non-JSON file -> defaults, no exception."""
    path = settings.settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("this is not json {{{", encoding="utf-8")
    s = settings.load_settings()
    assert s == {"region": "global", "serial": None}


def test_load_settings_ignores_unknown_keys(appdata_tmp):
    """Injected unknown keys are dropped; only known keys overlay defaults."""
    path = settings.settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"region": "china", "serial": "01XYZ", "evil": "haxx", "admin": True}),
        encoding="utf-8",
    )
    s = settings.load_settings()
    assert s == {"region": "china", "serial": "01XYZ"}
    assert "evil" not in s
    assert "admin" not in s


# --- token never persisted (T-04-01) --------------------------------------


def test_token_never_written_to_disk(appdata_tmp):
    """A save dict that accidentally includes a token must NOT write it to disk."""
    secret = "SUPERSECRETTOKEN123"
    settings.save_settings({"region": "global", "serial": "01ABC", "access_token": secret})
    raw = settings.settings_path().read_text(encoding="utf-8")
    assert secret not in raw
    assert "access_token" not in raw
    # And only the allowed keys are present in the parsed payload.
    payload = json.loads(raw)
    assert set(payload.keys()) == {"region", "serial"}


def test_settings_does_not_call_token_store():
    """settings.py must not import or call token_store/keyring (token stays out).

    We assert on the actual code -- imports and calls -- not on prose: docstrings
    are stripped before scanning so an explanatory comment cannot trip the check.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(settings))

    # No import of token_store or keyring anywhere.
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "token_store" not in alias.name
                assert "keyring" not in alias.name
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            assert "token_store" not in mod
            assert "keyring" not in mod

    # Strip docstrings, then assert no token call-site survives in the code.
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            if (
                node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            ):
                node.body = node.body[1:]
    code_no_docs = ast.unparse(tree)
    assert "save_token" not in code_no_docs
    assert "load_token" not in code_no_docs
    assert "access_token" not in code_no_docs
