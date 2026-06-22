"""Single-instance guard via a named Win32 mutex (ctypes).

A second launch of the tray app must exit cleanly WITHOUT disturbing the first
(threat T-04-06, DoS). We use a named kernel mutex rather than a lockfile because
the kernel owns the name globally and auto-releases the handle when the owning
process dies -- there is no stale-lockfile cleanup problem (a crashed first
instance can't wedge the name), and the check is a single atomic ``CreateMutexW``
+ ``GetLastError`` with no filesystem race. The mutex carries NO secret -- just a
fixed app-scoped name (threat T-04-05).

The guard never raises on the second instance: :meth:`InstanceGuard.acquire`
returns False (and :func:`already_running` returns True) so ``main()`` can log one
line and ``return 0``. ``ctypes.windll`` is accessed lazily through
:func:`_kernel32` so importing this module (and collecting its tests) does not
hard-crash on a non-Windows environment.
"""

import ctypes
import logging

logger = logging.getLogger("single_instance")

# A stable, app-scoped mutex name. "Global\\" makes it visible across sessions;
# the literal reuses the app namespace (token_store.SERVICE) for consistency.
MUTEX_NAME = "Global\\BambuLabSystray_singleton"

# Win32: a second CreateMutexW on an existing name returns the handle but sets
# GetLastError to ERROR_ALREADY_EXISTS.
ERROR_ALREADY_EXISTS = 183


def _kernel32():
    """Return the kernel32 API surface (lazy so non-Windows import won't crash).

    Tests monkeypatch this to inject a fake kernel32, so all OS access funnels
    through here.
    """
    return ctypes.windll.kernel32  # type: ignore[attr-defined]


class InstanceGuard:
    """Owns the named mutex for the lifetime of THIS process.

    Keep the instance alive for the whole app run so the handle is not GC'd while
    the app is running -- releasing it would free the name and let a later launch
    believe it is the first instance.
    """

    def __init__(self):
        self._handle = None

    def acquire(self) -> bool:
        """Try to become the single instance.

        Returns True if THIS process now owns the single instance (the mutex did
        NOT already exist), False if another instance already holds it
        (GetLastError == ERROR_ALREADY_EXISTS). Never raises: if the OS call
        itself fails, we fail OPEN (return True) so the guard can never be the
        reason the app refuses to start.
        """
        try:
            k = _kernel32()
            # CreateMutexW(lpAttributes=NULL, bInitialOwner=FALSE, lpName).
            self._handle = k.CreateMutexW(None, False, MUTEX_NAME)
            return k.GetLastError() != ERROR_ALREADY_EXISTS
        except Exception:  # noqa: BLE001 - guard must never block startup
            logger.debug("single-instance check failed; failing open (allowing start)")
            return True


def already_running() -> bool:
    """Convenience wrapper: True iff another instance already holds the mutex.

    Builds a module-level guard so the handle is retained for the process lifetime
    (the returned value answers "should this second instance bow out?").
    """
    return not _GUARD.acquire()


# Process-lifetime guard so the mutex handle outlives a transient call.
_GUARD = InstanceGuard()
