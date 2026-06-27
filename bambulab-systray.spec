# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Bambu Lab Systray (one-file, windowed).

Build with:  pyinstaller bambulab-systray.spec   ->  dist/bambulab-systray.exe

Why a checked-in spec (not an ad-hoc CLI):
  * datas bundles two resources so paths.resource_path(...) resolves them from
    sys._MEIPASS in the frozen exe exactly as it does from the project root in dev:
      - assets/DejaVuSans.ttf  : the icon font drawn onto the tray bitmap.
      - src/web/panel.html     : the flyout HTML the WebView2 backend loads; it
        lands at _MEIPASS/src/web/panel.html, exactly where
        paths.resource_path("src/web/panel.html") (via flyout._panel_file_url)
        looks. A wrong destination here = a blank flyout window.
  * hiddenimports declares the backends PyInstaller's static analysis routinely
    misses for this stack (all dynamically imported):
      - keyring.backends.Windows : keyring's Windows Credential Locker (DPAPI)
        backend, imported dynamically -> without it, token_store fails at runtime.
      - pystray._win32           : pystray's Windows tray backend, selected
        dynamically -> without it, the tray icon fails to initialize.
      - webview.platforms.winforms / webview.platforms.edgechromium / clr :
        pywebview's Windows (winforms) backend and its EdgeChromium/WebView2
        renderer, plus pythonnet (clr) which the winforms backend imports.
        pywebview selects its platform backend dynamically -> without these the
        frozen exe fails to render the flyout panel.
  * console=False == --windowed : a background tray app must spawn NO console.
  * one-file form: a.binaries + a.datas are passed straight into a single
    EXE(...) (no COLLECT), so the output is one self-contained
    bambulab-systray.exe.

App icon: no .ico is embedded (the tray icon is rendered at runtime by render.py
from the bundled font). The exe therefore uses PyInstaller's default icon; see
BUILD.md if a branded .ico is desired later.
"""

block_cipher = None

# Make the project root importable from the spec's working dir so the SINGLE
# version literal (src/version.py) is the source of the exe's file metadata too
# -- no second hand-edited copy lives in this spec (D-06).
import os
import sys

sys.path.insert(0, os.path.abspath("."))

from src.version import __version__, version_tuple
from PyInstaller.utils.win32.versioninfo import (
    VSVersionInfo, FixedFileInfo, StringFileInfo, StringTable,
    StringStruct, VarFileInfo, VarStruct,
)

_vt = version_tuple()  # (2, 1, 0, 0) -- 4-int tuple derived from __version__

# VSVersionInfo stamped onto the one-file exe so the Windows file-properties
# "Details" tab shows the version sourced from version.py (D-07). v2.1 ships
# UNSIGNED -- these strings are informational metadata, not a trust anchor.
version_info = VSVersionInfo(
    ffi=FixedFileInfo(
        filevers=_vt,
        prodvers=_vt,
        mask=0x3F,
        flags=0x0,
        OS=0x40004,
        fileType=0x1,
        subtype=0x0,
    ),
    kids=[
        StringFileInfo([
            StringTable(
                "040904B0",  # US English, Unicode
                [
                    StringStruct("ProductName", "Bambu Lab Systray"),
                    StringStruct("FileDescription", "Bambu Lab Systray — print monitor"),
                    StringStruct("ProductVersion", __version__),
                    StringStruct("FileVersion", __version__),
                    StringStruct("CompanyName", "Maarten Vlieger"),
                    StringStruct("OriginalFilename", "bambulab-systray.exe"),
                ],
            )
        ]),
        VarFileInfo([VarStruct("Translation", [0x0409, 0x04B0])]),
    ],
)


a = Analysis(
    ['run_app.py'],
    pathex=[],
    binaries=[],
    # Bundle the font at _MEIPASS/assets/DejaVuSans.ttf and the flyout HTML at
    # _MEIPASS/src/web/panel.html so resource_path resolves both when frozen.
    datas=[
        ('assets/DejaVuSans.ttf', 'assets'),
        ('src/web/panel.html', 'src/web'),
    ],
    # Backends PyInstaller misses by static analysis (dynamic imports).
    hiddenimports=[
        'keyring.backends.Windows',
        'pystray._win32',
        # pywebview Windows backend (dynamically selected -> PyInstaller misses it):
        'webview.platforms.winforms',     # the Windows backend
        'webview.platforms.edgechromium', # the EdgeChromium/WebView2 renderer
        'clr',                            # pythonnet, imported by the winforms backend
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
    version=version_info,  # stamp VSVersionInfo (from src/version.py) onto the exe
)
