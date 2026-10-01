@echo off
REM FinPulse Terminal CLI report launcher
REM Usage: run-cli.cmd [bars]   (default 250)
setlocal
set BARS=%1
if "%BARS%"=="" set BARS=250
wsl -e bash -lc "~/finpulse-gui-build/finpulse-cli --bars %BARS% --seed 42"
echo.
pause
