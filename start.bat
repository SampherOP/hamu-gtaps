@echo off
REM ===========================================================================
REM  HAMU-GPT WEB  -  start the website on this PC
REM
REM  Double-click this file. It starts the server, then opens your browser at
REM  the right address.
REM
REM  *** DO NOT open ui\index.html by double-clicking it. ***
REM  That loads the page straight off the disk with no server behind it, so
REM  every request fails and you get "Backend not connected". The page is only
REM  the front half of the app - this file is the other half.
REM
REM  On Vercel you do not need this file at all. Vercel runs wsgi.py for you,
REM  which is exactly what this file starts locally.
REM ===========================================================================
setlocal
cd /d "%~dp0"

set PORT=8000
if not "%~1"=="" set PORT=%~1

REM ---- find a Python -------------------------------------------------------
set PY=
py -c "import sys" >nul 2>nul
if not errorlevel 1 set PY=py
if not defined PY (
  python -c "import sys" >nul 2>nul
  if not errorlevel 1 set PY=python
)
if not defined PY if exist "C:\Python314\python.exe" set PY=C:\Python314\python.exe
if not defined PY if exist "C:\Python313\python.exe" set PY=C:\Python313\python.exe
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe

if not defined PY (
  echo.
  echo   Python was not found on this PC.
  echo.
  echo   Install it from https://www.python.org/downloads/
  echo   and TICK "Add python.exe to PATH" during setup.
  echo   Then run this file again.
  echo.
  pause
  exit /b 1
)

echo.
echo   ==========================================================
echo     HAMU-GPT WEB
echo   ==========================================================
echo     Address :  http://127.0.0.1:%PORT%
echo.
echo     The server opens in its own minimised window called
echo     "HAMU-GPT WEB server". Close that window to stop it.
echo.
echo     Do NOT open ui\index.html directly - it cannot work
echo     without this server running.
echo   ==========================================================
echo.

REM start the server in its own minimised window, then give it a moment to
REM bind the port before the browser asks for the page.
REM
REM NOTE: HAMU_HOSTED is deliberately NOT set here.
REM
REM Setting it would make this run claim to be a remote server, and the backend
REM would then refuse to read this machine's Chrome profiles - so the
REM "Continue with Google" button would disappear. Locally the backend IS your
REM own PC and the browser IS on the same machine, so one-tap Google sign-in is
REM both safe and useful, and _local_chrome_ok() allows exactly that.
REM
REM Vercel sets HAMU_HOSTED (via its own VERCEL env var), and there the button
REM correctly hides itself: a remote server has no local Chrome to read.
start "HAMU-GPT WEB server" /min %PY% wsgi.py
timeout /t 3 /nobreak >nul

start "" http://127.0.0.1:%PORT%

echo   Browser opened. This window can be closed.
timeout /t 5 /nobreak >nul
endlocal
