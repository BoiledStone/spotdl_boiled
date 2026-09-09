@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "PYTHONW="
for /f "delims=" %%P in ('where pythonw.exe 2^>nul') do if not defined PYTHONW set "PYTHONW=%%P"

if not defined PYTHONW (
    echo Pythonw.exe introuvable. Repare Python ou ajoute-le au PATH.
    pause
    exit /b 2
)

if not exist "%SCRIPT_DIR%spotdl_gui.py" (
    echo Interface introuvable: "%SCRIPT_DIR%spotdl_gui.py"
    pause
    exit /b 2
)

start "SpotDL Resolver" "%PYTHONW%" "%SCRIPT_DIR%spotdl_gui.py"
exit /b 0
