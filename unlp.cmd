@echo off
rem UnlProxy launcher. `unlp` opens the control UI in the browser.
rem The script is called by absolute path: a launcher that relies on `cd`
rem silently runs in the wrong directory when the change-directory fails.
setlocal
set "UNLP_DIR=%UNLPROXY_DIR%"
if "%UNLP_DIR%"=="" set "UNLP_DIR=%LOCALAPPDATA%\UnlProxy"
if not exist "%UNLP_DIR%\warppool.py" (
  echo UnlProxy is not installed at %UNLP_DIR% 1>&2
  echo Run:  irm https://raw.githubusercontent.com/a2z05/UnlProxy/master/install.ps1 ^| iex  1>&2
  exit /b 1
)
cd /d "%UNLP_DIR%" 2>nul
if "%~1"=="" (
  python "%UNLP_DIR%\warppool.py" ui
) else (
  python "%UNLP_DIR%\warppool.py" %*
)

