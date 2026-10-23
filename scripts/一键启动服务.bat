@echo off
chcp 65001 >nul
title 一键启动 AstrBot 服务
rem 双击这个就行；逐个拉起的逻辑在 launcher.py 里
rem 本机 python 路径写在 scripts\local.env（不入库，模板见 local.env.example）
set "PYTHONPATH="
set "PYTHONSTARTUP="
if exist "%~dp0local.env" for /f "usebackq tokens=1,* delims==" %%A in ("%~dp0local.env") do if not "%%B"=="" set "%%A=%%B"
if not defined ASTRBOT_PYTHON set "ASTRBOT_PYTHON=python"
"%ASTRBOT_PYTHON%" "%~dp0launcher.py"
echo.
pause
