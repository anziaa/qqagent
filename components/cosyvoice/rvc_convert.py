# -*- coding: utf-8 -*-
"""把一段 wav 送进 RVC WebUI(7897) 换音色，输出 mp3。

完全照抄 astrbot_plugin_rvc_svc/speak.py 的参数，保证对比实验里的 RVC
跟线上跑的是同一套配置（rmvpe / index_rate .75 / filter_radius 3 / rms_mix .25 / protect .33）。

用法（用 astrbot 环境的 python 跑）：
    python rvc_convert.py in.wav out.mp3 [--model chaotian.pth] [--index 路径] [--key 0]
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path


def _ffmpeg() -> str:
    """找 ffmpeg：环境变量 > PATH > 仓库自带 AstrBot 里那份。"""
    if os.environ.get("FFMPEG_BIN"):
        return os.environ["FFMPEG_BIN"]
    found = shutil.which("ffmpeg")
    if found:
        return found
    for up in range(1, 4):
        cand = Path(__file__).resolve().parents[up] / "runtime" / "astrbot" / "bin" / "ffmpeg.exe"
        if cand.is_file():
            return str(cand)
    return "ffmpeg"


FFMPEG = _ffmpeg()
BASE = "http://127.0.0.1:7897"
RVC_ROOT = Path(os.environ.get("RVC_HOME") or (Path(__file__).resolve().parents[2].drive + "/RVC20240604Nvidia"))


def post(path, payload, timeout=300):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def to_wav(src, dst):
    subprocess.run([FFMPEG, "-y", "-v", "error", "-i", str(src),
                    "-ar", "44100", "-ac", "1", "-acodec", "pcm_s16le", str(dst)], check=True)


def to_mp3(src, dst, bitrate="96k"):
    subprocess.run([FFMPEG, "-y", "-v", "error", "-i", str(src),
                    "-ac", "1", "-ar", "44100", "-b:a", bitrate, str(dst)], check=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inp")
    ap.add_argument("out")
    ap.add_argument("--model", default="chaotian.pth")
    ap.add_argument("--index", default="")
    ap.add_argument("--key", type=float, default=0.0)
    args = ap.parse_args()

    inp = Path(args.inp).resolve()
    tmp_wav = inp.with_name(inp.stem + "_44k.wav")
    to_wav(inp, tmp_wav)

    post("/run/infer_change_voice", {"data": [args.model, 0.33, 0.33]}, timeout=120)

    payload = {
        "data": [
            0, str(tmp_wav), float(args.key), None, "rmvpe",
            "", args.index or "", 0.75, 3, 0, 0.25, 0.33,
        ]
    }
    body = post("/run/infer_convert", payload, timeout=600)
    msg, out = body["data"][0], body["data"][1]
    path = out.get("name") if isinstance(out, dict) else out
    print("RVC:", str(msg).replace("\n", " ")[:120])
    if not path or not os.path.isfile(path):
        print("失败：后端没给出文件", path)
        return 1

    out_mp3 = Path(args.out).resolve()
    to_mp3(path, out_mp3)
    print("->", out_mp3, f"{out_mp3.stat().st_size / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
