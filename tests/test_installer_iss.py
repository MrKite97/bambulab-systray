"""Static self-check for installer/bambulab-systray.iss.

No ISCC and no Windows desktop are needed: this greps the authored .iss for the exact,
locked contract strings (D-01..D-16) and asserts the autostart value name + the singleton
mutex name match the LIVE source modules (src.autostart, src.single_instance). If the
installer or the app drifts apart on either side, this test fails — which is exactly what
guards the "installer and app own the same HKCU\\Run value / wait on the same mutex"
invariant in CI, where the real install/uninstall cannot run.
"""

import pathlib

from src import autostart, single_instance

ISS_PATH = pathlib.Path(__file__).resolve().parents[1] / "installer" / "bambulab-systray.iss"


def _iss_text() -> str:
    return ISS_PATH.read_text(encoding="utf-8")


def test_iss_exists_and_nonempty():
    assert ISS_PATH.is_file()
    assert _iss_text().strip()


def test_per_user_no_uac():
    text = _iss_text()
    assert "PrivilegesRequired=lowest" in text
    assert r"DefaultDirName={localappdata}\Programs\Bambu Lab Systray" in text


def test_appmutex_matches_single_instance():
    # AppMutex must equal the live MUTEX_NAME exactly (D-12).
    assert single_instance.MUTEX_NAME == "Global\\BambuLabSystray_singleton"
    assert "AppMutex=Global\\BambuLabSystray_singleton" in _iss_text()


def test_autostart_registry_matches_app_value():
    text = _iss_text()
    assert autostart.VALUE_NAME == "BambuLabSystray"
    assert 'ValueName: "BambuLabSystray"' in text
    # Triple-quoted ValueData embeds the literal quotes around the exe path (D-08/D-10).
    assert 'ValueData: """{app}\\{#MyAppExeName}"""' in text
    assert "Tasks: autostart" in text
    assert "Flags: uninsdeletevalue" in text


def test_default_checked_dutch_autostart_task():
    assert 'Description: "Bambu Lab Systray met Windows opstarten"' in _iss_text()


def test_run_relaunch_no_skipifsilent():
    text = _iss_text()
    assert "Flags: nowait postinstall" in text
    assert "skipifsilent" not in text  # relaunch MUST fire during silent self-update (D-14)


def test_files_source_is_onefile_exe():
    # The `..\` prefix is required: the .iss lives in installer/, dist/ is one level up (D-06).
    assert "..\\dist\\bambulab-systray.exe" in _iss_text()


def test_version_single_sourced():
    text = _iss_text()
    assert "#ifndef MyAppVersion" in text
    assert '#define MyAppVersion "2.1.2"' in text


def test_no_login_state_or_hklm_reference():
    # Forbid the ROAMING login-state path + HKLM, NOT the local install root.
    # The install root is `{localappdata}\Programs\...` (D-05) — the substring "appdata"
    # is legitimately inside `{localappdata}`, so forbid the precise roaming references
    # `{userappdata}` and `%appdata%` instead of a bare "appdata" (which would falsely
    # collide with the required `{localappdata}` install root).
    lowered = _iss_text().lower()
    for forbidden in ("keyring", "hklm", "{userappdata}", "%appdata%", "settings.json", "region", "serial"):
        assert forbidden not in lowered, f"login-state/HKLM reference leaked: {forbidden}"
