@echo off
rem 9router proxy-pool fix - Windows launcher for apply.mjs
rem
rem   Double-click it, or run from a terminal:
rem     fix-9router.bat              patch the installed 9router
rem     fix-9router.bat --check      is it patched?
rem     fix-9router.bat --revert     undo
rem
rem   Any argument apply.mjs takes works through here, e.g.
rem     fix-9router.bat --dir "C:\path\to\9router"
rem
rem Why a launcher and not the logic itself: the patch has to find an install,
rem match two different file shapes, and prove the result still parses. That is
rem JavaScript, and re-writing it in batch would be a second, drifting copy of
rem the same rules. This runs the one real implementation beside it.
setlocal EnableExtensions

rem --- go where this file lives (a double-click starts in the wrong place) ----
cd /d "%~dp0" || (
  echo Could not enter the folder this script is in.
  goto :hold
)

if not exist "apply.mjs" (
  echo.
  echo   apply.mjs is missing.
  echo.
  echo   It belongs next to this file - download the whole
  echo   9router-proxy-pool-fix folder, not just the .bat:
  echo.
  echo     https://github.com/a2z05/UnlProxy/tree/master/9router-proxy-pool-fix
  echo.
  goto :hold
)

rem --- node: 9router is a Node app, so it is installed wherever 9router runs --
set "NODE="
where node >nul 2>nul && set "NODE=node"
if not defined NODE if exist "%ProgramFiles%\nodejs\node.exe" set "NODE=%ProgramFiles%\nodejs\node.exe"
if not defined NODE if exist "%ProgramFiles(x86)%\nodejs\node.exe" set "NODE=%ProgramFiles(x86)%\nodejs\node.exe"
if not defined NODE if exist "%LOCALAPPDATA%\Programs\nodejs\node.exe" set "NODE=%LOCALAPPDATA%\Programs\nodejs\node.exe"

if not defined NODE (
  echo.
  echo   Node.js was not found.
  echo.
  echo   9router runs on Node, so it should already be installed. If 9router
  echo   runs, open a NEW terminal - a shell opened before installing Node
  echo   will not see it on PATH.
  echo.
  goto :hold
)

echo.
"%NODE%" apply.mjs %*
set "RC=%ERRORLEVEL%"
echo.

if not "%RC%"=="0" (
  echo   Finished with errors ^(exit %RC%^).
  goto :hold
)

rem --- only wait when double-clicked, so a script or terminal is not held ----
if "%~1"=="" pause
exit /b 0

:hold
echo   Press any key to close...
pause >nul
exit /b 1
