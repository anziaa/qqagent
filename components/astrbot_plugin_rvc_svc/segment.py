"""把整首翻唱结果裁成一小段，适合发一条 QQ 语音。

RVC 后端产出的永远是整首（3~5 分钟），而 QQ 单条语音上限 60 秒。
这里做三件事：
1. 解析 LRC，拿到每句歌词的时间点
2. 让 LLM 挑一句最适合截取的位置（通常是副歌），失败则用"重复最多的句子"兜底
3. 用 ffmpeg 截取并加淡入淡出，输出 96k 单声道 mp3
"""

import asyncio
import os
import random
import re
import shutil
import subprocess
from pathlib import Path

from astrbot.api import logger

_LRC_RE = re.compile(r"\[(\d{1,2}):(\d{2})(?:[.:](\d{1,3}))?\]")
_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_TIME_RE = re.compile(r"^(?:(\d{1,2}):)?(\d{1,2}):(\d{2})$|^(\d{1,3})\s*(?:秒|s|S)$")

# 片段长度：18 秒只是「目标」，实际长度会吸附到最近的歌词句末，
# 保证切口落在两句之间而不是半句腰上，所以真实时长会在目标上下浮动
DEFAULT_CLIP = 18.0
MIN_CLIP = 8.0
MAX_CLIP = 30.0
CLIP_SLACK = 0.45      # 允许在目标长度 ±45% 内浮动，用来换句末对齐
LINE_GAP = 0.35        # 切口贴到下一句之前留的空白，别咬字
EXPLICIT_MAX = 50.0    # 对方点名「唱四句」时允许超过 MAX_CLIP

# 让模型挑起点时最多等多久。模型只在「规则认不出来」时兜底，这时候多等一会儿也值得——
# 放弃就等于瞎挑一段。但实测推理型模型会在「哪一段算 bridge」上纠结上万个 token，
# 有一次烧了 50 秒，所以仍然要有个上限，超时就退到 late_fallback_start()。
PICK_TIMEOUT = 20.0
SECTION_MIN = 6.0      # 一个段落至少 6 秒才算段落
SECTION_MAX = 40.0     # 超过 40 秒的多半是没识别出副歌，不能整段当桥段唱

# 整首切段：QQ 语音单条上限 60 秒，55 秒留余量
FULL_SEG = 55.0
MAX_FULL_SEGMENTS = 8  # 最多 8 条 ≈ 7 分钟，再长就不唱了

_TITLE_RE = re.compile(r"\s+[-–—]\s+")  # "歌名 - 歌手"
_NORM_RE = re.compile(r"[\s\u3000·・,，。.!！?？、()（）\[\]【】\-—_~～'\"“”‘’]")


# LRC 里夹的元信息行（词：xxx / Lyrics by：xxx / 编曲：xxx），既不是歌词也影响结构判断
_META_RE = re.compile(
    r"^\s*(作词|作曲|编曲|词|曲|制作人|监制|混音|录音|录音室|和声|出品|发行|母带|原唱|"
    r"lyrics|composed|arranged|produced|written|mixed|mastered|op|sp)"
    r"[^\u4e00-\u9fa5]{0,6}[:：]",
    re.I,
)

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
    "桥段": ("bridge", None),
    "桥": ("bridge", None),
    "bridge": ("bridge", None),
    "过渡段": ("bridge", None),
    "过渡": ("bridge", None),
    "插段": ("bridge", None),
    "主歌": ("verse", None),
    "verse": ("verse", None),
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


# 「唱两句 / 来四句 / 唱1句」里的句数
_CN_NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8}
_COUNT_RE = re.compile(r"(\d{1,2}|[一两二三四五六七八])\s*(?:句|行)")


def requested_lines(part: str) -> int | None:
    """对方要求唱几句；没提就是 None"""
    m = _COUNT_RE.search(part or "")
    if not m:
        return None
    raw = m.group(1)
    n = int(raw) if raw.isdigit() else _CN_NUM.get(raw, 0)
    return n if 0 < n <= 12 else None


