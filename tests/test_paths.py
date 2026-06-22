"""Tests for src.paths: the resource-path helper (dev vs PyInstaller frozen) and
the %APPDATA% settings directory.

The OS boundary is mocked: ``sys.frozen`` / ``sys._MEIPASS`` are monkeypatched to
simulate a PyInstaller one-file build, and the ``APPDATA`` env var is redirected to
a tmp path so the real user profile is never touched.
"""

import os

import pytest

from src.paths import appdata_dir, resource_path


# --- resource_path ---------------------------------------------------------


def test_resource_path_from_source_resolves_existing_asset():
    """Run-from-source: resource_path returns an absolute path to the bundled font
    that actually exists on disk and ends with assets/DejaVuSans.ttf."""
    p = resource_path("assets/DejaVuSans.ttf")
    assert os.path.isabs(p)
    assert os.path.exists(p)
    assert p.replace("\\", "/").endswith("assets/DejaVuSans.ttf")


def test_resource_path_frozen_uses_meipass(monkeypatch):
    """When frozen (sys.frozen True + sys._MEIPASS set), resource_path joins the
    relative path onto _MEIPASS (the PyInstaller temp extraction dir)."""
    fake_meipass = os.path.join("C", "tmp", "_MEI12345")
    monkeypatch.setattr("sys.frozen", True, raising=False)
    monkeypatch.setattr("sys._MEIPASS", fake_meipass, raising=False)
    p = resource_path("assets/DejaVuSans.ttf")
    assert p == os.path.join(fake_meipass, "assets", "DejaVuSans.ttf")


def test_resource_path_not_frozen_ignores_meipass(monkeypatch):
    """sys.frozen False -> _MEIPASS is ignored even if present (dev layout wins)."""
    monkeypatch.setattr("sys.frozen", False, raising=False)
    monkeypatch.setattr("sys._MEIPASS", os.path.join("C", "should", "not", "be", "used"),
                        raising=False)
    p = resource_path("assets/DejaVuSans.ttf")
    assert os.path.exists(p)
    assert "should" not in p


# --- appdata_dir -----------------------------------------------------------


def test_appdata_dir_under_appdata_env(monkeypatch, tmp_path):
    """appdata_dir() points at <%APPDATA%>/BambuLabSystray when APPDATA is set."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    d = appdata_dir()
    assert d == tmp_path / "BambuLabSystray"
    assert d.name == "BambuLabSystray"


def test_appdata_dir_does_not_create_dir(monkeypatch, tmp_path):
    """appdata_dir() only computes a path; it must NOT create the directory
    (settings.py owns creation)."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    d = appdata_dir()
    assert not d.exists()


def test_appdata_dir_falls_back_to_home(monkeypatch):
    """With APPDATA unset, appdata_dir() falls back to the user home dir base."""
    monkeypatch.delenv("APPDATA", raising=False)
    import pathlib

    d = appdata_dir()
    assert d == pathlib.Path.home() / "BambuLabSystray"
    assert d.name == "BambuLabSystray"
