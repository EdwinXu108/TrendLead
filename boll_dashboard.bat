@echo off
rem One-click launcher for the weekly Bollinger dashboard (port 8766). Double-click this file.
rem The moving-average dashboard keeps its own launcher, dashboard.bat (port 8765).
setlocal
cd /d "%~dp0"
set PY=D:\Tools\miniconda3\python.exe
if not exist "%PY%" set PY=python
echo Starting the boll dashboard at http://127.0.0.1:8766/ ...
"%PY%" -X utf8 research\boll_v1\dashboard.py --open
if errorlevel 1 pause
