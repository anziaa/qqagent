"""把整首翻唱结果裁成一小段，适合发一条 QQ 语音。

RVC 后端产出的永远是整首（3~5 分钟），而 QQ 单条语音上限 60 秒。
这里做三件事：
1. 解析 LRC，拿到每句歌词的时间点
2. 让 LLM 挑一句最适合截取的位置（通常是副歌），失败则用"重复最多的句子"兜底
3. 用 ffmpeg 截取并加淡入淡出，输出 96k 单声道 mp3
"""

import random
import re
import shutil
import subprocess
from pathlib import Path

from astrbot.api import logger

_LRC_RE = re.compile(r"\[(\d{1,2}):(\d{2})(?:[.:](\d{1,3}))?\]")
_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_TIME_RE = re.compile(r"^(?:(\d{1,2}):)?(\d{1,2}):(\d{2})$|^(\d{1,3})\s*(?:秒|s|S)$")

# 片段长度：18 秒左右，留足 QQ 语音 60 秒余量，也够听出一段旋律
DEFAULT_CLIP = 18.0
MIN_CLIP = 8.0
MAX_CLIP = 30.0

# 整首切段：QQ 语音单条上限 60 秒，55 秒留余量
FULL_SEG = 55.0
MAX_FULL_SEGMENTS = 8  # 最多 8 条 ≈ 7 分钟，再长就不唱了

_NORM_RE = re.compile(r"[\s\u3000·・,，。.!！?？、()（）\[\]【】\-—_~～'\"“”‘’]")

# 「想唱哪儿」的口语说法
_POSITION_WORDS = {
    "开头": ("head", None),
    "前奏": ("head", None),
    "第一段": ("head", None),
    "开始": ("head", None),
    "第二段": ("mid", None),
    "后半": ("mid", None),
    "中间": ("mid", None),
    "中段": ("mid", None),
    "副歌": ("chorus", None),
    "高潮": ("chorus", None),
    "最嗨": ("chorus", None),
    "结尾": ("tail", None),
    "结束": ("tail", None),
    "最后": ("tail", None),
    "换一段": ("random", None),
    "换一个": ("random", None),
    "别的": ("random", None),
    "另一段": ("random", None),
    "随机": ("random", None),
    "随便": ("random", None),
}


def _norm(text: str) -> str:
    return _NORM_RE.sub("", (text or "").lower())


def parse_lrc(lrc: str) -> list[tuple[float, str]]:
    """把 LRC 解析成 [(起始秒, 歌词), ...]，按时间排序"""
    rows: list[tuple[float, str]] = []
    for raw in (lrc or "").splitlines():
        stamps = _LRC_RE.findall(raw)
        if not stamps:
            continue
        text = _LRC_RE.sub("", raw).strip()
        if not text:
            continue
        for minute, second, frac in stamps:
            ms = int((frac or "0").ljust(3, "0")[:3])
            rows.append((int(minute) * 60 + int(second) + ms / 1000, text))
    rows.sort(key=lambda x: x[0])
    return rows


def find_ffmpeg() -> str | None:
    """找一个可用的 ffmpeg：PATH → imageio-ffmpeg 自带 → 项目 bin 目录"""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    for candidate in (r"D:\AstrBot\bin\ffmpeg.exe",):
        if Path(candidate).exists():
            return candidate
    return None


def probe_duration(ffmpeg: str, path: Path) -> float:
    """用 ffmpeg -i 的 stderr 读时长（不额外依赖 ffprobe）"""
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=30,
        )
        m = _DUR_RE.search(proc.stderr or "")
        if m:
            h, mi, s = m.groups()
            return int(h) * 3600 + int(mi) * 60 + float(s)
    except Exception as e:
        logger.warning(f"读取音频时长失败: {e}")
    return 0.0


def _overlaps(a: float, b: float, clip: float) -> bool:
    """两个起点算不算抢同一段：只要片段区间有交叠就算"""
    return abs(a - b) < clip * 0.9


def chorus_times(lines: list[tuple[float, str]], limit: float) -> list[tuple[float, str]]:
    """重复出现过的句子（大概率是副歌）对应的所有时间点"""
    counts: dict[str, int] = {}
    for _, text in lines:
        counts[text] = counts.get(text, 0) + 1
    return [(t, s) for t, s in lines if t <= limit and counts.get(s, 0) > 1]


