@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
where py >nul 2>nul
if errorlevel 1 (
    python menu.py
) else (
    py -3 menu.py
)
if errorlevel 1 pause
