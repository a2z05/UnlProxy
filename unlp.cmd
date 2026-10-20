@echo off
rem UnlProxy launcher. `unlp` opens the control UI in the browser.
setlocal
set "DIR=%UNLPROXY_DIR%"
if "%DIR%"=="" set "DIR=%LOCALAPPDATA%\UnlProxy"
if not exist "%DIR%\warppool.py" (
  echo UnlProxy is not installed at %DIR% 1>&2
  exit /b 1
)
if "%~1"=="" ( set "CMD=ui" ) else ( set "CMD=%*" )
pushd "%DIR%"
python warppool.py %CMD%
popd