def parse_position(part: str) -> tuple[str, float] | None:
    """把「副歌 / 开头 / 2:15 / 90秒」解析成 (类型, 秒)"""
    if not part:
        return None
    text = part.strip()
    m = _TIME_RE.match(text)
    if m:
        h, mi, s = m.group(1), m.group(2), m.group(3)
        if h is not None:
            return ("time", int(h) * 3600 + int(mi) * 60 + int(s))
        if mi is not None:
            return ("time", int(mi) * 60 + int(s))
        return ("time", float(m.group(4)))
    for word, val in _POSITION_WORDS.items():
        if word in text:
            return val
    return None


def match_lyric_start(
    lines: list[tuple[float, str]], part: str, limit: float
) -> tuple[float, list[float]]:
    """把一句歌词／歌名片段定位到时间；返回 (命中起点, 所有命中点)"""
    key = _norm(part)
    if not key:
        return 0.0, []
    hits = [(t, s) for t, s in lines if t <= limit and key and (key in _norm(s) or _norm(s) in key)]
    if not hits:
        return 0.0, []
    return max(0.0, hits[0][0] - 1.0), [max(0.0, t - 1.0) for t, _ in hits]


def fallback_start(
    lines: list[tuple[float, str]],
    duration: float,
    clip: float,
    exclude: tuple[float, ...] = (),
    rng: random.Random | None = None,
) -> float:
    """没有 LLM / LLM 失败时的兜底：副歌里随机挑一句，避开刚唱过的段。"""
    rng = rng or random
    limit = max(0.0, duration - clip)
    cands = [(t, s) for t, s in lines if t <= limit]
    if not cands:
        base = max(0.0, duration / 2 - clip / 2)
        return _avoid(base, duration, clip, exclude, rng)

    pool = chorus_times(lines, limit) or cands
    cands_ex = _filter_overlap([t for t, _ in pool], exclude, clip)
    if not cands_ex:
        cands_ex = _filter_overlap([t for t, _ in cands], exclude, clip)
    if not cands_ex:
        cands_ex = [t for t, _ in cands]
    return max(0.0, rng.choice(cands_ex) - 1.0)


def _filter_overlap(points: list[float], exclude: tuple[float, ...], clip: float) -> list[float]:
    if not exclude:
        return points
    kept = [t for t in points if all(not _overlaps(t, e, clip) for e in exclude)]
    return kept


def _avoid(
    start: float, duration: float, clip: float, exclude: tuple[float, ...], rng
) -> float:
    limit = max(0.0, duration - clip)
    if all(not _overlaps(start, e, clip) for e in exclude):
        return min(start, limit)
    for _ in range(20):
        cand = rng.uniform(0, limit)
        if all(not _overlaps(cand, e, clip) for e in exclude):
            return cand
    return min(start, limit)


def pick_start(
    lines: list[tuple[float, str]],
    duration: float,
    clip: float,
    part: str = "",
    exclude: tuple[float, ...] = (),
    rng: random.Random | None = None,
) -> float:
    """按 `part` 的意图挑一个起点。

    part 的几种写法：
      副歌 / 开头 / 结尾 / 中间 / 换一段 / 2:15 / 90秒 / 直接一句歌词
    没写 part 时：副歌里随机选一句，并避开 `exclude` 里刚唱过的位置。
    """
    rng = rng or random.Random()
    limit = max(0.0, duration - clip)

    if part:
        pos = parse_position(part)
        if pos:
            kind, sec = pos
            if kind == "time":
                return max(0.0, min(sec - 1.0, limit))
            if kind == "head":
                first = lines[0][0] if lines else 0.0
                return max(0.0, min(first - 1.0, limit))
            if kind == "mid":
                return max(0.0, min(duration / 2 - clip / 2, limit))
            if kind == "tail":
                return max(0.0, min(duration - clip - 2.0, limit))
            if kind == "chorus":
                pool = chorus_times(lines, limit)
                if pool:
                    pts = _filter_overlap([t for t, _ in pool], exclude, clip) or [t for t, _ in pool]
                    return max(0.0, rng.choice(pts) - 1.0)
                # 没识别到副歌，退回中段
                return max(0.0, min(duration / 2 - clip / 2, limit))
            if kind == "random":
                pass  # 落到下面的通用逻辑
        else:
            # 当成一句歌词去找
            _, hits = match_lyric_start(lines, part, limit)
            if hits:
                # 同一句在副歌里会重复出现多次，随机选一处，避免每次都一样
                pts = _filter_overlap(hits, exclude, clip) or hits
                return rng.choice(pts)

    return fallback_start(lines, duration, clip, exclude=exclude, rng=rng)


