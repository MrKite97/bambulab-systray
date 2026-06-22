"""Tests for src.autostart -- the HKCU\\Run autostart toggle.

The Windows registry is NEVER touched: ``winreg`` is replaced wholesale by a
``FakeWinreg`` so no real ``HKCU\\...\\Run`` value is ever written. Assertions
cover the load-bearing security + idempotency contracts:

  * is_enabled() reports True/False purely from the Run value's presence,
  * enable() writes EXACTLY RUN_KEY + VALUE_NAME + executable_command(),
  * disable() deletes the value and tolerates an already-absent value,
  * executable_command() derives the command ONLY from the app's own
    interpreter/exe (frozen .exe vs ``"pythonw" -m src.app`` in dev) -- it
    accepts no caller-supplied path (threat T-04-04), and is HKCU-only.
"""

import os
import sys

import pytest

from src import autostart


# --- Fake winreg ------------------------------------------------------------


class FakeKey:
    """A fake open-key handle bound to a backing dict of values."""

    def __init__(self, store, subkey):
        self.store = store
        self.subkey = subkey

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeWinreg:
    """Minimal in-memory stand-in for the stdlib ``winreg`` module.

    Backs a single subkey (RUN_KEY) -> {value_name: value} dict. Mirrors the
    handful of functions autostart.py uses; raises FileNotFoundError for an
    absent value, exactly like the real winreg.
    """

    HKEY_CURRENT_USER = "HKCU"
    KEY_SET_VALUE = 0x0002
    REG_SZ = 1

    def __init__(self):
        self.values = {}  # value_name -> value
        self.set_calls = []  # (subkey, value_name, type, value)
        self.delete_calls = []

    def OpenKey(self, hive, subkey, reserved=0, access=0):
        assert hive == self.HKEY_CURRENT_USER, "must use HKCU, never HKLM"
        return FakeKey(self, subkey)

    def CreateKey(self, hive, subkey):
        assert hive == self.HKEY_CURRENT_USER
        return FakeKey(self, subkey)

    def QueryValueEx(self, key, value_name):
        if value_name not in self.values:
            raise FileNotFoundError(value_name)
        return (self.values[value_name], self.REG_SZ)

    def SetValueEx(self, key, value_name, reserved, type_, value):
        self.set_calls.append((key.subkey, value_name, type_, value))
        self.values[value_name] = value

    def DeleteValue(self, key, value_name):
        self.delete_calls.append((key.subkey, value_name))
        if value_name not in self.values:
            raise FileNotFoundError(value_name)
        del self.values[value_name]


@pytest.fixture
def fake_winreg(monkeypatch):
    fake = FakeWinreg()
    monkeypatch.setattr(autostart, "winreg", fake)
    return fake


# --- is_enabled -------------------------------------------------------------


def test_is_enabled_false_when_value_absent(fake_winreg):
    assert autostart.is_enabled() is False


def test_is_enabled_true_when_value_present(fake_winreg):
    fake_winreg.values[autostart.VALUE_NAME] = r'"C:\app.exe"'
    assert autostart.is_enabled() is True


def test_is_enabled_false_on_missing_run_key(fake_winreg, monkeypatch):
    """If the Run key itself can't be opened (OSError), is_enabled is False."""

    def boom(*a, **k):
        raise OSError("no such key")

    monkeypatch.setattr(fake_winreg, "OpenKey", boom)
    assert autostart.is_enabled() is False


# --- enable -----------------------------------------------------------------


def test_enable_writes_run_value_with_command(fake_winreg, monkeypatch):
    monkeypatch.setattr(autostart, "executable_command", lambda: '"X" -m src.app')
    autostart.enable()

    assert fake_winreg.set_calls == [
        (autostart.RUN_KEY, autostart.VALUE_NAME, fake_winreg.REG_SZ, '"X" -m src.app')
    ]
    assert fake_winreg.values[autostart.VALUE_NAME] == '"X" -m src.app'
    assert autostart.is_enabled() is True


# --- disable ----------------------------------------------------------------


def test_disable_deletes_value(fake_winreg):
    fake_winreg.values[autostart.VALUE_NAME] = r'"C:\app.exe"'
    autostart.disable()
    assert autostart.VALUE_NAME not in fake_winreg.values
    assert autostart.is_enabled() is False


def test_disable_tolerates_absent_value(fake_winreg):
    # No value present -> DeleteValue raises FileNotFoundError; must be swallowed.
    autostart.disable()  # must NOT raise
    assert autostart.is_enabled() is False


# --- executable_command -----------------------------------------------------


def test_executable_command_frozen_quotes_exe(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\Program Files\Bambu\bambu.exe", raising=False)
    cmd = autostart.executable_command()
    assert cmd == r'"C:\Program Files\Bambu\bambu.exe"'
    # No module/script args are appended for the frozen exe.
    assert "-m src.app" not in cmd


def test_executable_command_dev_uses_pythonw_module(monkeypatch, tmp_path):
    """Dev command runs the package as a module via pythonw (windowless) when a
    pythonw.exe sits next to the interpreter; the path comes from sys.executable's
    dir, never a caller argument."""
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    # Place a real pythonw.exe next to the fake interpreter so the preferred
    # (windowless) branch is exercised rather than the fallback.
    (tmp_path / "pythonw.exe").write_text("")
    fake_python = str(tmp_path / "python.exe")
    monkeypatch.setattr(sys, "executable", fake_python, raising=False)
    cmd = autostart.executable_command()
    assert "-m src.app" in cmd
    assert "pythonw" in cmd.lower()
    # The path is derived from sys.executable's dir, never a caller argument.
    assert str(tmp_path) in cmd


def test_executable_command_dev_falls_back_when_no_pythonw(monkeypatch, tmp_path):
    """If pythonw.exe is absent next to the interpreter, fall back to
    sys.executable so the command is still valid."""
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    fake_python = str(tmp_path / "python.exe")
    monkeypatch.setattr(sys, "executable", fake_python, raising=False)
    cmd = autostart.executable_command()
    assert "-m src.app" in cmd
    assert fake_python in cmd


def test_executable_command_takes_no_path_argument():
    """T-04-04: the command is derived from the app's OWN interpreter/exe; the
    function must not accept any caller-supplied path."""
    import inspect

    sig = inspect.signature(autostart.executable_command)
    assert len(sig.parameters) == 0
