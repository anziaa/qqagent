"""把一句话变成"她自己的声音"，两条链路：

**主链路（CosyVoice2，7870）**
  text + 情感指令 → CosyVoice2 → wav → mp3
  音色和韵律一起生成，情感来自自然语言指令（"用撒娇、带点埋怨的语气说"）。

**兜底链路（edge-tts → RVC 7897）**
  edge-tts 出朗读腔底音 → RVC 换音色 → mp3
  RVC 只搬 f0 曲线、不生成韵律，所以这条出来是"念稿"的味道。
  7870 没开或报错时自动回落，保证不会因为新服务挂了就发不出语音。

和翻唱的区别：
- 翻唱要走 QQ 音乐下载整首 + UVR 分离人声伴奏（7860 的 /convert），慢十几秒；
- 说话只有一条干净人声，直接喂给 RVC WebUI（7897）的 /run/infer_convert。

所以兜底链路要求 RVC WebUI 在 7897 上跑着。
"""

import os
import re
import subprocess
from pathlib import Path

import aiohttp

from astrbot.api import logger


# ---------------------------------------------------------------- 文本清洗

_MD_PAT = re.compile(r"[*_`>#~]+")
_EMOJI_PAT = re.compile(
    "[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D\u2B00-\u2BFF]"
)