async def choose_start_by_llm(
    lines: list[tuple[float, str]],
    duration: float,
    clip: float,
    provider,
    exclude: tuple[float, ...] = (),
    part: str = "",
) -> float | None:
    """让模型从歌词里挑一句作为起点，返回起始秒；失败返回 None"""
    if provider is None or not lines:
        return None
    limit = max(0.0, duration - clip)
    cands = [(t, s) for t, s in lines if t <= limit]
    if not cands:
        return None

    # 控制 token：最多给 60 句
    sample = cands[:60]
    listing = "\n".join(
        f"{i + 1}. [{int(t // 60)}:{int(t % 60):02d}] {s}" for i, (t, s) in enumerate(sample)
    )
    avoid = ""
    if exclude:
        marks = "、".join(f"{int(t // 60)}:{int(t % 60):02d}" for t in exclude)
        avoid = (
            f"\n这首歌刚才已经唱过这些位置：{marks}。"
            "这次的起点必须和它们错开，不要重复同一段。\n"
        )
    intent = f"\n对方这次特别想听：{part}。请优先满足这个要求。\n" if part else ""
    prompt = (
        f"这首歌总长约 {int(duration)} 秒，我要截 {int(clip)} 秒来展示。\n"
        f"下面是带序号的歌词（方括号是起始时间）：\n{listing}\n"
        f"{avoid}{intent}\n"
        "请挑出最适合作为这一小段开头的一句，只回复那个序号数字，不要任何其它文字。"
    )
    try:
        resp = await provider.text_chat(
            prompt=prompt,
            system_prompt="你是音乐剪辑助手，只输出一个整数序号。",
        )
        text = (getattr(resp, "completion_text", "") or "").strip()
        m = re.search(r"\d+", text)
        if not m:
            return None
        idx = int(m.group()) - 1
        if 0 <= idx < len(sample):
            start = max(0.0, sample[idx][0] - 1.0)
            # 模型不听话挑了刚唱过的段，就不采信
            if exclude and any(_overlaps(start, e, clip) for e in exclude):
                return None
            return start
    except Exception as e:
        logger.warning(f"LLM 选段失败，改用兜底策略: {e}")
    return None


def plan_full_segments(
    duration: float,
    lines: list[tuple[float, str]],
    seg: float = FULL_SEG,
    max_segments: int = MAX_FULL_SEGMENTS,
) -> list[tuple[float, float]]:
    """把整首切成 [(起点, 长度)]，尽量让每段从歌词句首开始。

    超过 max_segments 的部分会丢掉（返回的长度就代表实际能发的段数）。
    """
    if duration <= 0:
        return []
    if duration <= seg:
        return [(0.0, duration)]

    starts: list[float] = [0.0]
    while starts[-1] + seg < duration - 4.0:
        raw = starts[-1] + seg
        # 在 ±4 秒内找最近的句首，让切口落在两句之间
        near = [t for t, _ in lines if raw - 4.0 <= t <= raw + 4.0]
        nxt = min(near, key=lambda t: abs(t - raw)) if near else raw
        if nxt <= starts[-1] + 10.0:  # 防止退步造成死循环
            nxt = raw
        starts.append(nxt)
        if len(starts) >= max_segments:
            break

    out: list[tuple[float, float]] = []
    for i, s in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else duration
        length = end - s
        if length < MIN_CLIP and out:
            # 末段太短，并进上一段（但别超过上限太多）
            ps, pl = out[-1]
            out[-1] = (ps, min(duration - ps, pl + length))
            continue
        out.append((s, max(1.0, min(length, seg + 8.0))))
    return out[:max_segments]



def cut_clip(
    ffmpeg: str, src: Path, start: float, length: float, out_dir: Path
) -> Path | None:
    """截取 [start, start+length]，加淡入淡出"""
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{src.stem}_clip{int(start)}_{int(length)}.mp3"
    fade_out_at = max(0.0, length - 0.8)
    af = f"afade=t=in:st=0:d=0.6,afade=t=out:st={fade_out_at:.2f}:d=0.8"
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{start:.2f}",
        "-t",
        f"{length:.2f}",
        "-i",
        str(src),
        "-af",
        af,
        "-vn",
        "-ac",
        "1",
        "-ar",
        "44100",
        "-b:a",
        "96k",
        str(out),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=120)
        if proc.returncode == 0 and out.exists() and out.stat().st_size > 1024:
            return out
        logger.error(f"截取片段失败: {proc.stderr.decode(errors='ignore')[:300]}")
    except Exception as e:
        logger.error(f"截取片段异常: {e}")
    return None
