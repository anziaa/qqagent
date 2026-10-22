# -*- coding: utf-8 -*-
"""把仓库里的源码同步到运行位置。

这个仓库是唯一真源，但三个组件跑在各自的目录里：
  - 插件跑在 AstrBot 的 data/plugins 下
  - 翻唱后端跑在 RVCSVC-API 自己的目录（cwd 里绑着 venv / uvr5 模型 / output）
  - 语音服务跑在 cosyvoice2 目录（绑着 4.8G 模型和 venv）

所以不改运行位置，只把源码推过去。改完代码执行一次本脚本，再重启对应服务。

用法：
    python scripts/deploy.py            同步全部
    python scripts/deploy.py plugin     只同步插件
    python scripts/deploy.py --check    只检查差异，不写入

三个运行位置默认按下面 DEPLOY_DEFAULTS 走，也可以用环境变量覆盖：
    ASTRBOT_HOME     AstrBot 根目录（默认 runtime/astrbot）
    RVCSVC_HOME      翻唱后端目录（默认 D:\\RVCSVC-API\\RVCSVC-API）
    COSYVOICE_HOME   CosyVoice2 目录（默认 cosyvoice2）
"""

import os
import sys
from pathlib import Path, PurePath

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parents[1]

ASTRBOT_HOME = Path(os.environ.get("ASTRBOT_HOME") or ROOT / "runtime" / "astrbot")
RVCSVC_HOME = Path(os.environ.get("RVCSVC_HOME") or r"D:\RVCSVC-API\RVCSVC-API")
COSYVOICE_HOME = Path(os.environ.get("COSYVOICE_HOME") or ROOT / "cosyvoice2")

# (短名, 仓库内目录, 运行目录, 要同步的文件模式, 是否递归子目录)
SPEC = [
    (
        "plugin",
        "components/astrbot_plugin_rvc_svc",
        ASTRBOT_HOME / "data" / "plugins" / "astrbot_plugin_rvc_svc",
        ["*.py", "*.json", "*.yaml", "*.md", "*.txt", "LICENSE"],
        False,
    ),
    (
        "relation",
        "components/astrbot_plugin_relation",
        ASTRBOT_HOME / "data" / "plugins" / "astrbot_plugin_relation",
        ["*.py", "*.json", "*.yaml", "*.txt"],
        False,
    ),
    (
        "self_profile",
        "components/astrbot_plugin_self_profile",
        ASTRBOT_HOME / "data" / "plugins" / "astrbot_plugin_self_profile",
        ["*.py", "*.json", "*.yaml", "*.txt"],
        False,
    ),
    # 下面三个是上游插件，本地魔改过，所以源码也入库
    (
        "music",
        "components/astrbot_plugin_music",
        ASTRBOT_HOME / "data" / "plugins" / "astrbot_plugin_music",
        ["*.py", "*.json", "*.yaml", "*.md", "*.txt", "*.html"],
        True,
    ),
    (
        "qzone",
        "components/astrbot_plugin_qzone",
        ASTRBOT_HOME / "data" / "plugins" / "astrbot_plugin_qzone",
        ["*.py", "*.json", "*.yaml", "*.md", "*.txt", "*.html", "*.css", "*.js"],
        True,
    ),
    (
        "favourpro",
        "components/astrbot_plugin_favourpro",
        ASTRBOT_HOME / "data" / "plugins" / "astrbot_plugin_favourpro",
        ["*.py", "*.json", "*.yaml", "*.md", "*.txt"],
        True,
    ),
    (
        "rvc_api",
        "components/rvc_api",
        RVCSVC_HOME,
        ["*.py", "*.txt"],
        False,
    ),
    (
        "cosyvoice",
        "components/cosyvoice",
        COSYVOICE_HOME,
        ["*.py", "*.sh"],
        False,
    ),
    (
        "scripts",
        "scripts",
        ASTRBOT_HOME,
        ["launcher.py", "*.bat"],
        False,
    ),
    (
        "tools",
        "scripts/tools",
        ASTRBOT_HOME / "tools",
        ["*.py"],
        False,
    ),
]

RESTART_HINT = {
    "plugin": "重启 AstrBot（插件热重载有时不干净）",
    "relation": "重启 AstrBot",
    "self_profile": "重启 AstrBot",
    "music": "重启 AstrBot",
    "qzone": "重启 AstrBot",
    "favourpro": "重启 AstrBot",
    "rvc_api": "重启 7860 RVC 转换 API",
    "cosyvoice": "重启 7870 CosyVoice2",
    "scripts": "重新执行 一键启动服务.bat",
    "tools": "不用重启，脚本按需手动跑",
}

# 这些路径不参与同步：运行时数据、缓存
SKIP_PARTS = {"__pycache__", "data", "output", "temp", "logs", ".git"}


def collect(src_dir: Path, patterns: list[str], recursive: bool = False) -> list[PurePath]:
    """按模式收集文件，返回相对 src_dir 的路径。

    recursive=False 只取顶层（多数组件源码本来就是平的）；
    True 用于 music / qzone / favourpro 这种带 core/ pages/ 子目录的插件。
    """
    files = []
    walker = src_dir.rglob if recursive else src_dir.glob
    for pat in patterns:
        for p in walker(pat):
            if not p.is_file():
                continue
            rel = p.relative_to(src_dir)
            if SKIP_PARTS & set(rel.parts) or p.suffix == ".pyc":
                continue
            files.append(rel)
    return sorted(set(files))


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    check_only = "--check" in sys.argv

    targets = SPEC
    if args:
        wanted = set(args)
        unknown = wanted - {s[0] for s in SPEC}
        if unknown:
            print(f"未知目标: {', '.join(sorted(unknown))}")
            print(f"可用目标: {', '.join(s[0] for s in SPEC)}")
            return 2
        targets = [s for s in SPEC if s[0] in wanted]

    total_new = total_changed = total_same = 0
    drift = []  # 运行位置比仓库还新，可能是直接在运行目录改过
    need_restart = []

    for name, src_rel, dst_abs, patterns, recursive in targets:
        src_dir = ROOT / src_rel
        dst_dir = Path(dst_abs)
        if not src_dir.is_dir():
            print(f"[{name}] 仓库内目录不存在: {src_dir}")
            continue
        if not dst_dir.is_dir():
            print(f"[{name}] 运行目录不存在，跳过: {dst_dir}")
            continue

        new = changed = same = 0
        for rel in collect(src_dir, patterns, recursive):
            src = src_dir / rel
            dst = dst_dir / rel
            if not dst.exists():
                new += 1
                if not check_only:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes(src.read_bytes())
                print(f"  + {rel}")
                continue
            if src.read_bytes() == dst.read_bytes():
                same += 1
                continue
            changed += 1
            if dst.stat().st_mtime > src.stat().st_mtime:
                drift.append(f"{name}/{rel}")
            if not check_only:
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(src.read_bytes())
            print(f"  ~ {rel}")

        total_new += new
        total_changed += changed
        total_same += same
        if new or changed:
            need_restart.append(name)
        flag = "仅检查" if check_only else "已同步"
        print(f"[{name}] {flag}: 新增 {new} / 改动 {changed} / 一致 {same}")

    print()
    print(f"合计: 新增 {total_new}，改动 {total_changed}，一致 {total_same}")

    if drift:
        print()
        print("⚠ 以下文件运行位置比仓库更新，可能是你直接在运行目录改的：")
        for d in drift:
            print(f"    {d}")
        print("  这次同步会用仓库版本覆盖掉它们。如果是调试时的临时改动，")
        print("  先把它搬回仓库再提交，别让它丢掉。")

    if check_only:
        return 1 if (total_new or total_changed) else 0

    if need_restart:
        print()
        print("下一步需要重启：")
        for name in need_restart:
            print(f"    {name}: {RESTART_HINT[name]}")
    else:
        print("运行位置已经和仓库一致，不用重启。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
