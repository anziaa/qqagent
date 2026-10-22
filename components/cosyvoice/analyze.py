# -*- coding: utf-8 -*-
"""对比四份音频：参考素材 / edge→RVC / CosyVoice2 直出 / CosyVoice2→RVC
指标：时长、f0 均值与动态（半音）、f0 逐帧抖动、音色余弦相似度（campplus）。
"""
import os
import sys
from pathlib import Path

import numpy as np
import torch
import torchaudio
import pyworld as pw


def _root() -> Path:
    """CosyVoice2 运行目录：环境变量 > 自己所在位置。

    两个落脚点：仓库 components/cosyvoice/、部署后 cosyvoice2/。
    """
    env = os.environ.get("COSYVOICE_HOME")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    if (here.parent / "CosyVoice").is_dir():   # 已经在 cosyvoice2/ 里
        return here.parent
    return here.parents[2] / "cosyvoice2"


ROOT = _root()
sys.path.insert(0, str(ROOT / "CosyVoice"))
MODEL = str(ROOT / "CosyVoice2-0.5B")
TEXT_CHARS = 24  # 这句实际发音的字数（去掉标点）


def load(path, sr=16000):
    w, s = torchaudio.load(path, backend="soundfile")
    if w.shape[0] > 1:
        w = w.mean(0, keepdim=True)
    if s != sr:
        w = torchaudio.functional.resample(w, s, sr)
    return w[0].numpy().astype(np.float64), sr


def f0_stats(x, sr):
    f0, t = pw.harvest(x, sr, f0_floor=60, f0_ceil=500, frame_period=5.0)
    v = f0[f0 > 0]
    st = 12 * np.log2(v / 100.0)
    # 逐帧抖动：只统计相邻两帧都有效的段落
    d = np.abs(np.diff(np.where(f0 > 0, f0, np.nan)))
    d = d[~np.isnan(d)]
    dst = 12 * np.log2(1 + d / np.nanmedian(v)) if len(d) else np.array([0.0])
    return {
        "dur": len(x) / sr,
        "f0_med": float(np.median(v)),
        "std_st": float(st.std()),
        "range_st": float(np.percentile(st, 95) - np.percentile(st, 5)),
        "jitter_st": float(dst.mean()),
        "voiced_ratio": float(len(v) / len(f0)),
    }


def spk_emb(x, sr):
    if sr != 16000:
        x = torchaudio.functional.resample(torch.tensor(x).float(), sr, 16000).numpy()
    feat = torchaudio.compliance.kaldi.fbank(
        torch.tensor(x).float().unsqueeze(0), num_mel_bins=80, sample_frequency=16000, dither=0.0
    )
    feat = feat - feat.mean(dim=0, keepdim=True)
    sess = spk_emb.sess
    out = sess.run(None, {sess.get_inputs()[0].name: feat.unsqueeze(0).numpy()})[0]
    v = out.flatten()
    return v / np.linalg.norm(v)


def init_sess():
    import onnxruntime as ort
    so = ort.SessionOptions()
    so.intra_op_num_threads = 2
    return ort.InferenceSession(f"{MODEL}/campplus.onnx", so, providers=["CPUExecutionProvider"])


def rms_db(x):
    return 20 * np.log10(max(np.sqrt((x ** 2).mean()), 1e-9))


def energy_stats(x, sr, win=0.05):
    """能量包络：起伏幅度（dB 标准差）与停顿占比，是情绪的另一个侧面。"""
    n = int(sr * win)
    m = len(x) // n
    fr = x[:m * n].reshape(m, n)
    e = 20 * np.log10(np.maximum(np.sqrt((fr ** 2).mean(axis=1)), 1e-9))
    loud = e[e > e.max() - 40]
    return float(loud.std()), float((e < e.max() - 25).mean())


def main():
    files = [
        ("参考素材", str(ROOT / "ref" / "prompt_16k.wav")),
        ("① edge→RVC", str(ROOT / "out" / "1_edge_rvc.mp3")),
        ("② CosyVoice2", str(ROOT / "out" / "instruct_0.wav")),
        ("③ CV2→RVC", str(ROOT / "out" / "3_cv2_rvc.mp3")),
    ]
    spk_emb.sess = init_sess()
    ref_emb = None
    print(f"{'文件':<16}{'时长':>7}{'f0中位':>8}{'动态σ':>7}{'p5-p95':>8}{'抖动':>7}{'浊音比':>7}{'音色相似':>8}")
    rows = {}
    for name, path in files:
        x, sr = load(path)
        s = f0_stats(x, sr)
        e = spk_emb(x, sr)
        if ref_emb is None:
            ref_emb = e
        sim = float(np.dot(ref_emb, e))
        rows[name] = {**s, "sim": sim, "rms": rms_db(x)}
        print(f"{name:<16}{s['dur']:>6.2f}s{s['f0_med']:>7.0f}Hz{s['std_st']:>6.2f}{s['range_st']:>7.2f}"
              f"{s['jitter_st']:>7.3f}{s['voiced_ratio']:>7.2f}{sim:>8.3f}")


if __name__ == "__main__":
    main()
