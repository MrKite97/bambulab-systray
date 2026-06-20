# Bambu Lab Systray

A Windows 11 system-tray app that follows the active print of a Bambu Lab 3D
printer over the Bambu cloud, showing remaining time on the tray icon and
progress (%) + remaining time in the hover tooltip.

## Phase 1 — Cloud Connection Spike (console only)

Phase 1 is a console-only spike that proves the full Bambu cloud path
end-to-end (login → device list → MQTT-over-TLS → live print status) before any
UI is built. The tray icon, tooltip, autostart and packaging arrive in later
phases.

### Requirements

- Windows 11
- Python 3.12 (use the `python` command, not `python3`)

### Setup (PowerShell)

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### Running the spike

The spike entry point is:

```powershell
python -m src.spike
```

> Note: `src.spike` is created in plan 01-04. Running the spike requires a
> **real Bambu EU account** and an **active multi-hour print** to validate —
> the empirical heart of this phase is confirming, against a live print, that
> `mc_remaining_time` is reported in **minutes** (not seconds, which would be a
> 60× error).

### Token storage

The Bambu access token is stored encrypted in the **Windows Credential Manager**
(DPAPI, per-user) under the service name `BambuLabSystray`. It is **never**
written to a plaintext file, and the account password is never persisted — only
the access token is stored.
