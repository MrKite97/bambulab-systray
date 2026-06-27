; Bambu Lab Systray — per-user (no-UAC) Inno Setup installer.
; This .iss is BOTH the distribution artifact (DIST-03) and the Phase 14 self-update
; target. version.py is the single source of truth: CI passes the version via
; ISCC /DMyAppVersion=<x.y.z> (Phase 12); the #ifndef default below only makes a local
; `iscc bambulab-systray.iss` work and is NOT a second authoritative literal (D-02).

#ifndef MyAppVersion
  #define MyAppVersion "2.1.0"
#endif

#define MyAppName "Bambu Lab Systray"
#define MyAppPublisher "Maarten Vlieger"
#define MyAppExeName "bambulab-systray.exe"

[Setup]
; Stable AppId GUID — generate once, NEVER change (uninstall/upgrade keying depends on it).
AppId={{8B5F2E3A-9C41-4D7B-A2E6-1F0D7C4B9E55}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
; Per-user, no UAC: never elevates, never prompts (D-04). Non-negotiable.
PrivilegesRequired=lowest
; Conventional per-user install root, writable without admin (D-05).
DefaultDirName={localappdata}\Programs\Bambu Lab Systray
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; Silent self-update support: wait on the EXACT existing app mutex so the running app
; exits before files are replaced (D-12). CloseApplications/RestartApplications let the
; restart manager close+reopen the app under the silent flags (D-13).
AppMutex=Global\BambuLabSystray_singleton
CloseApplications=yes
RestartApplications=yes
; A DISTINCT setup-scoped mutex (NOT the app mutex) prevents two installers at once (D-16).
SetupMutex=BambuLabSystray_setup_singleton
; Output naming/identity (D-16).
OutputDir=Output
OutputBaseFilename=BambuLabSystray-Setup-{#MyAppVersion}
UninstallDisplayName=Bambu Lab Systray
UninstallDisplayIcon={app}\{#MyAppExeName}
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
; Target Windows 10/11 (Claude's discretion, D — keeps per-user mechanics valid).
MinVersion=10.0

[Languages]
Name: "dutch"; MessagesFile: "compiler:Languages\Dutch.isl"

[Tasks]
; Default-checked autostart task (D-09). Gates the [Registry] Run value below.
Name: "autostart"; Description: "Bambu Lab Systray met Windows opstarten"; GroupDescription: "Extra:"

[Files]
; The one-file exe self-contains the font + panel.html via PyInstaller datas — ship just
; this single file (D-06). Source is the spec's output dist/bambulab-systray.exe.
; NOTE: the `..\` prefix is REQUIRED — this .iss lives in installer/, one level below the
; repo root where dist/ sits, so the source path resolves up one directory. D-06 quotes
; the path as "dist\bambulab-systray.exe" relative to the repo root; from inside installer/
; that becomes "..\dist\bambulab-systray.exe". The `..\`-prefixed form is the canonical,
; functionally-correct literal — do NOT drop the `..\`.
Source: "..\dist\bambulab-systray.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
; Per-user Start-menu shortcut ({autoprograms} resolves per-user under lowest) (D-07).
Name: "{autoprograms}\Bambu Lab Systray"; Filename: "{app}\{#MyAppExeName}"

[Registry]
; The autostart value — byte-identical to what src/autostart.py writes: HKCU, value name
; "BambuLabSystray", data = the QUOTED exe path (inner triple-quotes embed the literal
; quotes). Same name + same format => the in-app toggle reads it as present and keeps
; ownership; exactly ONE value, no duplicate/stale entry (D-08/D-10/D-11).
; uninsdeletevalue removes it on uninstall (D-10/D-15).
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "BambuLabSystray"; ValueData: """{app}\{#MyAppExeName}"""; Tasks: autostart; Flags: uninsdeletevalue

[Run]
; Non-elevated relaunch after install (D-14). The relaunch MUST fire during a silent
; self-update too, so the silent-skip flag is deliberately OMITTED. Because Setup runs at
; `lowest`, [Run] already executes as the original non-elevated user.
Filename: "{app}\{#MyAppExeName}"; Description: "Bambu Lab Systray starten"; Flags: nowait postinstall
