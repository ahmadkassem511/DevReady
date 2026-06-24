@echo off
REM ===========================================================================
REM DevReady one-time installer for Windows (the "easy app" path)
REM ---------------------------------------------------------------------------
REM This is the only time a non-technical user touches a terminal. It:
REM   1. installs `uv` if missing  - a single binary that needs NO existing
REM      Python (it downloads Python for us; no admin rights needed),
REM   2. installs DevReady (with the web GUI) as an isolated tool,
REM   3. creates a Desktop shortcut so future use is just a double-click,
REM   4. launches the browser GUI.
REM
REM Usage:  double-click install.bat  (or run it from a terminal)
REM ===========================================================================
setlocal
set "REPO=https://github.com/ahmadkassem511/DevReady"

echo DevReady installer
echo ==================

REM 1) Ensure uv ------------------------------------------------------------
where uv >nul 2>nul
if errorlevel 1 (
  echo -^> Installing uv ^(one-time, no admin needed^)...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
)
REM Make uv/devready visible in THIS session (uv installs to %USERPROFILE%\.local\bin).
set "PATH=%USERPROFILE%\.local\bin;%PATH%"

REM 2) Install DevReady with the GUI extra ----------------------------------
echo -^> Installing DevReady...
uv tool install --force "devready[ui] @ git+%REPO%"

REM 3) Create a Desktop shortcut so the terminal is never needed again ------
echo -^> Creating a Desktop shortcut...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ws = New-Object -ComObject WScript.Shell; $lnk = $ws.CreateShortcut((Join-Path $ws.SpecialFolders('Desktop') 'DevReady.lnk')); $lnk.TargetPath = (Join-Path $env:USERPROFILE '.local\bin\devready.exe'); $lnk.Arguments = 'ui'; $lnk.IconLocation = 'shell32.dll,13'; $lnk.Description = 'Set up and run any project with a click'; $lnk.Save()"

REM 4) Launch now -----------------------------------------------------------
echo -^> Starting DevReady - your browser will open shortly.
"%USERPROFILE%\.local\bin\devready.exe" ui

endlocal