def parse_lrc(lrc: str) -> list[tuple[float, str]]:
    """把 LRC 解析成 [(起始秒, 歌词), ...]，按时间排序"""
    rows: list[tuple[float, str]] = []
    seen_lyric = False
    for raw in (lrc or "").splitlines():
        stamps = _LRC_RE.findall(raw)
        if not stamps:
            continue
        text = _LRC_RE.sub("", raw).strip()
        if not text or _META_RE.match(text):
            continue
        for minute, second, frac in stamps:
            ms = int((frac or "0").ljust(3, "0")[:3])
            t = int(minute) * 60 + int(second) + ms / 1000
            # QQ 有时把「歌名 - 歌手」当成第一行歌词塞进来（0 秒附近），不是歌词
            if not seen_lyric and t < 6.0 and _TITLE_RE.search(text):
                continue
            seen_lyric = True
            rows.append((t, text))
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
    # AstrBot 自带的 bin（插件装在 <AstrBot>/data/plugins/<名>/ 下，往上三级是根）
    home = os.environ.get("ASTRBOT_HOME")
    abin = Path(home) if home else Path(__file__).resolve().parents[3]
    for candidate in (str(abin / "bin" / "ffmpeg.exe"),):
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


def overlaps(a: float, b: float, clip: float) -> bool:
    """给插件主逻辑用的公开别名：判断两个起点是不是同一段。"""
    return _overlaps(a, b, clip)


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


def late_fallback_start(
    lines: list[tuple[float, str]],
    duration: float,
    clip: float,
    exclude: tuple[float, ...] = (),
    rng: random.Random | None = None,
) -> float | None:
    """点名了某一段、但模型和规则都没认出来时用：别给开头。

    对方说「唱 bridge」而这首认不出桥段，最糟的结果就是从 0 秒开始——
    那正是之前被吐槽的「说 bridge 却唱开头」。这里退而求其次：
    在全曲 45%~90% 之间挑一句整首只唱一次的歌词（bridge 通常是这种句子），
    实在没有再放宽到整首后半段。
    """
    rng = rng or random.Random()
    limit = max(0.0, duration - clip)
    if not lines or duration <= clip:
        return None

    counts: dict[str, int] = {}
    for _, s in lines:
        k = _norm(s)
        counts[k] = counts.get(k, 0) + 1

    lo, hi = duration * 0.45, duration * 0.9
    in_band = [(t, s) for t, s in lines if lo <= t <= min(hi, limit)]
    if not in_band:
        in_band = [(t, s) for t, s in lines if t >= lo * 0.7 and t <= limit]
    if not in_band:
        return None

    uniq = [t for t, s in in_band if counts.get(_norm(s), 0) <= 1]
    pool = uniq or [t for t, _ in in_band]
    pool = _filter_overlap(pool, exclude, clip) or pool
    return max(0.0, rng.choice(pool) - 1.0)


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


def line_count_end(
    lines: list[tuple[float, str]], start: float, count: int, duration: float
) -> float | None:
    """从 start 起数 count 句，返回这几句唱完的时间点（≈ 第 count+1 句起点前一点）"""
    after = [t for t, _ in lines if t >= start - 0.5]
    if len(after) > count:
        return max(after[0] + 1.0, after[count] - LINE_GAP)
    if after:
        # 剩下的句子不够数，就唱到结尾
        return duration
    return None


def snap_end(
    lines: list[tuple[float, str]], start: float, target: float, lo: float, hi: float
) -> float:
    """把终点吸附到离目标最近的句末，返回片段长度。

    候选句末 = 每一句的起点减一点点（让切口落在两句之间的空隙里）。
    区间 [lo, hi] 里一个句末都没有，就只能退回目标长度（总比没有强）。
    """
    cands = [
        t - LINE_GAP
        for t, _ in lines
        if start + lo <= t - LINE_GAP <= start + hi
    ]
    if not cands:
        return max(lo, min(target, hi))
    want = start + target
    return min(cands, key=lambda e: abs(e - want)) - start


def _clip_range(
    target: float, min_len: float = MIN_CLIP, max_len: float = MAX_CLIP
) -> tuple[float, float]:
    """片段长度的允许区间（围绕目标值 ±45%）"""
    lo = max(3.0, min(min_len, target * (1 - CLIP_SLACK)))
    hi = max(lo + 1.0, min(max_len, target * (1 + CLIP_SLACK)))
    return lo, hi


