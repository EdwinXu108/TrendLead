@echo off
rem One-click launcher for the ma_lab dashboard. Double-click this file.
setlocal
cd /d "%~dp0"
set PY=D:\Tools\miniconda3\python.exe
if not exist "%PY%" set PY=python
echo Starting the ma_lab dashboard at http://127.0.0.1:8765/ ...
"%PY%" -X utf8 research\ma_lab_v1\dashboard.py --open
if errorlevel 1 pause
