@echo off
cd /d "%~dp0"
if not exist .venv (echo Run build_windows.bat first. & pause & exit /b 1)
call .venv\Scripts\activate.bat
python main.py
if errorlevel 1 pause
