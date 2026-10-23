@echo off
rem Windows: double-click this file to start mini-server.
setlocal
cd /d "%~dp0"
rem Prefer the py launcher: "python" may be the Microsoft Store placeholder.
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 mini_server.py %*
) else (
    where python >nul 2>nul
    if errorlevel 1 (
        echo Python 3 was not found. Install it from https://www.python.org/downloads/
    ) else (
        python mini_server.py %*
    )
)
echo.
echo mini-server has stopped.
pause
