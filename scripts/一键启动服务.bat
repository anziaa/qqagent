@echo off
chcp 65001 >nul
title 一键启动 AstrBot 服务
rem 双击这个就行；逐个拉起的逻辑在 launcher.py 里
set "PYTHONPATH="
set "PYTHONSTARTUP="
"D:\Anaconda_envs\envs\astrbot\python.exe" "%~dp0launcher.py"
echo.
pause
