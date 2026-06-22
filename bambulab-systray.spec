# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Bambu Lab Systray (one-file, windowed).

Build with:  pyinstaller bambulab-systray.spec   ->  dist/bambulab-systray.exe

Why a checked-in spec (not an ad-hoc CLI):
  * datas bundles the icon font under "assets/" so paths.resource_path(
    "assets/DejaVuSans.ttf") resolves from sys._MEIPASS in the frozen exe
    exactly as it does from the project root in dev.
  * hiddenimports declares the two backends PyInstaller's static analysis
    routinely misses for this stack:
      - keyring.backends.Windows : keyring's Windows Credential Locker (DPAPI)
        backend, imported dynamically -> without it, token_store fails at runtime.
      - pystray._win32           : pystray's Windows tray backend, selected
        dynamically -> without it, the tray icon fails to initialize.
  * console=False == --windowed : a background tray app must spawn NO console.
  * one-file form: a.binaries + a.datas are passed straight into a single
    EXE(...) (no COLLECT), so the output is one self-contained
    bambulab-systray.exe.

App icon: no .ico is embedded (the tray icon is rendered at runtime by render.py
from the bundled font). The exe therefore uses PyInstaller's default icon; see
BUILD.md if a branded .ico is desired later.
"""

block_cipher = None


a = Analysis(
    ['run_app.py'],
    pathex=[],
    binaries=[],
    # Bundle the font at _MEIPASS/assets/DejaVuSans.ttf so resource_path resolves it.
    datas=[('assets/DejaVuSans.ttf', 'assets')],
    # Backends PyInstaller misses by static analysis (dynamic imports).
    hiddenimports=[
        'keyring.backends.Windows',
        'pystray._win32',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# One-file: pass binaries + datas into the single EXE (no COLLECT).
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='bambulab-systray',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,   # --windowed: no console window for a tray app
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
