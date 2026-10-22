# -*- coding: utf-8 -*-
"""评估一段参考音频能否用于 CosyVoice2 zero-shot 克隆。

看四件事：有效语音长度、信噪比、f0 覆盖范围、有没有削波。
CosyVoice2 官方建议 3~10s 干净人声；低于 3s 或噪声大会明显掉相似度。
"""
import os
import shutil
import subprocess
import sys
import numpy as np


def _ffmpeg() -> str:
    """找 ffmpeg：环境变量 > PATH > 仓库自带 AstrBot 里那份。"""
    if os.environ.get("FFMPEG_BIN"):
        return os.environ["FFMPEG_BIN"]
    found = shutil.which("ffmpeg")
    if found:
        return found
    from pathlib import Path
    for up in range(1, 4):
        cand = Path(__file__).resolve().parents[up] / "runtime" / "astrbot" / "bin" / "ffmpeg.exe"
        if cand.is_file():
            return str(cand)
    return "ffmpeg"


FF = _ffmpeg()
SR = 16000


def load(path):
    raw = subprocess.run(
        [FF, "-v", "error", "-i", path, "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
        capture_output=True,
    ).stdout
    return np.frombuffer(raw, dtype=np.float32)


def frames(x, win=0.025, hop=0.01):
    w, h = int(SR * win), int(SR * hop)
    n = 1 + (len(x) - w) // h
    idx = np.arange(w)[None, :] + h * np.arange(n)[:, None]
    return x[idx], w, h


def f0_of(fr, fmin=60, fmax=450):
    w = len(fr)
    lo, hi = int(SR / fmax), int(SR / fmin)
    f = fr - fr.mean()
    ac = np.correlate(f, f, mode="full")[w - 1:]
    ac /= (ac[0] + 1e-12)
    seg = ac[lo:hi]
    k = int(np.argmax(seg))
    if seg[k] < 0.4:
        return np.nan
    return SR / (lo + k)


def main(path):
    x = load(path)
    dur = len(x) / SR
    fr, w, h = frames(x)
    rms = np.sqrt((fr ** 2).mean(axis=1))
    db = 20 * np.log10(rms + 1e-9)

    # 噪声底取最低 10% 分位，语音取最高 30% 分位
    floor = np.percentile(db, 10)
    speech = db[db > np.percentile(db, 60)]
    snr = speech.mean() - floor

    voiced = db > (floor + 10)
    vdur = voiced.sum() * h / SR

    f0s = np.array([f0_of(f) for f, v in zip(fr, voiced) if v])
    v = f0s[~np.isnan(f0s)]
    st = 12 * np.log2(v / 100.0) if len(v) else np.array([0.0])

    clip = float((np.abs(x) > 0.985).mean() * 100)
    head_sil = float(np.argmax(db > floor + 10) * h / SR)

    print(f"文件      {path}")
    print(f"时长      {dur:.2f}s   (有效语音 {vdur:.2f}s, 首个发声在 {head_sil:.2f}s)")
    print(f"采样率    {SR}Hz 分析源  mono")
    if len(v):
        print(f"f0       中位 {np.median(v):.0f}Hz  范围 {np.percentile(v,5):.0f}~{np.percentile(v,95):.0f}Hz"
              f"  动态 {st.std():.2f}半音")
    else:
        print("f0       未检出浊音帧")
    print(f"信噪比    约 {snr:.1f}dB (噪声底 {floor:.1f}dBFS, 语音均值 {speech.mean():.1f}dBFS)")
    print(f"削波      {clip:.2f}% 帧超 0.985")
    print()
    ok = True
    if vdur < 3:
        print("✗ 有效语音 < 3s，低于 CosyVoice2 可用下限"); ok = False
    elif vdur < 5:
        print("△ 有效语音 3~5s，能克隆但相似度会打折（官方舒适区是 5~10s）")
    else:
        print("✓ 有效语音长度足够")
    if snr < 20:
        print(f"✗ 信噪比 {snr:.1f}dB 偏低，噪声会被一起克隆进音色"); ok = False
    elif snr < 30:
        print(f"△ 信噪比 {snr:.1f}dB 一般，建议先降噪")
    else:
        print("✓ 信噪比正常")
    if clip > 0.1:
        print(f"△ 削波 {clip:.2f}%，可能有爆音")
    return ok


if __name__ == "__main__":
    main(sys.argv[1])
