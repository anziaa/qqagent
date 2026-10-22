# -*- coding: utf-8 -*-
"""生成 edge-tts 基线音频，参数与 astrbot_plugin_rvc_svc/speak.py 完全一致。

用法（用 astrbot 环境的 python 跑）：
    python edge_gen.py out.mp3 "要说的话" [--voice zh-CN-XiaoyiNeural]
"""
import argparse
import asyncio
import sys

import edge_tts


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("text")
    ap.add_argument("--voice", default="zh-CN-XiaoyiNeural")
    ap.add_argument("--rate", default="+0%")
    ap.add_argument("--pitch", default="+0Hz")
    args = ap.parse_args()

    comm = edge_tts.Communicate(args.text, args.voice, rate=args.rate, pitch=args.pitch, volume="+0%")
    n = 0
    with open(args.out, "wb") as f:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
                n += len(chunk["data"])
    print(f"-> {args.out} {n / 1024:.0f} KB")
    return 0 if n else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
