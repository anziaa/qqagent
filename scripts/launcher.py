"""一键拉起 AstrBot 全套常驻服务（Windows）。

五个服务，启动顺序有依赖：
  1. AstrBot     6185  机器人本体 + WebUI，同时开 6199 等 NapCat 连进来
  2. NapCat      6099  QQ 协议端，一起来就要连 6199，所以必须排 AstrBot 后面
  3. CosyVoice2  7870  情感语音合成，说话链路的上游
  4. RVC WebUI   7897  换音色推理（唱歌链路调它；说话不再经过它）
  5. RVC API     7860  翻唱接口，插件调 /convert，模型目录来自 7897

为什么 AstrBot 必须排第一：机器人本体不在，6199 就没人监听，NapCat 一起来
就撞 ECONNREFUSED，每 5 秒重连一次、窗口一直刷；把它俩排在最前，其余三个
GPU 服务慢慢加载的时间就不占用机器人上线的时间。

为什么 WebUI 用 6185：AstrBot 自己的默认端口，浏览器开 http://localhost:6185。
2026-10-23 撞过一次：深信服的两个安全防护服务（SangforPWEx / SangforSP，设成
自动启动，关掉 VPN 客户端并不会停）临时占住了 127.0.0.1:6185，AstrBot 一 bind
就 WinError 10013/10048 直接崩。现在派进程前会先自己 bind 一次，被占就报出
占用者 PID，不再拉起一个必死的进程。要换端口就设 ASTRBOT_WEBUI_PORT。

已经在监听的服务会跳过，不会开第二份。
每个服务独占一个黑窗口，关掉窗口就等于停掉那个服务。

为什么 CosyVoice2 必须常驻：模型 4.8 GB，加载要 18~45 秒（看磁盘缓存冷热），
每条语音都重载的话用户等不起。进程活着时只在启动时加载一次，
之后每条语音只花推理那 3~6 秒。
"""

import os
import re
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field


def _runtime_root() -> str:
    """定位 runtime/ 目录。

    本文件有两个落脚点，所以不能写死路径：
      - 仓库里      <repo>/scripts/launcher.py
      - 部署后      <repo>/runtime/astrbot/launcher.py
    两个位置往上找一层或两层，谁底下有 napcat/ 就是 runtime。
    要放到别处去跑，设 QQAGENT_RUNTIME 直接指定。
    """
    env = os.environ.get("QQAGENT_RUNTIME")
    if env:
        return os.path.abspath(env)
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, os.pardir),
                 os.path.join(here, os.pardir, os.pardir)):
        cand = os.path.abspath(cand)
        if os.path.isdir(os.path.join(cand, "napcat")):
            return cand
    return os.path.abspath(os.path.join(here, os.pardir, "runtime"))


RUNTIME = _runtime_root()
# runtime/ 的上一级就是仓库根（cosyvoice2 直接躺在仓库根下）
REPO_ROOT = os.path.dirname(RUNTIME) if os.path.basename(RUNTIME) == "runtime" else RUNTIME

# 仓库内自带的运行时组件（NapCat / AstrBot 随仓库走，但不入库）
NAPCAT_DIR = os.environ.get("NAPCAT_HOME") or os.path.join(RUNTIME, "napcat")
ASTRBOT_DIR = os.environ.get("ASTRBOT_HOME") or os.path.join(RUNTIME, "astrbot")

# 仓库外的两个重家伙（RVC 模型体积太大，单独放）
RVC_DIR = os.environ.get("RVC_HOME") or r"D:\RVC20240604Nvidia"
API_DIR = os.environ.get("RVCSVC_HOME") or r"D:\RVCSVC-API\RVCSVC-API"


def _astrbot_exe() -> str:
    """定位 astrbot 可执行文件。

    优先 ASTRBOT_EXE，其次按「跑本脚本的这个 python」推同目录的 astrbot.exe
    （conda 和 venv 都装在 bin/ 或 Scripts/ 下），最后退到 PATH。
    这样同一个 launcher 换台机器就能跑，不用改文件。
    """
    env = os.environ.get("ASTRBOT_EXE")
    if env:
        return env
    bindir = os.path.dirname(sys.executable)
    for cand in (os.path.join(bindir, "astrbot.exe"),
                 os.path.join(bindir, "Scripts", "astrbot.exe")):
        if os.path.isfile(cand):
            return cand
    return shutil.which("astrbot") or os.path.join(bindir, "astrbot.exe")


ASTRBOT_EXE = _astrbot_exe()
# WebUI 端口。默认用 AstrBot 的 6185；被坏邻居占了就用 ASTRBOT_WEBUI_PORT 覆盖
WEBUI_PORT = int(os.environ.get("ASTRBOT_WEBUI_PORT") or 6185)
# CosyVoice2 跑在自己的 venv 里（不用 conda：那个 cosyvoice 环境被清理机制打坏过）
COSYVOICE_DIR = os.environ.get("COSYVOICE_HOME") or os.path.join(REPO_ROOT, "cosyvoice2")
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
    extra_ports: tuple = ()    # 主端口之外还要等通的端口（AstrBot 的 6199）
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


