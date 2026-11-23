@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
title WaveBox Studio 4 - lancement securise

where py >nul 2>&1
if %errorlevel%==0 (
    set "PY=py -3"
) else (
    set "PY=python"
)
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>&1
if errorlevel 1 (
    echo Python 3.11+ est necessaire. Source officielle : https://www.python.org/downloads/windows/
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] Creation de l'environnement Python isole...
    %PY% -m venv ".venv"
    if errorlevel 1 (
        echo Erreur de creation de .venv
        pause
        exit /b 1
    )
)
set "VENV_PY=%~dp0.venv\Scripts\python.exe"

"%VENV_PY%" -c "import importlib.metadata as m,sys; sys.exit(0 if m.version('yt-dlp')=='2026.10.26' and m.version('imageio-ffmpeg')=='0.8.1' else 1)" >nul 2>&1
if errorlevel 1 (
    echo [2/3] Installation des bibliotheques officielles, versions et SHA-256 controles...
    "%VENV_PY%" -m pip --isolated install --index-url https://pypi.org/simple --only-binary=:all: --no-deps --require-hashes -r "requirements.txt"
    if errorlevel 1 (
        echo ECHEC: telechargement ou controle SHA-256 des dependances.
        pause
        exit /b 1
    )
)

echo [3/3] Ouverture de WaveBox...
"%VENV_PY%" "app.py"
if errorlevel 1 (
    echo L'application s'est fermee avec une erreur. Verifiez Python et les bibliotheques.
    pause
)
endlocal
