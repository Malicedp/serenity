@echo off
setlocal enableextensions enabledelayedexpansion
title Serenity Gateway
chcp 65001 >nul
set PYTHONIOENCODING=utf-8

:: Always run from the directory this .bat lives in
cd /d "%~dp0"

echo.
echo  Starting Serenity...
echo.

:: --- Check Python is on PATH ---
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo  [ERROR] Python not found on PATH.
    echo.
    echo  Serenity requires Python 3.11 exactly.
    echo  Download: https://www.python.org/downloads/release/python-3119/
    echo  Make sure to tick "Add Python to PATH" during install.
    goto :fail
)

:: --- Check Python version is exactly 3.11 ---
python -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 11) else 1)" >nul 2>&1
if %errorlevel% neq 0 (
    echo  [ERROR] Wrong Python version.
    echo.
    for /f "tokens=*" %%v in ('python --version 2^>^&1') do echo  Found:    %%v
    echo  Required: Python 3.11
    echo.
    echo  Serenity's core is compiled for Python 3.11 and will not
    echo  run on any other version.
    echo.
    echo  Download Python 3.11: https://www.python.org/downloads/release/python-3119/
    echo  Install it, tick "Add Python to PATH", then run start.bat again.
    goto :fail
)

:: --- Kill any running Serenity process so pip can overwrite sera.exe ---
taskkill /F /IM sera.exe >nul 2>&1
taskkill /F /IM python.exe /FI "WINDOWTITLE eq Serenity Gateway" >nul 2>&1
timeout /t 2 /nobreak >nul

:: --- Install / update dependencies (only when pyproject.toml changed) ---
set MARKER=%~dp0.deps_installed
set PYPROJECT=%~dp0pyproject.toml
set NEEDS_INSTALL=0

if not exist "%MARKER%" set NEEDS_INSTALL=1
if exist "%MARKER%" (
    for %%A in ("%PYPROJECT%") do set PYPROJECT_TIME=%%~tA
    for %%B in ("%MARKER%") do set MARKER_TIME=%%~tB
    if "!PYPROJECT_TIME!" gtr "!MARKER_TIME!" set NEEDS_INSTALL=1
)

if "%NEEDS_INSTALL%"=="1" (
    echo  Installing dependencies...
    python -m pip install -e ".[senses,spotify,obs]" -q --no-warn-script-location
    if %errorlevel% neq 0 (
        echo  pip install failed - attempting repair...
        python -m pip install -e ".[senses,spotify,obs]" --force-reinstall -q --no-warn-script-location
        if %errorlevel% neq 0 (
            echo  [ERROR] Repair failed. Check your Python install.
            goto :fail
        )
    )
    type nul > "%MARKER%"
    echo  Dependencies OK.
) else (
    echo  Dependencies up to date.
)
echo.

:: --- GitNexus (optional - requires Node.js) ---
where npm >nul 2>&1
if %errorlevel% neq 0 (
    echo  [INFO] npm not found - GitNexus skipped.
    echo.
    goto :skip_gitnexus
)
where gitnexus >nul 2>&1
if %errorlevel% neq 0 (
    echo  Installing GitNexus...
    npm install -g gitnexus --silent >nul 2>&1
    if %errorlevel% neq 0 (
        echo  [WARNING] GitNexus install failed. Skipping.
    ) else (
        echo  GitNexus installed.
    )
) else (
    echo  GitNexus already installed.
)
echo.
if not exist "%~dp0.gitnexus\" (
    echo  Indexing codebase with GitNexus...
    gitnexus analyze "%~dp0" >nul 2>&1
    echo  GitNexus index ready.
    echo.
)

:skip_gitnexus

:: --- First-run: launch setup wizard if no config exists ---
if not exist "%USERPROFILE%\.serenity\config.json" (
    echo  No config found - launching setup wizard...
    echo.
    serenity
    goto :done
)

:: --- Launch the gateway ---
:: Try sera.exe first; fall back to running via Python directly
where sera >nul 2>&1
if %errorlevel% equ 0 (
    sera gateway
) else (
    echo  [INFO] sera not on PATH - launching via Python directly...
    python -m serenity.cli.commands gateway
)

:done
echo.
echo  Serenity has stopped.
pause
exit /b 0

:fail
echo.
pause
exit /b 1