def port_bindable(port: int) -> bool:
    """真的绑一下试试。

    connect 探测只回答"有没有人在监听"，回答不了"能不能绑"。深信服那类客户端
    会把端口 bind 住却不 listen：connect 失败（看着像空闲）、bind 失败
    （WinError 10013/10048），只看 connect 就会派个新进程出去送死，窗口一闪
    就没。所以派进程之前先自己绑一次，绑不上就当场说清楚。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def port_owner_hint(port: int) -> str:
    """从 netstat 里翻出占用这个端口的 PID，给一条能直接抄的命令。"""
    try:
        out = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True,
            timeout=10, encoding="mbcs", errors="replace",
        ).stdout
    except Exception:
        return f"管理员 PowerShell 里跑  netstat -ano | findstr :{port}  看是谁占着"

    pids = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1].endswith(f":{port}"):
            pids.add(parts[-1])
    if not pids:
        return f"管理员 PowerShell 里跑  netstat -ano | findstr :{port}  看是谁占着"
    return ("占用者 PID：" + "、".join(sorted(pids))
            + "（管理员 PowerShell：taskkill /F /PID <PID>；"
              "深信服/杀毒这类常驻服务要在管理员权限下停掉，或换端口）")


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


def spawn(svc: Service):
    """拉起服务，成功返回 Popen，失败返回 None。

    句柄留着不为等它结束（拉起就撒手，关窗口不影响服务），只为发现"起来就
    退出"：AstrBot 撞端口时就是窗口刷一屏 traceback 然后秒退，没有句柄的话
    启动器只能干等 150 秒。
    """
    try:
        return subprocess.Popen(
            svc.argv + svc.args,
            cwd=svc.cwd,
            env=build_env(svc),
            creationflags=NEW_CONSOLE,
            close_fds=True,
        )
    except Exception as e:
        print(f"  [失败] {svc.name} 拉不起来: {type(e).__name__}: {e}")
        return None


def wait_ready(svc: Service, proc=None) -> tuple:
    """等到主端口和所有附加端口都监听。

    返回 (是否就绪, 还差哪些端口, 退出码)。第三个不是 None 说明进程在等待
    期间自己退出了，退出码直接给出来，省掉干等的 150 秒。
    附加端口是给"端口通了不等于链路通"留的：AstrBot 的 6185 是 WebUI，
    6199 才是 NapCat 的反向 WS 入口，两个都得在。
    """
    ports = (svc.port, *svc.extra_ports)
    deadline = time.time() + svc.ready_timeout
    waited = 0
    while True:
        missing = tuple(p for p in ports if not port_open(p))
        if not missing:
            return True, (), None
        if proc is not None and proc.poll() is not None:
            return False, missing, proc.returncode
        if time.time() >= deadline:
            return False, missing, None
        time.sleep(2)
        waited += 2
        if waited % 10 == 0:
            print(f"         …还在等 {svc.name}（已等 {waited} 秒，"
                  f"没监听：{'、'.join(map(str, missing))}）")


def pad(text: str, width: int) -> str:
    """按显示宽度补空格：中文占两格，直接用 ljust 会错位。"""
    shown = sum(2 if ord(ch) > 127 else 1 for ch in text)
    return text + " " * max(0, width - shown)


def detect_qq() -> str:
    """认出要登录的 QQ 号，让 NapCat 免扫码直接进。

    NapCat 把账号相关配置存成 onebot11_<QQ>.json，所以号不用另外维护：
    换号、加号这里自动跟上。要显式指定就设 QQAGENT_QQ=1234567。
    """
    env = os.environ.get("QQAGENT_QQ")
    if env:
        return env.strip()

    try:
        names = os.listdir(os.path.join(NAPCAT_DIR, "config"))
    except OSError:
        return ""
    for name in sorted(names):
        m = re.fullmatch(r"onebot11_(\d+)\.json", name)
        if m:
            return m.group(1)
    return ""


def build_services() -> list:
    qq = detect_qq()
    if os.environ.get("QQAGENT_NO_QUICK"):
        qq = ""       # 首次登录 / 想换号扫码进来时设这个

    return [
        # 机器人本体排第一：它不起，6199 就没人监听，NapCat 连不上只能干等
        Service(
            name="AstrBot", port=WEBUI_PORT, cwd=ASTRBOT_DIR,
            argv=[ASTRBOT_EXE, "run", "-p", str(WEBUI_PORT)],
            desc="机器人本体", ready_timeout=150,
            clean_python_env=True, extra_path=os.path.join(ASTRBOT_DIR, "bin"),
            needs=("ffmpeg",),
            # 6199 是 aiocqhttp 的反向 WS 入口，NapCat 连的就是它；只等 WebUI
            # 端口就放 NapCat 出去，它到 6199 只能撞 ECONNREFUSED（起不来的假象）
            extra_ports=(6199,),
        ),
        Service(
            name="NapCat", port=6099, cwd=NAPCAT_DIR,
            argv=["cmd", "/c", "launcher-user.bat"],
            # bat 把 %* 原样转给 NapCatWinBootMain.exe，-q 就是免扫码登录
            args=["-q", qq] if qq else [],
            desc="QQ 协议端", ready_timeout=180,
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
            name="RVC WebUI", port=7897, cwd=RVC_DIR,
            argv=[os.path.join(RVC_DIR, r"runtime\python.exe"), "-u", "infer-web.py",
                  "--pycmd", os.path.join(RVC_DIR, r"runtime\python.exe"), "--port", "7897"],
            desc="换音色推理", ready_timeout=300,
            extra_path=RVC_DIR, needs=("ffmpeg", "ffprobe"),
        ),
        Service(
            name="RVC 转换 API", port=7860, cwd=API_DIR,
            argv=[os.path.join(API_DIR, r"venv\Scripts\python.exe"), "-u", "app_rvc.py"],
            desc="翻唱接口", ready_timeout=300, clean_python_env=True, needs=("ffmpeg", "ffprobe"),
            # app_rvc 用 pydub 处理音频，内部是裸调 "ffmpeg"/"ffprobe"，
            # 不把 RVC 目录塞进 PATH 就会 FileNotFoundError，/convert 直接 500
            extra_path=RVC_DIR,
        ),
    ]


def port_state(svc: Service) -> str:
    """在跑 / 被占 / 空闲。三者不能混：被占时拉进程必死。"""
    if port_open(svc.port):
        return "running"
    if not port_bindable(svc.port):
        return "squatted"
    return "free"


def main() -> int:
    services = build_services()
    check_only = "--check" in sys.argv[1:]

    print("=" * 46)
    print("  AstrBot 全套服务" + ("（只检查，不启动）" if check_only else ""))
    print("=" * 46)

    failed = []
    for svc in services:
        state = port_state(svc)

        if state == "running":
            print(f"[跳过] {svc.name}（{svc.port}）已经在跑")
            continue

        if state == "squatted":
            print(f"[占用] {svc.name}（{svc.port}）没在监听，端口却绑不上")
            print("        有程序把端口 bind 住却不 listen，硬拉只会拿到 "
                  "WinError 10013/10048，窗口一闪就没")
            print(f"        {port_owner_hint(svc.port)}")
            if not check_only:
                failed.append(svc)
            continue

        if check_only:
            print(f"[空闲] {svc.name}（{svc.port}）{svc.desc}")
            continue

        lack = preflight(svc)
        if lack:
            print(f"[失败] {svc.name} 的环境里找不到 {'、'.join(lack)}，"
                  f"起来了也会在转换时炸，先不启动")
            print(f"        检查 {svc.extra_path} 下这几个 exe 还在不在")
            failed.append(svc)
            continue

        print(f"[启动] {svc.name}（{svc.port}）{svc.desc} …")
        proc = spawn(svc)
        if proc is None:
            failed.append(svc)
            continue

        ok, not_up, code = wait_ready(svc, proc)
        if ok:
            ports = "、".join(map(str, (svc.port, *svc.extra_ports)))
            print(f"[就绪] {svc.name} 已监听 {ports}")
        elif code is not None:
            print(f"[退出] {svc.name} 起来就退了（exit code {code}），"
                  f"窗口里应该有 traceback")
            print("        AstrBot 还可以看 data/logs/astrbot.log 的末尾")
            failed.append(svc)
        elif port_open(svc.port):
            # 主端口通了说明服务本身没事，只是附加端口没起来（多半是配置里
            # 那个监听被关了）。记一条警告继续，不拖累整个启动流程
            print(f"[警告] {svc.name} 主端口 {svc.port} 已通，"
                  f"{'、'.join(map(str, not_up))} 没监听，相关链路会连不上")
        else:
            print(f"[超时] {svc.name} 等了 {svc.ready_timeout} 秒还没起来，"
                  f"去看它自己的窗口里报了什么错")
            failed.append(svc)

    print()
    print("=" * 46)
    print("  当前状态")
    print("=" * 46)
    for svc in services:
        ports = (svc.port, *svc.extra_ports)
        if all(port_open(p) for p in ports):
            mark = "OK "
        elif port_open(svc.port):
            mark = "!! "
        elif not port_bindable(svc.port):
            mark = "占 "
        else:
            mark = " X "
        print(f"  [{mark}] {pad(svc.name, 12)} {pad('+'.join(map(str, ports)), 12)} {svc.desc}")
    print()
    print("  图例    OK=在跑   !! =主端口在、附加端口没起   占=端口被别的程序锁了   X=没起来")
    print()
    print(f"  WebUI   http://localhost:{WEBUI_PORT}")
    print("  链路    NapCat → ws://127.0.0.1:6199/ws → AstrBot")
    print("  语音    说话走 7870（CosyVoice2），唱歌走 7860（RVC）")
    print("  说明    关掉这个窗口不会停服务；停服务就关掉对应服务的黑窗口")
    print()

    if failed:
        print("  !! 有服务没起来：" + "、".join(s.name for s in failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
