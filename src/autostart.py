"""Per-user Windows autostart via the ``HKCU\\...\\Run`` registry key.

Registers/unregisters the app under
``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run`` using stdlib
``winreg`` -- the per-user run location, so NO admin/elevation is needed and the
autostart only ever affects the current user (never HKLM / all-users).

Security boundary (threat T-04-04, Tampering/Elevation): the auto-run command is
derived EXCLUSIVELY from the app's own interpreter/executable via
:func:`executable_command` -- it accepts no caller-supplied path, so no arbitrary
path can be injected into the auto-run key. When frozen by PyInstaller the value
is the quoted ``sys.executable`` (the packaged ``.exe``); in dev it is
``"<pythonw>" -m src.app`` so the source tree starts windowless on login.

No secret is referenced or logged here: the Run value is just the exe path; the
access token stays in the Windows Credential Locker via ``token_store``.
"""

import os
import sys

try:  # Guard the import so test collection works on non-Windows CI.
    import winreg  # type: ignore
except ImportError:  # pragma: no cover - tests inject a FakeWinreg anyway
    winreg = None  # type: ignore

# The per-user auto-run subkey and our value name (reuses token_store.SERVICE so
# the registry value, the credential entry, and %APPDATA% dir share one name).
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "BambuLabSystray"


def executable_command() -> str:
    """Return the auto-run command, derived ONLY from this app's own binary.

    - Frozen (PyInstaller ``--onefile``): the quoted absolute ``sys.executable``
      -- the packaged ``.exe``. No script/module args are appended.
    - Dev (run from source): ``"<pythonw>" -m src.app`` where ``<pythonw>`` is
      ``pythonw.exe`` next to ``sys.executable`` (windowless), falling back to
      ``sys.executable`` itself if ``pythonw.exe`` is not found.

    Takes NO argument: the command can never be a caller-supplied path (T-04-04).
    """
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable
    return f'"{pythonw}" -m src.app'


def is_enabled() -> bool:
    """Return True iff the Run value ``BambuLabSystray`` is present under HKCU.

    A missing value (FileNotFoundError) or an unopenable Run key (OSError) both
    mean "not registered" -> False, never an exception.
    """
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, VALUE_NAME)
        return True
    except (FileNotFoundError, OSError):
        return False


def enable() -> None:
    """Register autostart by writing the Run value = :func:`executable_command`.

    ``CreateKey`` is idempotent (opens the key if it already exists), so this is
    safe to call repeatedly. HKCU only -- no admin rights required.
    """
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, executable_command())


def disable() -> None:
    """Unregister autostart by deleting the Run value. Idempotent: an
    already-absent value (FileNotFoundError) is swallowed so a double-disable or a
    toggle-off-when-never-on never raises."""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, VALUE_NAME)
    except FileNotFoundError:
        pass
