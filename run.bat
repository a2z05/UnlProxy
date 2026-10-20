@echo off
cd /d "%~dp0"
where python >nul 2>nul || (echo Python 3 not found - install from python.org & pause & exit /b 1)
rem headless: bring the pool up (idempotent), sync 9router, then open the console
python warppool.py init
python warppool.py cli
pause
