"""Persistent app settings (region + printer serial) as JSON.

Stored at ``%APPDATA%\\BambuLabSystray\\settings.json`` (path from
:func:`src.paths.appdata_dir`). This survives restarts so a remembered serial can
skip device re-selection and the chosen region is reused (APP-03 ship-readiness).

Security boundary (threat T-04-01): the access token NEVER lives here. It stays in
the Windows Credential Locker via ``token_store``/keyring. ``save_settings`` writes
ONLY the known keys (``region``, ``serial``); any extra key in the input dict -- a
token, a password -- is silently dropped, so a secret can never reach the file.

Robustness:
- T-04-02 (Tampering): a corrupt/non-JSON file falls back to DEFAULTS, and only
  known keys are overlaid, so an injected unknown key is ignored.
- T-04-03 (DoS): ``save_settings`` creates the dir if missing; ``load_settings``
  tolerates a missing file by returning defaults. Neither raises.

No value is logged here (the serial is benign but kept out of logs anyway).
"""

import json
from pathlib import Path

from src.paths import appdata_dir

DEFAULTS = {"region": "global", "serial": None}
_ALLOWED_KEYS = ("region", "serial")


def settings_path() -> Path:
    """Absolute path to the settings JSON under the per-user app dir."""
    return appdata_dir() / "settings.json"


def load_settings() -> dict:
    """Load settings, falling back to a copy of DEFAULTS on any problem.

    Missing file (FileNotFoundError) or corrupt/non-JSON (JSONDecodeError) ->
    ``dict(DEFAULTS)``. On success, start from defaults and overlay ONLY the
    allowed keys present in the parsed object, so unknown/secret keys are ignored.
    """
    result = dict(DEFAULTS)
    try:
        raw = settings_path().read_text(encoding="utf-8")
        parsed = json.loads(raw)
    except (FileNotFoundError, json.JSONDecodeError):
        return result
    if isinstance(parsed, dict):
        for key in _ALLOWED_KEYS:
            if key in parsed:
                result[key] = parsed[key]
    return result


def save_settings(settings: dict) -> None:
    """Persist region + serial to the settings JSON, creating the dir if needed.

    Only ``_ALLOWED_KEYS`` are written -- a token/password key in ``settings`` is
    therefore never serialized. Missing keys fall back to their default.
    """
    appdata_dir().mkdir(parents=True, exist_ok=True)
    payload = {key: settings.get(key, DEFAULTS[key]) for key in _ALLOWED_KEYS}
    settings_path().write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )
