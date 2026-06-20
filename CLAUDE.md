<!-- GSD:project-start source:PROJECT.md -->
## Project

**Bambu Lab Systray**

Een Windows 11 systeemvak-app (system tray) die de actieve print van een Bambu Lab 3D-printer volgt via de Bambu cloud. De app draait stil op de achtergrond en laat in één oogopslag zien hoe ver de huidige print is: de resterende tijd staat op het tray-icoon, en bij hover toont de tooltip de voortgang in procenten plus de resterende tijd.

**Core Value:** Met één blik op de taakbalk weten hoe ver de actieve print is — zonder een app te openen, in te loggen of ergens op te klikken.

### Constraints

- **Platform**: Windows 11 systray-app — moet als achtergrond-app in het systeemvak leven.
- **Connectiviteit**: Bambu cloud (geen LAN) — afhankelijk van Bambu's (ongedocumenteerde) cloud-auth en MQTT-stroom; risico op wijzigingen aan Bambu's kant.
- **Authenticatie**: vereist Bambu-accountgegevens; mogelijk 2FA/region-handling en veilige opslag van tokens.
- **Footprint**: lichtgewicht, lage CPU/geheugen — draait permanent op de achtergrond.
- **Scope**: bewust klein en alleen-lezen; geen printeraansturing.
<!-- GSD:project-end -->

<!-- GSD:stack-start source:research/STACK.md -->
## Technology Stack

