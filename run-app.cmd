@echo off
REM Double-clickable launcher for the Bambu Lab systray app (dev / first-login).
REM
REM Why this exists: double-clicking run_app.py launches the SYSTEM Python, which
REM does NOT have the project's dependencies (pystray/paho/keyring/Pillow) -- those
REM live only in .venv. This launcher always uses the venv interpreter and keeps
REM the console open so you can type your Bambu email/password/verification code
REM and read any error before the window closes.

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [FOUT] .venv niet gevonden in "%~dp0".
    echo Maak hem aan met:
    echo     python -m venv .venv
    echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

echo Bambu Lab systray starten...
echo (Het tray-icoon verschijnt rechtsonder zodra je bent ingelogd. Rechtsklik -^> Afsluiten om te stoppen.)
echo.

".venv\Scripts\python.exe" -m src.app
set EXITCODE=%ERRORLEVEL%

echo.
echo (Afgesloten met code %EXITCODE%. Druk op een toets om dit venster te sluiten.)
pause >nul
exit /b %EXITCODE%
