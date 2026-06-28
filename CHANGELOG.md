# Changelog

All notable changes to Bambu Lab Systray are documented here. Versions follow
[semantic versioning](https://semver.org/); the version comes from the git tag
(`v*`) and is the single source of truth (`src/version.py`).

## v2.1.3

- Test release to confirm the fixed 1-click self-update works from a build that
  contains the v2.1.2 updater fixes. No functional changes.

## v2.1.2

Fixes four bugs that broke the auto-update path end-to-end (found during the
clean-machine self-update verification):

- **Update check now reliably detects new releases.** The periodic check no longer
  re-sends the cached ETag, which made GitHub answer `304 Not Modified` and the app
  conclude "no update" even when a newer version existed — so detection silently
  died after the first poll/restart and "Controleer op updates" wrongly reported
  "Je gebruikt de nieuwste versie".
- **"Nu bijwerken" works after a manual check.** A manually-discovered update is now
  stashed for the apply handler, instead of failing with "Bijwerken mislukt".
- **The silent installer no longer aborts on its own app mutex.** The app now frees
  the single-instance mutex before launching the installer (Inno's `AppMutex` aborts
  a silent install if the mutex still exists — it does not wait for it).
- **The one-file exe can now be replaced in place.** On self-update the app fully
  terminates its PyInstaller bootloader process so the installer can overwrite the
  (previously locked) `.exe` and relaunch the new version.

Note: updating *from* v2.1.0/v2.1.1 to this release still needs a one-time manual
install (those builds predate these fixes); self-update works from v2.1.2 onward.

## v2.1.1

- Test release to validate the in-app update notification + 1-click self-update
  flow end-to-end against a real GitHub Release. No functional changes.

## v2.1.0

- First public release: per-user Inno Setup installer (no UAC), tray icon with
  remaining-time glyph, flyout panel with live progress + controls, autostart
  toggle, and the in-app update check / dual notification / 1-click self-update.
- Ships unsigned — first run shows Windows SmartScreen ("Meer info → Toch
  uitvoeren"); see the README.
