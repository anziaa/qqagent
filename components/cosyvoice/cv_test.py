# -*- coding: utf-8 -*-
"""CosyVoice2 最小验证：用 chaotian 的 3.67s 素材做 zero-shot / instruct 合成。

用法：
    python cv_test.py "要说的话"              # 纯克隆音色
    python cv_test.py "要说的话" --instruct   # 加情感指令
"""
import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(r"D:/WorkBuddy/QQAgent/cosyvoice2")
sys.path.insert(0, str(ROOT / "CosyVoice"))
sys.path.insert(0, str(ROOT / "CosyVoice/third_party/Matcha-TTS"))

import torch
import torchaudio

from cosyvoice.cli.cosyvoice import CosyVoice2
from cosyvoice.utils.file_utils import load_wav

MODEL_DIR = ROOT / "CosyVoice2-0.5B"
PROMPT = ROOT / "ref/prompt_16k.wav"
OUT = ROOT / "out"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("text")
    ap.add_argument("--instruct", action="store_true", help="用情感指令模式")
    ap.add_argument("--style", default="用温柔撒娇的语气说")
    ap.add_argument("--prompt-text", default="", help="参考音频的转写，填了就用 zero_shot")
    ap.add_argument("--fp16", action="store_true")
    args = ap.parse_args()

    print(f"CUDA: {torch.cuda.is_available()}  {torch.cuda.get_device_name(0) if torch.cuda.is_available() else ''}")
    t0 = time.time()
    model = CosyVoice2(str(MODEL_DIR), load_jit=False, load_trt=False, fp16=args.fp16)
    print(f"模型加载 {time.time() - t0:.1f}s  采样率 {model.sample_rate}")
    if torch.cuda.is_available():
        alloc = torch.cuda.memory_allocated() / 1024 ** 3
        peak = torch.cuda.max_memory_allocated() / 1024 ** 3
        print(f"显存: 已分配 {alloc:.2f}GB  峰值 {peak:.2f}GB")

    prompt = str(PROMPT)
    OUT.mkdir(exist_ok=True)

    t0 = time.time()
    if args.instruct:
        style = args.style if args.style.endswith('<|endofprompt|>') else args.style + '<|endofprompt|>'
        gen = model.inference_instruct2(args.text, style, prompt, stream=False)
        tag = "instruct"
    elif args.prompt_text:
        gen = model.inference_zero_shot(args.text, args.prompt_text, prompt, stream=False)
        tag = "zeroshot"
    else:
        gen = model.inference_cross_lingual(args.text, prompt, stream=False)
        tag = "crosslingual"

    saved = []
    for i, out in enumerate(gen):
        p = OUT / f"{tag}_{i}.wav"
        torchaudio.save(str(p), out["tts_speech"], model.sample_rate)
        saved.append(p)
    cost = time.time() - t0

    dur = 0.0
    for p in saved:
        info = torchaudio.info(str(p))
        dur += info.num_frames / info.sample_rate
    print(f"合成 {dur:.1f}s 音频，耗时 {cost:.1f}s，RTF={cost / max(dur, 1e-6):.3f}")
    if torch.cuda.is_available():
        print(f"推理峰值显存 {torch.cuda.max_memory_allocated() / 1024 ** 3:.2f}GB")
    for p in saved:
        print(" ->", p)


if __name__ == "__main__":
    main()
