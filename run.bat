@echo off
setlocal
cd /d "%~dp0"
py -3.11 tools\bootstrap_env.py --venv .venv
if errorlevel 1 goto error
.venv\Scripts\python.exe -m pip install -r requirements.txt -c constraints-build.txt
if errorlevel 1 goto error
.venv\Scripts\python.exe -m justpiano
if errorlevel 1 goto error
exit /b 0
:error
 echo Just Piano could not start. Install Python 3.11 x64 with Tcl/Tk and retry.
pause
exit /b 1
