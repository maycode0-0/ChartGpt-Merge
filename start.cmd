@echo off
setlocal
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 goto use_python
py -3 codex_chat_merge.py
if errorlevel 1 pause
exit /b
:use_python
where python >nul 2>nul
if errorlevel 1 goto missing
python codex_chat_merge.py
if errorlevel 1 pause
exit /b
:missing
echo Python 3.10 or newer is required. Install Python with Tcl/Tk support.
echo https://www.python.org/downloads/windows/
pause
