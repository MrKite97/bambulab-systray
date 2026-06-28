"""The pure update-check core: ``check_for_update(...) -> UpdateInfo | None``.

PURE + INJECTABLE + SOFT-FAIL (UPD-03). No I/O at import, no threads. The
``requests.get`` callable is injected (default ``requests.get``) so the whole
module is driven by fakes in tests -- no real network ever runs in unit tests.

Soft-fail contract (D-04): ANY problem -> ``None``, NEVER raise, NEVER pop up:
- the injected ``get`` raising (offline / timeout / connection error),
- HTTP 403 (rate limit), 404 (no releases yet), 304 (not modified),
- any malformed/tampered JSON.
Logged at debug only. The daemon thread (Plan 02) can therefore never hang or
crash on a bad release.

Version compare (D-03) reuses :func:`src.version.parse_version` -- NEVER a string
'>' compare -- so ``2.10.0 > 2.9.0`` is correct and a leading 'v' is tolerated.

Phase 13 DOES NOT download anything. It only PARSES the Windows Setup ``.exe``
asset (and its ``.sha256`` sidecar) into ``asset_url``/``sha256_url`` so Phase 14
can consume them later (UPD-06 SHA-256 verify gates execution there, not here).
"""

from dataclasses import dataclass
import hashlib
import logging
import os
import subprocess
import tempfile
from pathlib import Path

import requests

from src.version import __version__, parse_version

logger = logging.getLogger("updater")

# The public GitHub repo the update check + 1-click update read from (D-02).
# Set at Phase 12 go-public; the check soft-fails (404/offline) if it ever can't
# reach this, which is the correct calm behavior.
REPO = "MrKite97/bambulab-systray"
_API_URL = "https://api.github.com/repos/{repo}/releases/latest"
_ACCEPT = "application/vnd.github+json"


@dataclass(frozen=True)
class UpdateInfo:
    """A newer-than-current release found on GitHub (public metadata only)."""

    version: str  # no leading 'v'
    tag: str  # raw tag_name (may have 'v')
    html_url: str  # release page (UPD-05 "Wat is er nieuw?")
    asset_name: str | None
    asset_url: str | None  # browser_download_url of the Setup .exe (Phase 14)
    asset_size: int | None
    sha256_url: str | None  # the .sha256 sidecar URL, or None
    etag: str | None  # response ETag for caching


def _parse_setup_asset(assets):
    """Return (name, url, size, sha256_url) for the Setup .exe + its .sha256, or Nones.

    Every field is read via ``.get`` (T-13-01): a non-dict / missing-field asset
    yields None, never an index/attr error.
    """
    setup = None
    for a in assets:
        name = (a.get("name") or "") if isinstance(a, dict) else ""
        low = name.lower()
        if low.endswith(".exe") and "setup" in low:
            setup = a
            break
    if setup is None:
        return (None, None, None, None)
    name = setup.get("name")
    url = setup.get("browser_download_url")
    size = setup.get("size")
    sha_url = None
    target = f"{name}.sha256".lower()
    for a in assets:
        if isinstance(a, dict) and (a.get("name") or "").lower() == target:
            sha_url = a.get("browser_download_url")
            break
    return (name, url, size, sha_url)


def check_for_update(
    current: str | None = None,
    *,
    get=requests.get,
    etag: str | None = None,
    repo: str = REPO,
) -> UpdateInfo | None:
    """SOFT-FAIL: return None on ANY error / 403 / 404 / 304; never raise (UPD-03).

    ``current`` defaults to ``version.__version__``. ``etag``, when given, is sent
    as ``If-None-Match`` so an unchanged release returns 304 (-> None). The returned
    UpdateInfo carries the response ETag for caching.
    """
    current_v = parse_version(current)  # defaults to __version__
    headers = {"Accept": _ACCEPT}
    if etag:
        headers["If-None-Match"] = etag
    try:
        resp = get(_API_URL.format(repo=repo), headers=headers, timeout=10)
    except Exception:  # noqa: BLE001 - offline/timeout/etc soft-fails to None
        logger.debug("update check failed (network); soft-fail to None")
        return None

    status = getattr(resp, "status_code", None)
    if status in (304, 403, 404):
        logger.debug("update check non-actionable status %s; None", status)
        return None
    try:
        if status is not None and status >= 400:
            return None
        data = resp.json()
    except Exception:  # noqa: BLE001 - malformed body soft-fails to None
        return None
    if not isinstance(data, dict):
        return None

    tag = str(data.get("tag_name") or "")
    if not tag:
        return None
    try:
        latest_v = parse_version(tag)
    except Exception:  # noqa: BLE001 - unparseable tag soft-fails to None
        return None
    if latest_v <= current_v:
        return None

    asset_name, asset_url, asset_size, sha256_url = _parse_setup_asset(
        data.get("assets") or []
    )

    resp_etag = None
    headers_obj = getattr(resp, "headers", None)
    if headers_obj is not None:
        try:
            resp_etag = headers_obj.get("ETag")
        except Exception:  # noqa: BLE001 - odd headers object -> no etag
            resp_etag = None

    return UpdateInfo(
        version=str(latest_v),
        tag=tag,
        html_url=str(data.get("html_url") or ""),
        asset_name=asset_name,
        asset_url=asset_url,
        asset_size=asset_size,
        sha256_url=sha256_url,
        etag=resp_etag,
    )