def _runs(flags: list[bool]) -> list[list[int]]:
    """把布尔序列切成连续 True 的下标块"""
    out: list[list[int]] = []
    cur: list[int] = []
    for i, f in enumerate(flags):
        if f:
            cur.append(i)
        elif cur:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def _chorus_flags(lines: list[tuple[float, str]], max_n: int = 3) -> list[bool]:
    """标记哪些句属于「会重唱的那一段」。

    单个句子重复说明不了什么（一句口号反复出现很常见），
    真正能认副歌的是**连续几句整块再唱一遍**，所以按 n 句滑窗找重复。
    """
    keys = [_norm(s) for _, s in lines]
    flags = [False] * len(keys)
    for n in range(max_n, 1, -1):
        seen: dict[tuple[str, ...], list[int]] = {}
        for i in range(len(keys) - n + 1):
            gram = tuple(keys[i : i + n])
            if any(not k for k in gram):
                continue
            seen.setdefault(gram, []).append(i)
        for idxs in seen.values():
            if len(idxs) < 2:
                continue
            for i in idxs:
                for j in range(i, i + n):
                    flags[j] = True
    return flags


def section_spans(
    lines: list[tuple[float, str]], duration: float
) -> dict[str, object]:
    """从歌词的重复结构里猜段落。

    副歌 = 连续几句整块重唱的区域；剩下的就是不重复的主歌/桥段。
    桥段取经典位置：前面有副歌、后面还有副歌、自己不重复，取最靠后的那段。
    猜错也没关系，调用方拿不到会退回随机/让模型挑。
    """
    chorus: list[tuple[float, float]] = []
    verse: list[tuple[float, float]] = []
    if not lines:
        return {"chorus": chorus, "verse": verse, "bridge": None}

    is_ch = _chorus_flags(lines)

    def to_span(idx: list[int]) -> tuple[float, float]:
        start = max(0.0, lines[idx[0]][0] - 1.0)
        last = idx[-1]
        if last + 1 < len(lines):
            end = lines[last + 1][0] - LINE_GAP
        else:
            end = min(duration, lines[last][0] + 3.0)
        return start, max(start + 1.0, end)

    for block in _runs(is_ch):
        chorus.append(to_span(block))
    for block in _runs([not f for f in is_ch]):
        verse.append(to_span(block))

    # 副歌至少要出现两次才像副歌；只有一次说明重复检测没抓到，别硬认
    if len(chorus) < 2:
        chorus = []

    bridge: tuple[float, float] | None = None
    # 一段 6~40 秒才像一个段落；几百秒的"段"是重复检测没抓到，不能当桥段。
    # 位置也限定在 28%~88%：桥段不可能在开场，88% 往后那是尾声。
    floor = duration * 0.28
    ceil_ = duration * 0.88
    cands = [
        sp for sp in verse
        if SECTION_MIN <= sp[1] - sp[0] <= SECTION_MAX and floor <= sp[0] <= ceil_
    ]
    if chorus:
        first = chorus[0][0]
        later = [sp for sp in cands if sp[0] > first]
        if later:
            cands = later
    if cands:
        # 桥段后面通常还有一遍副歌；满足这个条件的里取最靠后的
        with_next = [sp for sp in cands if any(c[0] >= sp[1] - 0.5 for c in chorus)]
        pool = with_next or cands
        bridge = max(pool, key=lambda sp: (sp[0], sp[1] - sp[0]))

    return {"chorus": chorus, "verse": verse, "bridge": bridge}


