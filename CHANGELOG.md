# Changelog

All notable changes to Bambu Lab Systray are documented here. Versions follow
[semantic versioning](https://semver.org/); the version comes from the git tag
(`v*`) and is the single source of truth (`src/version.py`).

## v2.1.1

- Test release to validate the in-app update notification + 1-click self-update
  flow end-to-end against a real GitHub Release. No functional changes.

## v2.1.0

- First public release: per-user Inno Setup installer (no UAC), tray icon with
  remaining-time glyph, flyout panel with live progress + controls, autostart
  toggle, and the in-app update check / dual notification / 1-click self-update.
- Ships unsigned — first run shows Windows SmartScreen ("Meer info → Toch
  uitvoeren"); see the README.
