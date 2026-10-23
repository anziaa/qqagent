@echo off
if defined RVCSVC_HOME (cd /d "%RVCSVC_HOME%") else (cd /d "%~d0\RVCSVC-API\RVCSVC-API")
if defined RVC_HOME (set "PATH=%RVC_HOME%;%PATH%") else (set "PATH=%~d0\RVC20240604Nvidia;%PATH%")
set "PYTHONPATH="
set "PYTHONHOME="
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
if not exist logs mkdir logs
venv\Scripts\pythonw.exe -u app_rvc.py > logs\api.log 2>&1
