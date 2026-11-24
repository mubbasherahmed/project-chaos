@echo off
rem Starts C.H.A.O.S. on Windows 10/11 with the Python 3.11 that has its packages installed.
cd /d "%~dp0"
echo Starting CHAOS...
echo.
where py >nul 2>nul
if %errorlevel%==0 (
    py -3.11 "%~dp0CHAOS.py"
) else (
    python "%~dp0CHAOS.py"
)
pause
