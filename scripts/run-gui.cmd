@echo off
setlocal
REM ===========================================================================
REM FinPulse Terminal GUI launcher (the window is shown through WSLg)
REM Requires: WSL with ~/finpulse-gui-build already compiled
REM
REM -- Why this is longer than a one-line "wsl" call --------------------------
REM
REM WSLg has a known bug (microsoft/wslg#972). To hand a Linux window over to
REM the Windows desktop it must first allocate a shared-memory block under
REM /mnt/shared_memory. When that allocation fails, /mnt/wslg/weston.log shows
REM
REM   rdp_allocate_shared_memory: Failed to open "..." Input/output error
REM   RDP backend: use_gfxredir = 0
REM
REM and the whole session silently degrades to "copy mode": windows are still
REM created and get a [WARN:COPY MODE] title prefix, but their contents never
REM paint. That is exactly what "I double-clicked run-gui but see no UI" is.
REM
REM The state is re-rolled for every WSLg session, and this machine shuts its
REM WSL VM down whenever it goes idle -- so nearly every double-click starts a
REM fresh session, and fresh is close to a coin flip. Nothing inside FinPulse
REM can influence it; the remedy is to restart WSL and roll again, which is
REM what this script automates. Two checks, because each covers the other's
REM blind spot:
REM
REM   1. pre-flight  -- read weston.log before showing anything, so no broken
REM                     window flashes up.
REM   2. post-flight -- read the [WARN:COPY MODE] prefix off the window title,
REM                     in case the log looked fine but nothing painted.
REM
REM Kept ASCII-only on purpose: cmd.exe reads .cmd files as ANSI, so UTF-8
REM Chinese comments come out as mojibake. The Chinese write-up lives in
REM docs/manual.md -- see section 10.1 and the troubleshooting table.
REM ===========================================================================

set "BIN=~/finpulse-gui-build/src/gui/finpulse-gui"
set "MAX_TRY=3"
set /a TRY=1
set "SKIP_TITLE_CHECK=0"

:launch
set "LAUNCHED=0"
call :wslg_copy_mode
if not "%PAINT%"=="0" goto start_app

echo.
echo   [WARN] This WSLg session is in "copy mode": the GUI would open but
echo          never paint. Known WSLg bug microsoft/wslg#972, unrelated to
echo          FinPulse. (weston.log says: use_gfxredir = 0)
echo.
goto ask_restart

:start_app
set "LAUNCHED=1"
start "" wsl -e bash -lc "exec %BIN%"
if "%SKIP_TITLE_CHECK%"=="1" exit /b 0

call :wait_for_window
if %TIMEOUT%==1 exit /b 0

REM Belt and braces: msrdc.exe hosts WSLg windows on the Windows side, and a
REM COPY MODE prefix on its title means this session paints nothing.
tasklist /v /fi "IMAGENAME eq msrdc.exe" 2>nul | findstr /c:"COPY MODE" /c:"COPY-MODE" >nul
if errorlevel 1 exit /b 0

echo.
echo   [WARN] The window opened with a [WARN:COPY MODE] title: nothing will
echo          be painted. Known WSLg bug microsoft/wslg#972, unrelated to
echo          FinPulse.
echo.

:ask_restart
if %TRY% GEQ %MAX_TRY% goto give_up
echo          Restarting WSL usually clears it. That also stops every other
echo          WSL distro, Docker Desktop included.
echo.
set /a TRY+=1
choice /c YN /n /m "  Restart WSL and try again? Y/N: "
if not errorlevel 2 goto do_restart
REM N -- do not second-guess the user. If the window has not been opened yet,
REM open it anyway (they may want to look); if it is already up, just leave it.
if "%LAUNCHED%"=="1" exit /b 0
set "SKIP_TITLE_CHECK=1"
goto start_app

:do_restart
wsl --shutdown
call :sleep 4
goto launch

:give_up
echo          Tried %MAX_TRY% times without success; reboot Windows and retry.
echo.
pause
exit /b 1

REM ---------------------------------------------------------------------------
REM wslg_copy_mode: PAINT=0 when this WSLg session cannot paint.
REM
REM weston.log gets its "use_gfxredir" line while WSLg is still starting up,
REM before any application runs -- so waiting for it here both gives WSLg time
REM to come up and lets us answer the question before a window exists. If the
REM line never shows up we let it through (fail open) and rely on the title
REM check further down.
REM ---------------------------------------------------------------------------
:wslg_copy_mode
set PAINT=1
wsl -e bash -lc "for i in 1 2 3 4 5 6 7 8 9 10; do grep -q use_gfxredir /mnt/wslg/weston.log && break; sleep 1; done; grep -q 'use_gfxredir = 0' /mnt/wslg/weston.log"
if not errorlevel 1 set PAINT=0
exit /b 0

REM ---------------------------------------------------------------------------
REM wait_for_window: poll up to ~30s for the FinPulse window to appear. If it
REM never does, set TIMEOUT=1 and let the caller stay quiet rather than guess at
REM other causes -- a dead engine reports itself on the GUI's Log tab.
REM ---------------------------------------------------------------------------
:wait_for_window
set TIMEOUT=0
set /a WAITED=0
:wait_loop
call :sleep 2
set /a WAITED+=2
tasklist /v /fi "IMAGENAME eq msrdc.exe" 2>nul | findstr /c:"FinPulse Terminal" >nul
if not errorlevel 1 exit /b 0
if %WAITED% LSS 30 goto wait_loop
set TIMEOUT=1
exit /b 0

REM sleep <seconds>. ping is used instead of timeout because timeout refuses to
REM run when stdin is redirected.
:sleep
set /a SLEEP_N=%1+1
ping -n %SLEEP_N% 127.0.0.1 >nul
exit /b 0