def locate_part(
    lines: list[tuple[float, str]],
    duration: float,
    part: str,
    target: float = DEFAULT_CLIP,
    exclude: tuple[float, ...] = (),
    rng: random.Random | None = None,
) -> tuple[float, float] | None:
    """把 part 定成 (起点, 长度)；实在定位不了返回 None，交给上层兜底。

    和 `pick_start` 的区别：定位失败时**明确说失败**，而不是悄悄随机一句
    ——以前 "bridge" 就是这么被当成歌词、匹配不到、最后随机到开头去的。
    """
    if not part or not part.strip():
        return None
    rng = rng or random.Random()
    pos = parse_position(part)

    if pos and pos[0] in ("chorus", "bridge", "verse"):
        spans = section_spans(lines, duration)
        picked: tuple[float, float] | None = None
        if pos[0] == "chorus":
            pool: list[tuple[float, float]] = list(spans["chorus"])  # type: ignore[arg-type]
            if pool:
                free = [
                    sp for sp in pool
                    if not any(_overlaps(sp[0], e, target) for e in exclude)
                ]
                pool = free or pool
                # 优先挑一个「完整且不太长」的副歌块（完整副歌比截一半好听）
                fit = [sp for sp in pool if sp[1] - sp[0] <= SECTION_MAX]
                picked = max(fit or pool, key=lambda sp: sp[1] - sp[0])
        elif pos[0] == "verse":
            pool = list(spans["verse"])  # type: ignore[arg-type]
            fit = [sp for sp in pool if SECTION_MIN <= sp[1] - sp[0] <= SECTION_MAX]
            picked = (fit or pool)[0] if (fit or pool) else None
        else:
            picked = spans["bridge"]  # type: ignore[assignment]
        if picked:
            start, end = picked
            length = end - start
            if length > SECTION_MAX:
                # 段落被判得过大（副歌没认出来，整段糊成一片），
                # 就从段首按目标长度截，别一口气发 50 秒
                lo, hi = _clip_range(target)
                length = snap_end(lines, start, target, lo, hi)
            length = min(max(3.0, length), EXPLICIT_MAX, duration - start)
            return start, length
        return None

    if pos is None:
        # 当成一句歌词去找，先确认真的匹配得上
        limit = max(0.0, duration - target)
        key = _norm(part)
        hit = any(
            t <= limit and (key in _norm(s) or (_norm(s) and _norm(s) in key))
            for t, s in lines
        )
        if not hit:
            return None

    start = pick_start(lines, duration, target, part=part, exclude=exclude, rng=rng)
    start = max(0.0, min(start, max(0.0, duration - target)))
    lo, hi = _clip_range(target)
    length = snap_end(lines, start, target, lo, hi)
    return start, max(3.0, min(length, duration - start))


def plan_clip(
    lines: list[tuple[float, str]],
    duration: float,
    target: float = DEFAULT_CLIP,
    part: str = "",
    exclude: tuple[float, ...] = (),
    rng: random.Random | None = None,
    min_len: float = MIN_CLIP,
    max_len: float = MAX_CLIP,
    start_hint: float | None = None,
) -> tuple[float, float]:
    """定片段的 (起点, 长度)。

    - 长度不再写死：target 只是目标值，终点会吸附到最近的句末
    - 对方说了「唱两句 / 四句」：按句数收尾，允许超过 MAX_CLIP
    - start_hint 给了就用它当起点（LLM 已经挑过的情况），只负责算长度
    - 没歌词时退回 target，行为与老版本一致
    """
    rng = rng or random.Random()
    lo, hi = _clip_range(target, min_len, max_len)

    if start_hint is not None:
        start = max(0.0, min(start_hint, max(0.0, duration - lo)))
    else:
        start = pick_start(lines, duration, target, part=part, exclude=exclude, rng=rng)
        start = max(0.0, min(start, max(0.0, duration - lo)))

    count = requested_lines(part)
    if count and lines:
        end = line_count_end(lines, start, count, duration)
        if end is not None:
            length = min(end - start, EXPLICIT_MAX, duration - start)
            if length >= 3.0:
                return start, length

    # 指定了段落（副歌/bridge/主歌）且起点落在某个推断出的段落里：
    # 就唱到那段结束，而不是硬截目标长度——"唱副歌"要的是完整副歌，不是副歌的前 18 秒
    pos = parse_position(part) if part else None
    kind = pos[0] if pos else None
    if kind in ("chorus", "bridge", "verse") and lines:
        spans = section_spans(lines, duration)
        pool: list[tuple[float, float]] = []
        if kind == "chorus":
            pool = list(spans["chorus"])  # type: ignore[arg-type]
        elif kind == "verse":
            pool = list(spans["verse"])  # type: ignore[arg-type]
        elif spans["bridge"]:
            pool = [spans["bridge"]]  # type: ignore[list-item]
        for sp in pool:
            if sp[0] - 0.5 <= start <= sp[1]:
                length = min(sp[1] - start, SECTION_MAX, duration - start)
                if length >= 3.0:
                    return start, length

    length = snap_end(lines, start, target, lo, hi)
    length = max(3.0, min(length, duration - start))
    return start, length


