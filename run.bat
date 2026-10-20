@echo off
cd /d "%~dp0"
where python >nul 2>nul || (echo Python 3 not found - install from python.org & pause & exit /b 1)
python warppool.py init
if errorlevel 1 pause & exit /b 1
python warppool.py up
python warppool.py serve
pause
