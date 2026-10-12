@echo off
rem Builds dist\MeetingTranscriber\MeetingTranscriber.exe
rem Usage: build_windows.bat [models...]     default models: base small  (use "none" to skip)
setlocal
cd /d "%~dp0"
set MODELS=%*
if "%MODELS%"=="" set MODELS=base small

where py >nul 2>nul
if errorlevel 1 (echo Install 64-bit Python 3.11 or 3.12 from python.org first. & pause & exit /b 1)
if not exist .venv (
  py -3.11 -m venv .venv 2>nul || py -3.12 -m venv .venv
  if errorlevel 1 (echo Could not create the virtual environment. & pause & exit /b 1)
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip || goto :fail
python -m pip install -r requirements-dev.txt || goto :fail

echo === Static checks and tests ===
python -m ruff check . || goto :fail
python -m pytest -q || goto :fail

echo === PyInstaller ===
pyinstaller --noconfirm --clean MeetingTranscriber.spec || goto :fail

if /i not "%MODELS%"=="none" (
  echo === Bundling models: %MODELS% ===
  python tools\fetch_models.py %MODELS% --dest dist\MeetingTranscriber\models || goto :fail
)

echo === Smoke test of the built exe ===
start /wait "" dist\MeetingTranscriber\MeetingTranscriber.exe --self-test --report selftest-report.json
if errorlevel 1 (echo Self-test reported problems, see selftest-report.json) else (echo Self-test passed.)

echo.
echo Built: %CD%\dist\MeetingTranscriber\MeetingTranscriber.exe
echo Distribute the whole dist\MeetingTranscriber folder.
pause
exit /b 0

:fail
echo BUILD FAILED
pause
exit /b 1