def clean_text(text: str) -> str:
    """去掉 TTS 念不出来的东西：markdown 符号、表情、链接、括号里的@。"""
    text = _MD_PAT.sub("", text or "")
    text = _EMOJI_PAT.sub("", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"@\S+", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def trim_text(text: str, limit: int) -> str:
    """超长时在最后一个句读处截断，避免一句话被切在词中间。"""
    if limit <= 0 or len(text) <= limit:
        return text
    head = text[:limit]
    cut = max(head.rfind(p) for p in "。！？!?；;，,、")
    if cut >= limit * 0.5:
        head = head[: cut + 1]
    return head.rstrip() + "…"


# ---------------------------------------------------------------- TTS

async def tts_edge(text: str, out_mp3: Path, voice: str, rate: str, pitch: str) -> float:
    """微软 Edge TTS，音色自然、免费，但需要联网。返回音频时长（秒，估算为 0 时未知）。"""
    import edge_tts  # 延迟导入：插件加载时不因为缺包整体失败

    kwargs = {"rate": rate or "+0%", "volume": "+0%"}
    try:
        comm = edge_tts.Communicate(text, voice, pitch=pitch or "+0Hz", **kwargs)
    except TypeError:  # 老版本 edge-tts 没有 pitch 参数
        comm = edge_tts.Communicate(text, voice, **kwargs)

    written = 0
    with open(out_mp3, "wb") as f:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
                written += len(chunk["data"])
    if written == 0:
        raise RuntimeError("edge-tts 没有返回音频数据")
    return 0.0


async def tts_baidu(text: str, out_mp3: Path, timeout: float = 20) -> bool:
    """兜底：百度翻译的朗读接口，无需 key。edge-tts 被墙/改协议时用。"""
    url = "https://fanyi.baidu.com/gettts"
    params = {"lan": "zh", "text": text, "spd": "5", "source": "web"}
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(
                url,
                params=params,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as r:
                ctype = r.headers.get("Content-Type", "")
                data = await r.read()
        if not ctype.startswith("audio") or len(data) < 2000:
            logger.warning(f"百度 TTS 返回异常: {ctype} {len(data)}B")
            return False
        out_mp3.write_bytes(data)
        return True
    except Exception as e:
        logger.warning(f"百度 TTS 失败: {e}")
        return False


async def to_speech_mp3(
    text: str,
    out_mp3: Path,
    voice: str = "zh-CN-XiaoyiNeural",
    rate: str = "+0%",
    pitch: str = "+0Hz",
) -> bool:
    """TTS 出 mp3，edge 优先，失败自动退到百度。"""
    text = clean_text(text)
    if not text:
        return False
    try:
        await tts_edge(text, out_mp3, voice, rate, pitch)
        if out_mp3.exists() and out_mp3.stat().st_size > 2000:
            logger.info(f"edge-tts 完成: {out_mp3.name} {out_mp3.stat().st_size}B")
            return True
        logger.warning("edge-tts 输出为空，改用百度")
    except Exception as e:
        logger.warning(f"edge-tts 失败({type(e).__name__}: {e})，改用百度")
    return await tts_baidu(text, out_mp3)


# ---------------------------------------------------------------- CosyVoice2

async def tts_cosyvoice(
    text: str,
    out_wav: Path,
    base_url: str = "http://127.0.0.1:7870",
    style: str = "",
    timeout: float = 180,
) -> bool:
    """调 CosyVoice2 常驻服务出 24k wav，成功返回 True。

    style 是自然语言情感指令（"用撒娇、带点埋怨的语气说"），留空则由服务端用默认语气。
    这个服务把音色和韵律一起生成，不像 RVC 那样只换音色、原样搬运 f0。
    """
    text = clean_text(text)
    if not text:
        return False
    base = (base_url or "http://127.0.0.1:7870").rstrip("/")
    payload = {"text": text, "mode": "instruct"}
    if style.strip():
        payload["style"] = style.strip()

    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.post(
                f"{base}/tts",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as r:
                data = await r.read()
                if r.status != 200:
                    logger.warning(
                        f"CosyVoice2 返回 {r.status}: {data[:200].decode('utf-8', 'ignore')}"
                    )
                    return False
    except Exception as e:
        logger.warning(f"CosyVoice2 请求失败({type(e).__name__}: {e})")
        return False

    if len(data) < 2000:
        logger.warning(f"CosyVoice2 输出过小: {len(data)}B")
        return False
    out_wav.write_bytes(data)
    logger.info(f"CosyVoice2 完成: {out_wav.name} {len(data)}B style={style or '默认'}")
    return True


# ---------------------------------------------------------------- ffmpeg

def to_wav(ffmpeg: str, src: Path, dst: Path) -> bool:
    """统一成 RVC 要的 44.1k 单声道 wav。"""
    cmd = [ffmpeg, "-y", "-v", "error", "-i", str(src),
           "-ar", "44100", "-ac", "1", "-acodec", "pcm_s16le", str(dst)]
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode != 0 or not dst.exists():
        logger.warning(f"转 wav 失败: {p.stderr.decode('utf-8', 'ignore')[:200]}")
        return False
    return True


def to_mp3(ffmpeg: str, src: Path, dst: Path, bitrate: str = "96k") -> bool:
    """成品转 mp3：QQ 语音条用 mp3 最稳（翻唱那条链路也是 mp3）。"""
    cmd = [ffmpeg, "-y", "-v", "error", "-i", str(src),
           "-ac", "1", "-ar", "44100", "-b:a", bitrate, str(dst)]
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode != 0 or not dst.exists():
        logger.warning(f"转 mp3 失败: {p.stderr.decode('utf-8', 'ignore')[:200]}")
        return False
    return True


# ---------------------------------------------------------------- RVC 换音色

async def rvc_speak(
    wav_path: Path,
    base_url: str,
    model: str,
    key_shift: float = 0,
    index_path: str = "",
    timeout: float = 300,
) -> str | None:
    """把一段人声 wav 换成指定音色，返回后端产出的文件路径。

    直接调 RVC WebUI 的 /run/infer_convert（12 个参数，顺序跟 infer-web.py 里
    vc.vc_single 的 inputs 一致），不经过 UVR 分离。
    """
    base = (base_url or "http://127.0.0.1:7897").rstrip("/")
    async with aiohttp.ClientSession() as sess:
        try:
            async with sess.post(
                f"{base}/run/infer_change_voice",
                json={"data": [model, 0.33, 0.33]},
                timeout=aiohttp.ClientTimeout(total=60),
            ) as r:
                if r.status != 200:
                    logger.warning(f"切换音色返回 {r.status}: {(await r.text())[:200]}")
        except Exception as e:
            logger.warning(f"切换音色请求失败: {e}")

        payload = {
            "data": [
                0,                    # 说话人 id
                str(Path(wav_path).resolve()),   # 输入音频（必须是绝对路径，后端按自己的 CWD 解析相对路径）
                float(key_shift or 0),
                None,                 # f0 曲线文件
                "rmvpe",              # 音高提取算法，说话用 rmvpe 最稳
                "",                   # file_index1
                index_path or "",     # file_index2
                0.75,                 # 检索特征占比
                3,                    # 中值滤波半径
                0,                    # 重采样率，0 = 不重采样
                0.25,                 # 音量包络混合
                0.33,                 # 保护清辅音
            ]
        }
        async with sess.post(
            f"{base}/run/infer_convert",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as r:
            if r.status != 200:
                logger.warning(f"RVC 推理返回 {r.status}: {(await r.text())[:200]}")
                return None
            body = await r.json()

    try:
        msg = body["data"][0]
        logger.info(f"RVC 说话推理: {str(msg).replace(chr(10), ' ')[:120]}")
        out = body["data"][1]
        path = out.get("name") if isinstance(out, dict) else out
    except Exception as e:
        logger.warning(f"解析 RVC 返回失败: {e} / {str(body)[:200]}")
        return None

    if path and os.path.isfile(path):
        return path
    logger.warning(f"RVC 没给出有效文件: {path!r}")
    return None