async def choose_start_by_llm(
    lines: list[tuple[float, str]],
    duration: float,
    clip: float,
    provider,
    exclude: tuple[float, ...] = (),
    part: str = "",
    timeout: float = PICK_TIMEOUT,
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
    # 标出不重复的句子：模型靠这个把副歌和主歌/桥段分开（副歌会重唱，主歌/桥段只唱一次）
    counts: dict[str, int] = {}
    for _, s in lines:
        k = _norm(s)
        counts[k] = counts.get(k, 0) + 1
    listing = "\n".join(
        f"{i + 1}. [{int(t // 60)}:{int(t % 60):02d}] {s}"
        + (" *" if counts.get(_norm(s), 0) <= 1 else "")
        for i, (t, s) in enumerate(sample)
    )
    avoid = ""
    if exclude:
        marks = "、".join(f"{int(t // 60)}:{int(t % 60):02d}" for t in exclude)
        avoid = (
            f"\n这首歌刚才已经唱过这些位置：{marks}。"
            "这次的起点必须和它们错开，不要重复同一段。\n"
        )
    intent = ""
    kind = None
    if part:
        pos = parse_position(part)
        kind = pos[0] if pos else None
        section = {
            "bridge": (
                "对方要的是 bridge（桥段/过渡段），也就是副歌之间那段不重复的歌词，"
                "通常位于歌曲后半、最后一遍副歌之前。别给开头，也别给副歌。"
            ),
            "chorus": "对方要的是副歌，也就是整首里重复出现最多次的那一段。",
            "verse": "对方要的是主歌，通常是不重复、靠前的那一段，不是副歌。",
        }.get(kind or "")
        intent = f"\n对方这次特别想听：{part}。" + (section or "请优先满足这个要求。") + "\n"
    prompt = (
        f"这首歌总长约 {int(duration)} 秒，我要截 {int(clip)} 秒来展示。\n"
        f"下面是带序号的歌词（方括号是起始时间，句尾带 * 的是整首只唱过一次的句子，"
        f"没带 * 的是会重唱的副歌）：\n{listing}\n"
        f"{avoid}{intent}\n"
        "请挑出最适合作为这一小段开头的一句，只回复那个序号数字，不要任何其它文字。"
    )
    try:
        resp = await asyncio.wait_for(
            provider.text_chat(
                prompt=prompt,
                system_prompt="你是音乐剪辑助手，只输出一个整数序号。",
            ),
            timeout=timeout,
        )
        text = (getattr(resp, "completion_text", "") or "").strip()
        m = re.search(r"\d+", text)
        if not m:
            return None
        idx = int(m.group()) - 1
        if 0 <= idx < len(sample):
            # 挑了跟意图矛盾的句子就不采信：要 bridge（不重复的段）却挑了会重唱的句子，
            # 要副歌却挑了一句只唱一次的。只管有「更好的候选」的情况，避免把全曲无重复的歌一刀切。
            chosen = _norm(sample[idx][1])
            dup = counts.get(chosen, 0) > 1
            has_dup = any(c > 1 for c in counts.values())
            has_uniq = any(c <= 1 for c in counts.values())
            if kind == "bridge" and dup and has_uniq:
                logger.info("模型给 bridge 挑中了重唱句，判为无效")
                return None
            if kind == "chorus" and not dup and has_dup:
                logger.info("模型给副歌挑中了只唱一次的句子，判为无效")
                return None
            start = max(0.0, sample[idx][0] - 1.0)
            # 模型不听话挑了刚唱过的段，就不采信
            if exclude and any(_overlaps(start, e, clip) for e in exclude):
                return None
            return start
    except asyncio.TimeoutError:
        logger.warning(f"模型挑段超时（{timeout:.0f}s），改用规则定位")
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
