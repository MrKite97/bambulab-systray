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
import logging

import requests

from src.version import __version__, parse_version

logger = logging.getLogger("updater")

# Repo owner is a TODO placeholder until the public repo is created (D-02). The
# user sets the real owner at Phase 12 go-public; until then the check soft-fails
# (404) against this placeholder, which is the correct calm behavior.
REPO = "OWNER-TODO/bambulab-systray"
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
