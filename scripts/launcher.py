"""一键拉起 AstrBot 全套常驻服务（Windows）。

五个服务，启动顺序有依赖：
  1. NapCat      6099  QQ 协议端，AstrBot 靠它收发消息
  2. RVC WebUI   7897  换音色推理（唱歌链路调它；说话不再经过它）
  3. CosyVoice2  7870  情感语音合成，说话链路的上游
  4. RVC API     7860  翻唱接口，插件调 /convert
  5. AstrBot     6185  机器人本体 + WebUI

已经在监听的服务会跳过，不会开第二份。
每个服务独占一个黑窗口，关掉窗口就等于停掉那个服务。

为什么 CosyVoice2 必须常驻：模型 4.8 GB，加载要 18~45 秒（看磁盘缓存冷热），
每条语音都重载的话用户等不起。进程活着时只在启动时加载一次，
之后每条语音只花推理那 3~6 秒。
"""

import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field

NAPCAT_DIR = r"D:\Tools\NapCat.Shell"
RVC_DIR = r"D:\RVC20240604Nvidia"
API_DIR = r"D:\RVCSVC-API\RVCSVC-API"
ASTRBOT_DIR = r"D:\AstrBot"
ASTRBOT_EXE = r"D:\Anaconda_envs\envs\astrbot\Scripts\astrbot.exe"
# CosyVoice2 跑在自己的 venv 里（不用 conda：那个 cosyvoice 环境被清理机制打坏过）
COSYVOICE_DIR = r"D:\WorkBuddy\QQAgent\cosyvoice2"
COSYVOICE_PY = os.path.join(COSYVOICE_DIR, r"venv310\Scripts\python.exe")

# 每开一个窗口就置这个标志，让子进程自己带一个控制台
NEW_CONSOLE = 0x00000010


@dataclass
class Service:
    name: str
    port: int
    cwd: str
    argv: list
    desc: str
    ready_timeout: int = 240
    clean_python_env: bool = False
    extra_path: str = ""
    needs: tuple = ()          # 依赖的外部命令，启动前先确认能看到
    args: list = field(default_factory=list)


def preflight(svc: Service) -> list:
    """启动前确认它要用的外部命令在它的环境里能找到。

    7860 那次事故就是：端口起来了、/config 也正常，但 PATH 里没有 ffmpeg，
    一直到真去转换时才在 pydub 里炸。这种错光看端口发现不了。
    """
    env = build_env(svc)
    path = env.get("PATH", "")
    return [tool for tool in svc.needs if not shutil.which(tool, path=path)]


