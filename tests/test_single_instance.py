"""Tests for src.single_instance -- the named-mutex single-instance guard.

The Win32 kernel is NEVER touched: the module's ``_kernel32`` accessor is
replaced by a ``FakeKernel32`` so no real OS mutex is created/held. Assertions
cover the load-bearing DoS-resistance contract (threat T-04-06):

  * the first acquire() on a fresh name OWNS the instance (True) and is NOT
    already-running,
  * a SECOND acquire (GetLastError == ERROR_ALREADY_EXISTS / 183) reports
    already-running True / acquire False,
  * the guard NEVER raises on the second instance -- the caller just exits.
"""

import pytest

from src import single_instance


class FakeKernel32:
    """In-memory stand-in for the kernel32 functions the guard uses.

    ``existing`` decides whether CreateMutexW reports a pre-existing mutex: when
    True the next GetLastError returns ERROR_ALREADY_EXISTS (183).
    """

    def __init__(self, *, existing=False, handle=4242):
        self.existing = existing
        self.handle = handle
        self.create_calls = []
        self._last_error = 0

    def CreateMutexW(self, attrs, initial_owner, name):
        self.create_calls.append(name)
        self._last_error = single_instance.ERROR_ALREADY_EXISTS if self.existing else 0
        return self.handle

    def GetLastError(self):
        return self._last_error


def test_first_acquire_owns_instance(monkeypatch):
    fake = FakeKernel32(existing=False)
    monkeypatch.setattr(single_instance, "_kernel32", lambda: fake)

    guard = single_instance.InstanceGuard()
    owns = guard.acquire()

    assert owns is True
    # The stable named mutex was requested.
    assert fake.create_calls == [single_instance.MUTEX_NAME]
    # The handle is retained so the OS keeps the mutex alive for this process.
    assert guard._handle == fake.handle


def test_second_acquire_reports_already_running(monkeypatch):
    fake = FakeKernel32(existing=True)
    monkeypatch.setattr(single_instance, "_kernel32", lambda: fake)

    guard = single_instance.InstanceGuard()
    owns = guard.acquire()

    assert owns is False  # another instance already holds the mutex


def test_acquire_does_not_raise_on_second_instance(monkeypatch):
    """The guard must never raise on the already-running path -- the caller just
    returns 0 cleanly (T-04-06)."""
    fake = FakeKernel32(existing=True)
    monkeypatch.setattr(single_instance, "_kernel32", lambda: fake)

    guard = single_instance.InstanceGuard()
    # Must not raise.
    assert guard.acquire() is False


def test_already_running_convenience(monkeypatch):
    """already_running() returns True when a prior instance holds the mutex."""
    fake = FakeKernel32(existing=True)
    monkeypatch.setattr(single_instance, "_kernel32", lambda: fake)

    assert single_instance.already_running() is True


def test_already_running_false_when_fresh(monkeypatch):
    fake = FakeKernel32(existing=False)
    monkeypatch.setattr(single_instance, "_kernel32", lambda: fake)

    assert single_instance.already_running() is False


def test_mutex_name_is_stable_and_app_scoped():
    assert "BambuLabSystray" in single_instance.MUTEX_NAME
    assert single_instance.ERROR_ALREADY_EXISTS == 183


def test_acquire_tolerates_create_failure(monkeypatch):
    """If CreateMutexW itself fails (returns NULL handle / raises), the guard
    fails open -- it reports ownership so the app still starts (the single-instance
    guard must never be the reason the app can't launch)."""

    class BrokenKernel32:
        def CreateMutexW(self, *a):
            raise OSError("CreateMutexW failed")

        def GetLastError(self):
            return 0

    monkeypatch.setattr(single_instance, "_kernel32", lambda: BrokenKernel32())
    guard = single_instance.InstanceGuard()
    assert guard.acquire() is True  # fails open, no raise
