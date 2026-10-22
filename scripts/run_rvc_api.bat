@echo off
cd /d D:\RVCSVC-API\RVCSVC-API
set "PATH=D:\RVC20240604Nvidia;%PATH%"
set "PYTHONPATH="
set "PYTHONHOME="
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
if not exist logs mkdir logs
venv\Scripts\pythonw.exe -u app_rvc.py > logs\api.log 2>&1
