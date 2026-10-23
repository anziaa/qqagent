@echo off
chcp 65001 >nul
title QQ Music Login
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0qqmusic_login.ps1"
if "%~1"=="" pause
