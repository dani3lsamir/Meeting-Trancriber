@echo off
cd /d "%~dp0"
if not exist .venv (echo Please run build_windows.bat first & pause & exit /b 1)
call .venv\Scripts\activate.bat
python main.py
pause
