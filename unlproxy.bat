@echo off
rem UnlProxy launcher - written by install.ps1. `unlproxy cli` / `unlproxy serve` / `unlproxy status`
setlocal
cd /d "%~dp0"
where python >nul 2>nul || (echo Python 3 not found - install from python.org & exit /b 1)
if "%~1"=="" ( python warppool.py cli ) else ( python warppool.py %* )