# --- Phase 14: download + verify + spawn (UPD-06, verify-before-trust) ------ #


class UpdateError(Exception):
    """A 1-click update could not be completed safely (download or integrity
    failure). Raised by download_installer so the caller surfaces a VISIBLE
    failure (flyout.push_error) and NEVER spawns the installer (UPD-06)."""


_DOWNLOAD_CHUNK = 1024 * 256  # 256 KiB streaming chunks


def _expected_sha256(sha_text: str) -> str:
    """First whitespace-delimited token of the .sha256 sidecar, lowercased."""
    tokens = (sha_text or "").split()
    if not tokens:
        raise UpdateError("empty .sha256 sidecar")
    return tokens[0].strip().lower()


def _delete_partial(path: Path) -> None:
    """Best-effort delete of a failed/partial download (never raises)."""
    try:
        os.remove(path)
    except OSError:
        pass


def download_installer(
    info: "UpdateInfo",
    *,
    get=requests.get,
    dest_dir=None,
) -> Path:
    """Stream the Setup asset to a temp dir and verify size + SHA-256 BEFORE
    returning its Path. Raise UpdateError on ANY mismatch/error (and delete the
    partial file); NEVER return a path on failure, NEVER spawn (D-01/D-02/D-03).

    ``get`` is injectable (default requests.get) so tests use a fake -- no real
    network. ``dest_dir`` defaults to the system temp dir.
    """
    if not info.asset_url or not info.sha256_url:
        # Cannot verify integrity -> never trust. (No asset or no sidecar.)
        raise UpdateError("update asset or .sha256 sidecar missing")

    dest_dir = dest_dir or tempfile.gettempdir()
    dest = Path(dest_dir) / (info.asset_name or "BambuLabSystray-Setup.exe")

    sha = hashlib.sha256()
    size = 0
    try:
        # 1) Stream the binary asset to disk, hashing as we write.
        resp = get(info.asset_url, stream=True, timeout=60)
        with open(dest, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=_DOWNLOAD_CHUNK):
                if not chunk:
                    continue
                fh.write(chunk)
                sha.update(chunk)
                size += len(chunk)

        # 2) Verify size (when the release advertised one).
        if info.asset_size is not None and size != info.asset_size:
            raise UpdateError(
                f"size mismatch: got {size}, expected {info.asset_size}"
            )

        # 3) Fetch the .sha256 sidecar and compare (case-insensitive).
        sha_resp = get(info.sha256_url, timeout=30)
        expected = _expected_sha256(getattr(sha_resp, "text", "") or "")
        actual = sha.hexdigest().lower()
        if actual != expected:
            raise UpdateError("SHA-256 mismatch")
    except UpdateError:
        _delete_partial(dest)
        raise
    except Exception as exc:  # noqa: BLE001 - any download/IO error -> UpdateError
        _delete_partial(dest)
        raise UpdateError(f"download failed: {type(exc).__name__}") from exc

    return dest


# Win32 process-creation flags: run the installer FULLY DETACHED so it survives
# this app's imminent exit and never shares our console/handles (D-04).
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200

# The EXACT silent + relaunch flags the Phase 11 installer declares
# (installer/bambulab-systray.iss). Order + spelling are load-bearing.
INSTALLER_SILENT_ARGS = (
    "/VERYSILENT",
    "/SUPPRESSMSGBOXES",
    "/NORESTART",
    "/CLOSEAPPLICATIONS",
    "/RESTARTAPPLICATIONS",
)


def spawn_installer(installer_path, *, spawn=subprocess.Popen) -> None:
    """Launch the verified installer FULLY DETACHED and return immediately (D-04/D-05).

    The installer is started with the exact silent + relaunch flags the Phase 11
    .iss supports and with DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP so it
    outlives THIS process. It does NOT wait -- the caller proceeds straight to the
    locked quit sequence so the app exits.

    IMPORTANT (the load-bearing ordering, see make_update_apply): the caller MUST
    have already freed the single-instance mutex (single_instance.release) BEFORE
    calling this. Inno's AppMutex=Global\\BambuLabSystray_singleton check ABORTS the
    silent install if the mutex still exists -- it does NOT block/wait for it to
    clear (a long-standing misconception). With the mutex freed, the installer's
    CloseApplications/RestartApplications (Restart Manager) close+relaunch the app
    to swap the exe non-elevated.

    ``spawn`` is injectable (default subprocess.Popen) so tests assert the exact
    args + creationflags with a recorder -- no real process is launched.
    """
    args = [str(installer_path), *INSTALLER_SILENT_ARGS]
    spawn(
        args,
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
        stdin=None,
        stdout=None,
        stderr=None,
    )
    # Deliberately NO .wait(): return immediately so app.py can run the locked
    # quit order. The installer waits on AppMutex for our exit.
