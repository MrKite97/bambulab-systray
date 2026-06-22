"""Filesystem path helpers: bundled-resource resolution (dev vs PyInstaller) and
the per-user settings directory.

Two boundaries are crossed here:

1. **Bundled data files** (the icon font in ``assets/``). Run-from-source they sit
   one level above this package; once frozen by PyInstaller ``--onefile`` they are
   extracted to ``sys._MEIPASS``. :func:`resource_path` resolves both so callers
   (``render.py``) never hand-build a path that breaks in the packaged ``.exe``.
2. **The settings directory** under ``%APPDATA%\\BambuLabSystray``. :func:`appdata_dir`
   only computes the path; creation is owned by ``settings.py``.

Stdlib only (sys/os/pathlib): no logging, and no secret is ever referenced here.
The directory name reuses ``token_store.SERVICE`` ("BambuLabSystray") so the token
store and the settings file live under one consistent per-app namespace.
"""

import os
import pathlib
import sys

# Same literal as token_store.SERVICE -- keep the per-app namespace consistent.
_APP_DIR_NAME = "BambuLabSystray"


def resource_path(relative: str) -> str:
    """Resolve a bundled resource path for both dev and PyInstaller-frozen runs.

    ``relative`` is given with forward slashes (e.g. ``"assets/DejaVuSans.ttf"``)
    and is normalized to the OS separator so it works on Windows.

    - Frozen (``sys.frozen`` truthy and ``sys._MEIPASS`` present): join onto the
      PyInstaller extraction dir ``sys._MEIPASS``.
    - Otherwise (run from source): join onto the project root, which is one level
      above this ``src/`` package.
    """
    parts = relative.split("/")
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        base = sys._MEIPASS  # type: ignore[attr-defined]
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


def appdata_dir() -> pathlib.Path:
    """Return the per-user app directory ``%APPDATA%\\BambuLabSystray``.

    Falls back to the user home directory when ``APPDATA`` is unset (non-Windows or
    a stripped environment). Does NOT create the directory -- ``settings.py`` owns
    creation via ``mkdir(parents=True, exist_ok=True)``.
    """
    base = os.environ.get("APPDATA") or pathlib.Path.home()
    return pathlib.Path(base) / _APP_DIR_NAME
