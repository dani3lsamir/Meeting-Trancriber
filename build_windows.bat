@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (echo Install Python 3.11 first & pause & exit /b 1)
if not exist .venv py -3.11 -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
pyinstaller --noconfirm --clean --onedir --windowed --name MeetingTranscriber --collect-all faster_whisper --collect-all ctranslate2 --collect-all tokenizers --collect-all av --collect-all soundcard main.py
if errorlevel 1 (echo BUILD FAILED & pause & exit /b 1)
echo Built: dist\MeetingTranscriber\MeetingTranscriber.exe
pause
