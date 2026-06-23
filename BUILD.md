# Building Bambu Lab Systray

This produces a single, console-less, portable `bambulab-systray.exe` that a user
can drop in place and run. No installer, no admin rights, no auto-update.

## Prerequisites

- Windows 11, Python 3.12 (the `python` launcher on PATH).
- A virtual environment with the project + build dependencies installed:

  ```powershell
  python -m venv .venv
  .venv\Scripts\activate
  pip install -r requirements.txt
  ```

  `requirements.txt` includes `pyinstaller==6.21.0` (marked `# build/dev only`),
  so no separate install step is needed.

- **Microsoft Edge WebView2 (Evergreen) runtime** on the target machine. The
  flyout panel renders its HTML through the WebView2 (EdgeChromium) backend. On
  **Windows 11 it is present by default** (and on most up-to-date Windows 10).
  For older machines where it is missing, install the **Evergreen Bootstrapper**
  from <https://developer.microsoft.com/en-us/microsoft-edge/webview2/> (download
  the "Evergreen Bootstrapper"). **If WebView2 is missing the flyout window fails
  to render** (blank/no panel) even though the tray icon still appears.

## Build command

From the repo root, build from the checked-in spec (preferred — reproducible):

```powershell
pyinstaller bambulab-systray.spec
```

Output: **`dist\bambulab-systray.exe`** (a single self-contained file).
Intermediate files land in `build\`; both `dist\` and `build\` are git-ignored.

The spec is the source of truth. The equivalent ad-hoc one-liner (for reference
only — prefer the spec) is:

```powershell
pyinstaller --onefile --windowed `
  --add-data "assets/DejaVuSans.ttf;assets" `
  --add-data "src/web/panel.html;src/web" `
  --hidden-import keyring.backends.Windows `
  --hidden-import pystray._win32 `
  --hidden-import webview.platforms.winforms `
  --hidden-import webview.platforms.edgechromium `
  --hidden-import clr `
  run_app.py
```

What the spec guarantees:

- `--onefile` + `--windowed` (`console=False`): one exe, no console window.
- `datas=[('assets/DejaVuSans.ttf', 'assets')]`: the icon font is bundled at
  `_MEIPASS/assets/DejaVuSans.ttf`, where `paths.resource_path` resolves it.
- `datas=[..., ('src/web/panel.html', 'src/web')]`: the flyout HTML is bundled at
  `_MEIPASS/src/web/panel.html`, exactly where
  `paths.resource_path("src/web/panel.html")` resolves it when frozen. A wrong
  destination here would surface as a blank flyout window.
- `hiddenimports=['keyring.backends.Windows', 'pystray._win32']`: the two
  dynamically-imported backends PyInstaller's static analysis misses. Without
  these the frozen exe raises `ImportError` at runtime (token store / tray init).
- `hiddenimports=[..., 'webview.platforms.winforms', 'webview.platforms.edgechromium', 'clr']`:
  pywebview's Windows/EdgeChromium (WebView2) backend, which it imports
  dynamically at startup. Without these the frozen exe cannot render the flyout
  panel from the bundled `panel.html`.

No `.ico` is embedded — the tray icon is rendered at runtime from the bundled
font, so the exe uses PyInstaller's default file icon. Add an `icon=` to the spec
if a branded file icon is wanted later.

## Antivirus / Windows Defender note

PyInstaller one-file `.exe` files are **commonly flagged as false positives** by
Windows Defender and other AV engines. This is a well-known PyInstaller issue: the
one-file bootloader self-extracts to a temp dir at launch, which heuristic
scanners treat as suspicious. **It is not a real infection** — the bundle ships
only this project's code plus the font (`assets/DejaVuSans.ttf`); no token or
password is embedded (the token is fetched at runtime and stored in the Windows
Credential Manager / DPAPI on the user's own machine).

Mitigations, in order of preference:

1. **Code-sign the exe** with a code-signing certificate. This is the most
   effective fix but requires a certificate (the user's call — see "Out of
   scope"/deferred). A signed binary is trusted by Defender and SmartScreen.
2. **Fall back to a `--onedir` build** (a folder with the exe + its
   dependencies, produced via a `COLLECT` step). The onedir form is flagged far
   less often than onefile because there is no self-extracting bootloader. The
   trade-off is distributing a folder instead of a single file.
3. **Submit the binary to Microsoft as a false positive**
   (https://www.microsoft.com/en-us/wdsi/filesubmission) so future definition
   updates stop flagging it.

## Verify on a clean Windows 11 machine

Do this on a fresh machine or a fresh user profile (no `.venv`, no prior run):

1. Copy `dist\bambulab-systray.exe` to the clean machine and **double-click it**.
2. Confirm **no console window** appears and the **tray icon shows up**. The icon
   text/glyphs must be legible — this proves the bundled DejaVuSans font loaded
   from `_MEIPASS` (a missing font would crash the render).

   **LEFT-CLICK the tray icon**: the frameless flyout panel opens bottom-right
   and shows the **rendered HTML** — the login form (and, once logged in, the
   progress UI). It must **NOT** be a blank/white window: a blank window means
   `panel.html` or the pywebview WebView2 backend was not bundled (re-check the
   spec's `('src/web/panel.html', 'src/web')` datas tuple and the
   `webview.platforms.*` hidden imports, then rebuild). A blank panel with the
   tray icon present can also mean the **WebView2 runtime is missing** — install
   the Evergreen Bootstrapper (see Prerequisites).

   **First login now happens IN THE PANEL** (no console prompt this milestone):
   enter email/password, then the e-mail verification code, directly in the
   flyout. There is **no console-style login flow** — the windowless
   (`--windowed`) exe is fully usable because login moved into the panel.
   Completing login shows the printer list / live progress.
3. Right-click the icon -> **"Met Windows opstarten"** to enable autostart. Verify
   the `HKCU\Run` value now points at the exe:

   ```powershell
   reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v BambuLabSystray
   ```

   Toggle "Met Windows opstarten" off and re-run the query — the
   `BambuLabSystray` value must be **gone**.
4. Launch a **second copy** of `dist\bambulab-systray.exe`. It must exit
   immediately with **no second tray icon**; the first instance is undisturbed
   (single-instance mutex).
5. Right-click -> **"Afsluiten"**. The tray icon must disappear (**no orphaned
   icon**) and the process must exit — check Task Manager for no lingering
   `bambulab-systray.exe`, MQTT disconnected.
6. (Recommended) Reboot and confirm the app autostarts to the tray when "Met
   Windows opstarten" is enabled. Note any Windows Defender prompt and apply a
   mitigation above if the exe is flagged.

Record the build result, the `reg query` output, and any AV flag in
`.planning/phases/04-persistence-autostart-packaging/04-03-SUMMARY.md`.

## Out of scope

- **No installer / MSI** — the deliverable is a single portable `.exe`.
- **No auto-update** mechanism.
- **Code signing certificate procurement** is the user's call (documented above
  as the primary AV mitigation, but not performed by the build).
