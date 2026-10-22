# -*- coding: utf-8 -*-
"""CosyVoice2 常驻 TTS 服务（说话链路）。

为什么要常驻：模型加载 7.3 秒，如果每条语音都重新加载，用户要等 14 秒以上。
这里进程活着、模型只加载一次，之后每条语音只算推理那 6 秒左右。

接口（都给 wav 原始字节，前端再转 mp3）：
  GET  /health                       -> {"ok": true, "sr": 24000, "model": "..."}
  GET  /tts?text=...&style=...&mode=instruct
  POST /tts  {"text": "...", "style": "...", "mode": "instruct",
              "prompt": "参考音频绝对路径（可选）",
              "prompt_text": "参考音频转写（mode=zero_shot 时必填）"}

mode:
  instruct      情感指令 + 参考音色（默认，日常说话用这个）
  zero_shot     参考音色 + 参考文本（相似度通常更高，需要转写）
  cross_lingual 只给参考音色，不给文本（跨语种/无转写时兜底）

推理串行：GPU 只有一张卡，并发只会互相抢显存，排队反而更快。
"""
import argparse
import io
import json
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(r"D:/WorkBuddy/QQAgent/cosyvoice2")
sys.path.insert(0, str(ROOT / "CosyVoice"))
sys.path.insert(0, str(ROOT / "CosyVoice/third_party/Matcha-TTS"))

import torch
import torchaudio

MODEL_DIR = ROOT / "CosyVoice2-0.5B"
DEFAULT_PROMPT = ROOT / "ref" / "prompt_16k.wav"
DEFAULT_STYLE = "用自然、亲切、带点情绪的语气说"

LOCK = threading.Lock()
MODEL = None


def synth(text, style="", mode="instruct", prompt="", prompt_text=""):
    """返回 (wav_bytes, sample_rate, 合成耗时, 音频时长)。"""
    text = (text or "").strip()
    if not text:
        raise ValueError("text 为空")

    prompt = prompt or str(DEFAULT_PROMPT)
    if not Path(prompt).is_file():
        raise FileNotFoundError(f"参考音频不存在: {prompt}")

    with LOCK:  # 单卡，串行
        with torch.inference_mode():
            t0 = time.time()
            if mode == "zero_shot":
                if not prompt_text.strip():
                    raise ValueError("mode=zero_shot 需要 prompt_text（参考音频的转写）")
                gen = MODEL.inference_zero_shot(text, prompt_text, prompt, stream=False)
            elif mode == "cross_lingual":
                gen = MODEL.inference_cross_lingual(text, prompt, stream=False)
            else:
                tag = style.strip() or DEFAULT_STYLE
                if not tag.endswith("<|endofprompt|>"):
                    tag += "<|endofprompt|>"
                gen = MODEL.inference_instruct2(text, tag, prompt, stream=False)

            chunks = [out["tts_speech"] for out in gen]
            cost = time.time() - t0

        if not chunks:
            raise RuntimeError("模型没有返回音频")
        wav = torch.cat(chunks, dim=1).cpu()
        sr = MODEL.sample_rate
        dur = wav.shape[1] / sr

        buf = io.BytesIO()
        torchaudio.save(buf, wav, sr, format="wav")
        return buf.getvalue(), sr, cost, dur


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "CosyVoice2/0.1"

    def log_message(self, fmt, *args):  # 关掉每请求一行的噪音，只打业务日志
        pass

    def _json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _wav(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/health", "/"):
            return self._json(200, {
                "ok": MODEL is not None,
                "sr": MODEL.sample_rate if MODEL else None,
                "model": "CosyVoice2-0.5B",
                "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else "cpu",
            })
        if u.path == "/tts":
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            return self._handle_tts(q)
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        if u.path != "/tts":
            return self._json(404, {"error": "not found"})
        n = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception as e:
            return self._json(400, {"error": f"body 不是合法 JSON: {e}"})
        return self._handle_tts(payload)

    def _handle_tts(self, req):
        t_start = time.time()
        try:
            data, sr, cost, dur = synth(
                req.get("text", ""),
                req.get("style", ""),
                req.get("mode", "instruct"),
                req.get("prompt", ""),
                req.get("prompt_text", ""),
            )
        except Exception as e:
            traceback.print_exc()
            return self._json(500, {"error": f"{type(e).__name__}: {e}"})

        print(f"[tts] {dur:.1f}s 音频 / 合成 {cost:.1f}s / RTF {cost / max(dur, 1e-6):.2f} "
              f"/ 端到端 {time.time() - t_start:.1f}s :: {req.get('text', '')[:24]}",
              flush=True)
        return self._wav(data)


def main():
    global MODEL
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7870)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--fp16", action="store_true", help="半精度，显存再省一半，音质略降")
    args = ap.parse_args()

    print(f"CUDA {torch.cuda.is_available()}"
          + (f" | {torch.cuda.get_device_name(0)}" if torch.cuda.is_available() else ""),
          flush=True)

    t0 = time.time()
    try:
        from cosyvoice.cli.cosyvoice import CosyVoice2
        MODEL = CosyVoice2(str(MODEL_DIR), load_jit=False, load_trt=False, fp16=args.fp16)
    except Exception:
        traceback.print_exc()
        print("[fatal] 模型加载失败，服务不启动", flush=True)
        return 1

    if torch.cuda.is_available():
        print(f"显存 已分配 {torch.cuda.memory_allocated() / 1024 ** 3:.2f}GB "
              f"峰值 {torch.cuda.max_memory_allocated() / 1024 ** 3:.2f}GB", flush=True)
    print(f"模型加载 {time.time() - t0:.1f}s | 采样率 {MODEL.sample_rate}", flush=True)

    # 模型就绪后才 listen：端口一开就代表能出活，launcher 的端口探活才准
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    print(f"CosyVoice2 已就绪 http://{args.host}:{args.port}  (/health, /tts)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n收到中断，退出", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
