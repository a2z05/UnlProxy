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
rem Kept deliberately tiny and single-line-per-statement: this file has to
rem survive being downloaded from GitHub, which serves it with LF endings and
rem breaks cmd.exe on multi-line blocks and goto labels.
setlocal
cd /d "%~dp0"
if not exist "apply.mjs" goto nomjs
where node >nul 2>nul && goto havenode
if exist "%ProgramFiles%\nodejs\node.exe" goto nodeexe
echo. & echo   Node.js was not found. 9router runs on Node, so it should be installed. & echo   If it is, open a NEW terminal so PATH picks it up. & goto hold
:nodeexe
set "NODE=%ProgramFiles%\nodejs\node.exe"
goto run
:havenode
set "NODE=node"
:run
"%NODE%" apply.mjs %*
if errorlevel 1 goto failed
if "%~1"=="" pause
exit /b 0
:nomjs
echo. & echo   apply.mjs is missing. Download the whole 9router-proxy-pool-fix folder: & echo   https://github.com/a2z05/UnlProxy/tree/master/9router-proxy-pool-fix & goto hold
:failed
echo. & echo   Finished with errors. & goto hold
:hold
pause
exit /b 1
