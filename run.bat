@echo off
rem Start the app without a console window.
setlocal
cd /d "%~dp0"

where pythonw >nul 2>nul
if errorlevel 1 goto console

start "" pythonw "%~dp0app.py"
exit /b 0

:console
echo pythonw not found; starting with a console window.
python "%~dp0app.py"
if errorlevel 1 pause
