@echo off
chcp 65001 >nul
title AstrBot
rem 这个 bat 就放在 AstrBot 根目录，用 %~dp0 跟着走，别写死盘符
cd /d "%~dp0"
rem 清掉外部注入的 PYTHONPATH：某些工具的 sitecustomize 会劫持 Path.unlink，
rem 触发 AstrBot 处理图片后的临时文件清理，抛 SystemExit 直接杀掉进程。
set PYTHONPATH=
set PYTHONSTARTUP=
rem 把自带的 ffmpeg 放进 PATH：AstrBot 发送语音时会把音频转 wav，
rem 它调用的是裸命令 "ffmpeg"，系统 PATH 里没有就会报 "ffmpeg not found"，
rem 语音直接降级成文件发送。
set "PATH=%~dp0bin;%PATH%"
echo Starting AstrBot on http://localhost:6185 ...
rem AstrBot 本体装在独立环境里（体积大且要独立升级，不随仓库走）。
rem 路径不写死，依次试：ASTRBOT_EXE 环境变量 -> 同目录或仓库里的 local.env -> PATH 里的 astrbot
rem 端口用 AstrBot 默认的 6185。2026-10-23 深信服的安全服务临时占过它，
rem 真被占时先跑 scripts\check-ports.ps1 看占用者，别盲目重试
if not defined ASTRBOT_EXE (
    for %%F in ("%~dp0local.env" "%~dp0..\..\scripts\local.env") do (
        if exist "%%~F" (
            for /f "usebackq eol=# tokens=1,* delims==" %%A in ("%%~F") do (
                if /i "%%A"=="ASTRBOT_EXE" if not defined ASTRBOT_EXE set "ASTRBOT_EXE=%%~B"
            )
        )
    )
)
if defined ASTRBOT_EXE goto :run_exe
where astrbot >nul 2>nul
if errorlevel 1 (
    echo [X] 找不到 astrbot：在 scripts\local.env 里写一行 ASTRBOT_EXE=...，
    echo     或者设同名环境变量，或者把它放进 PATH。
    goto :done
)
astrbot run -p 6185
goto :done
:run_exe
"%ASTRBOT_EXE%" run -p 6185
:done
echo.
echo AstrBot stopped. Press any key to close.
pause >nul
