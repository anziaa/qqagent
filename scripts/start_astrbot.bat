@echo off
chcp 65001 >nul
title AstrBot
cd /d D:\AstrBot
rem 清掉外部注入的 PYTHONPATH：某些工具的 sitecustomize 会劫持 Path.unlink，
rem 触发 AstrBot 处理图片后的临时文件清理，抛 SystemExit 直接杀掉进程。
set PYTHONPATH=
set PYTHONSTARTUP=
rem 把自带的 ffmpeg 放进 PATH：AstrBot 发送语音时会把音频转 wav，
rem 它调用的是裸命令 "ffmpeg"，系统 PATH 里没有就会报 "ffmpeg not found"，
rem 语音直接降级成文件发送。
set "PATH=D:\AstrBot\bin;%PATH%"
echo Starting AstrBot on http://localhost:6185 ...
D:\Anaconda_envs\envs\astrbot\Scripts\astrbot.exe run -p 6185
echo.
echo AstrBot stopped. Press any key to close.
pause >nul
