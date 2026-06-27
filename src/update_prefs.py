"""Persistent update preferences as JSON, kept SEPARATE from settings.py (D-06).

Stored at ``%APPDATA%\\BambuLabSystray\\update.json`` (path from
:func:`src.paths.appdata_dir`). This carries the auto-update toggle, the
skipped/last-notified version bookkeeping, and the ETag + last-check timestamp the
periodic update checker uses.

Why a SEPARATE file from settings.py: settings.py guards a narrow secret boundary
-- it allowlists ONLY ``region``/``serial`` so a token can never slip into it. Update
prefs carry no secret, but mixing them into settings.py would widen that allowlist
and blur the boundary. Keeping update.json in its own allowlisted file preserves
settings.py's region/serial-only contract intact. This module imports NO
token_store/keyring -- it never touches credentials.

Robustness (mirrors settings.py):
- T-13-03 (Tampering): a corrupt/non-JSON update.json falls back to DEFAULTS, and
  only ``_ALLOWED_KEYS`` are overlaid, so an injected unknown key is ignored.
- T-13-04 (Information Disclosure): ``save_update_prefs`` writes ONLY the five
  known keys; a stray/secret key in the input dict is silently dropped, so it can
  never reach disk (mirrors settings.py T-04-01).
- A missing %APPDATA% dir is created on save; a missing file loads as defaults.

No value is logged here.
"""

import json
from pathlib import Path

from src.paths import appdata_dir

DEFAULTS = {
    "auto_update_enabled": True,
    "skipped_version": None,
    "last_notified_version": None,
    "last_check": None,
    "etag": None,
}
_ALLOWED_KEYS = (
    "auto_update_enabled",
    "skipped_version",
    "last_notified_version",
    "last_check",
    "etag",
)


def update_prefs_path() -> Path:
    """Absolute path to the update-prefs JSON under the per-user app dir."""
    return appdata_dir() / "update.json"


def load_update_prefs() -> dict:
    """Load update prefs, falling back to a copy of DEFAULTS on any problem.

    Missing file (FileNotFoundError) or corrupt/non-JSON (JSONDecodeError) ->
    ``dict(DEFAULTS)``. On success, start from defaults and overlay ONLY the
    allowed keys present in the parsed object, so unknown/secret keys are ignored.
    """
    result = dict(DEFAULTS)
    try:
        raw = update_prefs_path().read_text(encoding="utf-8")
        parsed = json.loads(raw)
    except (FileNotFoundError, json.JSONDecodeError):
        return result
    if isinstance(parsed, dict):
        for key in _ALLOWED_KEYS:
            if key in parsed:
                result[key] = parsed[key]
    return result


def save_update_prefs(prefs: dict) -> None:
    """Persist the allowlisted update prefs to update.json, creating the dir if needed.

    Only ``_ALLOWED_KEYS`` are written -- a stray/secret key in ``prefs`` is
    therefore never serialized. Missing keys fall back to their default.
    """
    appdata_dir().mkdir(parents=True, exist_ok=True)
    payload = {key: prefs.get(key, DEFAULTS[key]) for key in _ALLOWED_KEYS}
    update_prefs_path().write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )
