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
"""

import sys
from pathlib import Path, PurePath

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parents[1]

# (短名, 仓库内目录, 运行目录, 要同步的文件模式)
SPEC = [
    (
        "plugin",
        "components/astrbot_plugin_rvc_svc",
        r"D:\AstrBot\data\plugins\astrbot_plugin_rvc_svc",
        ["*.py", "*.json", "*.yaml", "*.md", "*.txt", "LICENSE"],
    ),
    (
        "rvc_api",
        "components/rvc_api",
        r"D:\RVCSVC-API\RVCSVC-API",
        ["*.py", "*.txt"],
    ),
    (
        "cosyvoice",
        "components/cosyvoice",
        r"D:\WorkBuddy\QQAgent\cosyvoice2",
        ["*.py", "*.sh"],
    ),
    (
        "scripts",
        "scripts",
        r"D:\AstrBot",
        ["launcher.py", "*.bat"],
    ),
]

RESTART_HINT = {
    "plugin": "重启 AstrBot（插件热重载有时不干净）",
    "rvc_api": "重启 7860 RVC 转换 API",
    "cosyvoice": "重启 7870 CosyVoice2",
    "scripts": "重新执行 一键启动服务.bat",
}


def collect(src_dir: Path, patterns: list[str]) -> list[PurePath]:
    """只取顶层文件，按模式匹配，不递归（源码目录本来就是平的）。"""
    files = []
    for pat in patterns:
        files.extend(p for p in src_dir.glob(pat) if p.is_file())
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

    for name, src_rel, dst_abs, patterns in targets:
        src_dir = ROOT / src_rel
        dst_dir = Path(dst_abs)
        if not src_dir.is_dir():
            print(f"[{name}] 仓库内目录不存在: {src_dir}")
            continue
        if not dst_dir.is_dir():
            print(f"[{name}] 运行目录不存在，跳过: {dst_dir}")
            continue

        new = changed = same = 0
        for src in collect(src_dir, patterns):
            dst = dst_dir / src.name
            if not dst.exists():
                new += 1
                if not check_only:
                    dst.write_bytes(src.read_bytes())
                print(f"  + {src.name}")
                continue
            if src.read_bytes() == dst.read_bytes():
                same += 1
                continue
            changed += 1
            if dst.stat().st_mtime > src.stat().st_mtime:
                drift.append(f"{name}/{src.name}")
            if not check_only:
                dst.write_bytes(src.read_bytes())
            print(f"  ~ {src.name}")

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