## Recommendation in one line
## Recommended Stack
### Core Technologies
| Technology | Version | Purpose | Why Recommended |
|------------|---------|---------|-----------------|
| **Python** | 3.12.x (3.11+ OK) | App language/runtime | The entire reverse-engineered Bambu ecosystem (`pybambu`, `bambulab-cloud-py`, HA integration) is Python. You can read and copy the *exact, currently-working* cloud auth + MQTT field parsing instead of re-deriving it. Drawing text on an icon + TLS MQTT + tray are all trivial. Lowest total risk for this specific domain. |
| **paho-mqtt** | 2.1.0 | MQTT-over-TLS client to Bambu cloud broker | The de-facto Python MQTT client (Eclipse). Mature TLS support on port 8883, used by pybambu itself. v2.x has a new callback API (`CallbackAPIVersion.VERSION2`) — pin to it deliberately. |
| **pystray** | 0.19.5 | System tray icon + menu + tooltip on Windows | Accepts a `PIL.Image` as the icon and lets you **reassign `icon.icon` at runtime** to redraw it, and set `icon.title` for the hover tooltip. This is the mechanism that makes "remaining time drawn on the icon, % + time in tooltip" work. Uses `Shell_NotifyIcon` under the hood. |
| **Pillow (PIL)** | 12.2.0 | Generate the 16×16 icon image with text drawn on it | `Image.new()` + `ImageDraw.Draw().text()` renders `1:23` onto the tray bitmap each time remaining time changes. This is the load-bearing "draw text onto the icon" piece. See note below on sizing. |
### Supporting Libraries
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| **keyring** | 25.7.0 | Store the Bambu access token in Windows Credential Manager (DPAPI-backed) | Always. On Windows, `keyring` uses the Windows Credential Locker — token is encrypted per-user via DPAPI. Never write the token to a plain file. |
| **requests** | latest (2.32+) | HTTPS calls for the cloud login/2FA flow | Always — the login/verification-code exchange is plain REST before MQTT. (`httpx` is a fine async alternative; not needed here.) |
| **PyInstaller** | 6.21.0 | Package to a single windowed `.exe` | For distribution. Use `--onefile --windowed --icon=app.ico`. Nuitka is an alternative if you want a faster/native build, but PyInstaller is lower-friction. |
### Development Tools
| Tool | Purpose | Notes |
|------|---------|-------|
| **venv + pip** | Dependency isolation | Standard. Pin versions in `requirements.txt`. |
| **Windows Registry `Run` key** | Autostart with Windows | Write `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` → value pointing at the `.exe`. See "Autostart" below — this is the recommended mechanism, not the Startup folder or Task Scheduler. |
| **PyInstaller `--windowed`** | No console window for a tray app | Critical: a tray-only background app must not spawn a console. |
## Installation
# Core
# Supporting
# Dev / packaging
## How the two load-bearing requirements are resolved
### 1. Drawing remaining-time text onto the tray icon (the quality gate)
# update loop:
### 2. Cloud (not LAN) MQTT auth — the real, current flow
- Global/EU/US: `https://api.bambulab.com`
- China: `https://api.bambulab.cn` (phone-number accounts; email/password login does not work — token-only)
- `POST /v1/user-service/user/login` with `{"account": email, "password": pw, "apiError": ""}`
- Response branches on `loginType`:
- MQTT **username** = decode the JWT `accessToken`, read the `username` claim (format `u_<digits>`).
- MQTT **password** = the `accessToken` itself.
- Broker: `us.mqtt.bambulab.com:8883` (global/EU/US) or `cn.mqtt.bambulab.com:8883` (China). TLS required.
- Subscribe `device/<printer_serial>/report`; publish `pushing.pushall` to `device/<printer_serial>/request` to force a full status push (needed for P1 series; X1 pushes full state already).
- Read **`mc_remaining_time`** (minutes left), **`mc_percent`** (progress %), **`gcode_state`** (RUNNING / IDLE / FINISH / FAILED → drives the idle/offline display).
## Alternatives Considered
| Recommended | Alternative | When to Use Alternative |
|-------------|-------------|-------------------------|
| **Python** | **C# / .NET 8 (WinForms `NotifyIcon` + `MQTTnet` 5.1.x)** | Choose C# if you want a small self-contained AOT `.exe` (~single-digit–~54 MB depending on trim/AOT), the lowest idle footprint, and best native Windows integration. Real cost: you must **re-implement the Bambu cloud auth flow yourself** (no mature C# cloud-auth reference as battle-tested as pybambu), and dynamic icon text requires GDI+ `Graphics.DrawString` → bitmap → `Icon.FromHandle`. Strong second choice; pick it if footprint/single-file polish matters more than reusing existing auth code. |
| **paho-mqtt** | **MQTTnet 5.1.0 (C#)** | Only if you go the C# route. MQTTnet is mature, actively maintained (Feb 2026 release), supports TLS — fully capable, just in the wrong language for code reuse here. |
| **pystray** | **WinForms `NotifyIcon`** | C#-only path; native and excellent, but ties you to the .NET tradeoff above. |
| **PyInstaller** | **Nuitka** | If PyInstaller's `.exe` is flagged by AV or you want faster startup / smaller size, Nuitka compiles to C — more setup, better runtime characteristics. |
## What NOT to Use
| Avoid | Why | Use Instead |
|-------|-----|-------------|
| **LAN/local MQTT mode** (`192.168.x.x:8883` + printer Access Code) and any "LAN-only" guide | Project is explicitly **cloud account** only; the printer is not assumed reachable on the LAN. LAN mode uses a different broker/host and the printer's local access code, not the cloud token. | Cloud broker `us.mqtt.bambulab.com:8883` with JWT-derived username + access token. |
| **`infi.systray`** | Windows-only, effectively unmaintained, and it takes an `.ico` **file path** — clumsy for redrawing dynamic text every minute (you'd write temp files). | `pystray`, which accepts in-memory `PIL.Image` objects and supports live `icon.icon` reassignment. |
| **`bambu-connect` and most "Bambu Connect"-derived tools** | These center on **LAN** control using the extracted Bambu Connect cert/key (post-Jan-2025 auth changes). Not the cloud-account flow this project needs. | Port the **cloud** auth from `pybambu` (`bambu_cloud.py`) or study `zhaobenny/bambulab-cloud-py`. |
| **Depending on the whole `pybambu` package as a library** | It's structured as a Home Assistant integration backend, not a clean standalone PyPI package; pulling it in wholesale adds HA-shaped baggage. | **Copy the auth + field-parsing logic** (it's reverse-engineered reference code) into a small local module. |
| **Electron / Node** | Hundreds of MB, heavy idle RAM/CPU — violates the lightweight always-on background constraint. Drawing text on a 16×16 tray icon is also awkward via Electron's Tray API. | Python or C#. |
| **Storing the token in a plain file / config / env var** | Token grants full account access; plaintext on disk is a credential-leak risk. | `keyring` → Windows Credential Manager (DPAPI-encrypted per user). |
| **Startup folder shortcut for autostart** | Works but is fragile (shortcut breakage, user can't see it managed) and less robust than the registry key. | `HKCU\...\Run` registry value (see below). |
| **Task Scheduler for autostart** | Overkill for a per-user tray app; adds admin/elevation complexity and a heavier mechanism than needed. | `HKCU\...\Run` registry value. |
## Autostart with Windows (recommended mechanism)
## Credential / Token Storage on Windows
- **Use `keyring`** → on Windows it writes to the **Windows Credential Manager** (Credential Locker), encrypted with **DPAPI** scoped to the current user. No extra dependency beyond `keyring`.
- Store **only the access token** (and printer serial). **Never persist the password.** Re-prompt for the email verification code when the token expires.
- This matches how `bambulab-cloud-py` behaves (it never persists passwords; tokens only).
## Stack Patterns by Variant
- Switch to **C# / .NET 8, WinForms `NotifyIcon`, `MQTTnet` 5.1.x**, GDI+ `DrawString` for icon text, `PublishSingleFile` (or Native AOT with `_SuppressWinFormsTrimError`).
- Because .NET gives the lowest idle memory and a self-contained native binary — at the cost of hand-porting the Bambu cloud auth flow.
- Use the **Python** stack above.
- Because you inherit a known-good cloud auth + MQTT field map from pybambu and ship in days, not weeks.
- `tray-icon` only takes raw RGBA (`Icon::from_rgba`) and has no built-in text rendering — you'd add `image`/`imageproc` + a font crate just to draw `1:23`, then hand-port the entire cloud auth flow with no Rust reference as mature as pybambu. Highest effort, least payoff for a small read-only monitor.
## Version Compatibility
| Package A | Compatible With | Notes |
|-----------|-----------------|-------|
| paho-mqtt 2.1.0 | Python 3.7+ | **Breaking from 1.x:** must pass `mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)`. Copying pybambu code written for paho 1.x → adjust callbacks. |
| pystray 0.19.5 | Pillow 12.x, Python 3.8+ | pystray needs Pillow for image creation; `icon.run()` blocks — run MQTT loop on `paho`'s own network thread (`loop_start()`) and update the icon from there. |
| Pillow 12.2.0 | Python 3.9+ | `ImageFont.truetype` needs a font file present; bundle `segoeui.ttf` or ship a font so the packaged `.exe` doesn't depend on a system path. |
| PyInstaller 6.21.0 | Python ≤ 3.13 | `--windowed` to suppress console; verify `keyring` and `pystray` backends are collected (add hidden-imports if AV/import errors appear). |
## Sources
- `coelacant1/Bambu-Lab-Cloud-API` — `API_AUTHENTICATION.md` & `API_MQTT.md` — login endpoints, 2FA/verifyCode flow, region hosts, MQTT broker/topics. MEDIUM (reverse-engineered, may drift).
- `greghesp/ha-bambulab` `pybambu/bambu_cloud.py` (live source) — confirmed login branch logic (`verifyCode`/`tfa`), JWT `username` claim → MQTT username, access token → MQTT password. HIGH for current behavior.
- `Doridian/OpenBambuAPI` `mqtt.md` — `device/<serial>/report` topic, `pushing.pushall`, `mc_remaining_time` / `mc_percent` / `gcode_state` fields; cloud & LAN share topic structure. HIGH.
- `zhaobenny/bambulab-cloud-py` — independent confirmation of cloud login + token-only storage, ~3-month token validity. MEDIUM.
- PyPI JSON API (queried 2026-06-20) — current versions: paho-mqtt 2.1.0, pystray 0.19.5, Pillow 12.2.0, keyring 25.7.0, PyInstaller 6.21.0. HIGH.
- nuget.org/packages/MQTTnet — MQTTnet 5.1.0 (Feb 2026), actively maintained, TLS-capable (alternative C# path). HIGH.
- pystray.readthedocs.io + Pillow docs — `icon.icon` live reassignment, `icon.title` tooltip, `ImageDraw.text`. HIGH.
- Microsoft Learn (WinForms `NotifyIcon`, .NET single-file/AOT publishing) — C# alternative footprint/packaging. HIGH.
- crates.io/docs.rs `tray-icon` — RGBA-only icon, no text rendering (basis for rejecting Rust). HIGH.
<!-- GSD:stack-end -->

<!-- GSD:conventions-start source:CONVENTIONS.md -->
## Conventions

Conventions not yet established. Will populate as patterns emerge during development.
<!-- GSD:conventions-end -->

<!-- GSD:architecture-start source:ARCHITECTURE.md -->
## Architecture

Architecture not yet mapped. Follow existing patterns found in the codebase.
<!-- GSD:architecture-end -->

<!-- GSD:skills-start source:skills/ -->
## Project Skills

No project skills found. Add skills to any of: `.claude/skills/`, `.agents/skills/`, `.cursor/skills/`, or `.github/skills/` with a `SKILL.md` index file.
<!-- GSD:skills-end -->

<!-- GSD:workflow-start source:GSD defaults -->
## GSD Workflow Enforcement

Before using Edit, Write, or other file-changing tools, start work through a GSD command so planning artifacts and execution context stay in sync.

Use these entry points:
- `/gsd-quick` for small fixes, doc updates, and ad-hoc tasks
- `/gsd-debug` for investigation and bug fixing
- `/gsd-execute-phase` for planned phase work

Do not make direct repo edits outside a GSD workflow unless the user explicitly asks to bypass it.
<!-- GSD:workflow-end -->



<!-- GSD:profile-start -->
## Developer Profile

> Profile not yet configured. Run `/gsd-profile-user` to generate your developer profile.
> This section is managed by `generate-claude-profile` -- do not edit manually.
<!-- GSD:profile-end -->