def port_open(port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def build_env(svc: Service) -> dict:
    env = os.environ.copy()
    if svc.clean_python_env:
        # 外部工具注入的 sitecustomize 会把 os.remove 换成"移到回收站"，
        # 无桌面会话时抛 OSError 0x2，直接炸掉临时文件清理，必须清掉
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONSTARTUP", None)
    if svc.extra_path:
        env["PATH"] = svc.extra_path + os.pathsep + env.get("PATH", "")
    return env


def spawn(svc: Service) -> bool:
    try:
        subprocess.Popen(
            svc.argv + svc.args,
            cwd=svc.cwd,
            env=build_env(svc),
            creationflags=NEW_CONSOLE,
            close_fds=True,
        )
        return True
    except Exception as e:
        print(f"  [失败] {svc.name} 拉不起来: {type(e).__name__}: {e}")
        return False


def wait_ready(svc: Service) -> bool:
    deadline = time.time() + svc.ready_timeout
    dots = 0
    while time.time() < deadline:
        if port_open(svc.port):
            return True
        time.sleep(2)
        dots += 1
        if dots % 5 == 0:
            print(f"         …还在等 {svc.name}（已等 {dots * 2} 秒）")
    return False


def pad(text: str, width: int) -> str:
    """按显示宽度补空格：中文占两格，直接用 ljust 会错位。"""
    shown = sum(2 if ord(ch) > 127 else 1 for ch in text)
    return text + " " * max(0, width - shown)


def build_services() -> list:
    return [
        Service(
            name="NapCat", port=6099, cwd=NAPCAT_DIR,
            argv=["cmd", "/c", "launcher-user.bat"],
            desc="QQ 协议端", ready_timeout=180,
        ),
        Service(
            name="RVC WebUI", port=7897, cwd=RVC_DIR,
            argv=[os.path.join(RVC_DIR, r"runtime\python.exe"), "-u", "infer-web.py",
                  "--pycmd", os.path.join(RVC_DIR, r"runtime\python.exe"), "--port", "7897"],
            desc="换音色推理", ready_timeout=300,
            extra_path=RVC_DIR, needs=("ffmpeg", "ffprobe"),
        ),
        Service(
            name="CosyVoice2", port=7870, cwd=COSYVOICE_DIR,
            argv=[COSYVOICE_PY, "-u", "server.py", "--port", "7870"],
            desc="情感语音合成", ready_timeout=120,
            # 这台机器上 PYTHONPATH 被指到 WorkBuddy 的 shim，sitecustomize
            # 会劫持 Path.unlink，装包/清临时文件都会失败回滚，必须清掉
            clean_python_env=True,
        ),
        Service(
            name="RVC 转换 API", port=7860, cwd=API_DIR,
            argv=[os.path.join(API_DIR, r"venv\Scripts\python.exe"), "-u", "app_rvc.py"],
            desc="翻唱接口", ready_timeout=300, clean_python_env=True, needs=("ffmpeg", "ffprobe"),
            # app_rvc 用 pydub 处理音频，内部是裸调 "ffmpeg"/"ffprobe"，
            # 不把 RVC 目录塞进 PATH 就会 FileNotFoundError，/convert 直接 500
            extra_path=RVC_DIR,
        ),
        Service(
            name="AstrBot", port=6185, cwd=ASTRBOT_DIR,
            argv=[ASTRBOT_EXE, "run", "-p", "6185"],
            desc="机器人本体", ready_timeout=150,
            clean_python_env=True, extra_path=os.path.join(ASTRBOT_DIR, "bin"),
            needs=("ffmpeg",),
        ),
    ]


def main() -> int:
    services = build_services()

    print("=" * 46)
    print("  AstrBot 全套服务")
    print("=" * 46)

    failed = []
    for svc in services:
        if port_open(svc.port):
            print(f"[跳过] {svc.name}（{svc.port}）已经在跑")
            continue

        missing = preflight(svc)
        if missing:
            print(f"[失败] {svc.name} 的环境里找不到 {'、'.join(missing)}，"
                  f"起来了也会在转换时炸，先不启动")
            print(f"        检查 {svc.extra_path} 下这几个 exe 还在不在")
            failed.append(svc)
            continue

        print(f"[启动] {svc.name}（{svc.port}）{svc.desc} …")
        if not spawn(svc):
            failed.append(svc)
            continue
        if wait_ready(svc):
            print(f"[就绪] {svc.name} 已监听 {svc.port}")
        else:
            print(f"[超时] {svc.name} 等了 {svc.ready_timeout} 秒还没起来，"
                  f"去看它自己的窗口里报了什么错")
            failed.append(svc)

    print()
    print("=" * 46)
    print("  当前状态")
    print("=" * 46)
    for svc in services:
        mark = "OK " if port_open(svc.port) else " X "
        print(f"  [{mark}] {pad(svc.name, 14)} {pad(str(svc.port), 6)} {svc.desc}")
    print()
    print("  WebUI   http://localhost:6185")
    print("  语音    说话走 7870（CosyVoice2），唱歌走 7860（RVC）")
    print("  说明    关掉这个窗口不会停服务；停服务就关掉对应服务的黑窗口")
    print()

    if failed:
        print("  !! 有服务没起来：" + "、".join(s.name for s in failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
