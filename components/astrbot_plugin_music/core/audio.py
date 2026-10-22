"""音频切片工具：把超过 QQ 语音时长上限的歌曲切成多条可发送的语音片段。

QQ 单条语音消息上限 60s，超过后 NapCat 转 silk 会被服务端拒绝，
插件原本会降级到文件模式。这里在发送前用 ffmpeg 切段，保留语音形态。
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import os
import re
import shutil
import subprocess
from pathlib import Path

from astrbot.api import logger

# QQ 单条语音上限 60s，留足余量（mp3 编码后帧对齐会多出零点几秒）
VOICE_MAX_SECONDS = 56.0
# 切段后的码率，64k 单声道足够人声/流行乐，体积也可控
_SPLIT_BITRATE = "64k"
# 每段最长处理时间，防止卡死
_FFMPEG_TIMEOUT = 120

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")

_ffmpeg_path: str | None = None
_ffmpeg_checked = False


def find_ffmpeg() -> str | None:
    """定位可用的 ffmpeg 可执行文件（优先 imageio-ffmpeg 自带的静态构建）。"""
    global _ffmpeg_path, _ffmpeg_checked
    if _ffmpeg_checked:
        return _ffmpeg_path
    _ffmpeg_checked = True

    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).exists():
            _ffmpeg_path = exe
            return _ffmpeg_path
    except Exception:
        pass

    _ffmpeg_path = shutil.which("ffmpeg")
    return _ffmpeg_path


def _subprocess_kwargs() -> dict:
    """Windows 下避免弹出控制台窗口。"""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


async def _run_ffmpeg(cmd: list[str]) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        **_subprocess_kwargs(),
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_FFMPEG_TIMEOUT)
    except asyncio.TimeoutError:
        with contextlib.suppress(Exception):
            proc.kill()
        return -1, "ffmpeg timeout"
    return proc.returncode or 0, (stderr or b"").decode("utf-8", "ignore")


async def probe_duration(path: Path) -> float | None:
    """读取音频时长（秒），失败返回 None。"""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return None
    _, stderr = await _run_ffmpeg([ffmpeg, "-hide_banner", "-i", str(path)])
    match = _DURATION_RE.search(stderr)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


async def split_for_voice(
    path: Path,
    out_dir: Path,
    max_seconds: float = VOICE_MAX_SECONDS,
) -> list[Path]:
    """把音频切成不超过 max_seconds 的若干段。

    Returns:
        切好的音频路径列表；若无需切段（时长未知或未超限）则返回空列表。
    """
    duration = await probe_duration(path)
    if not duration:
        logger.warning(f"无法读取音频时长，跳过切片：{path.name}")
        return []
    if duration <= max_seconds:
        return []

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        logger.warning("未找到 ffmpeg，无法切片语音")
        return []

    parts_count = math.ceil(duration / max_seconds)
    out_dir.mkdir(parents=True, exist_ok=True)
    parts: list[Path] = []

    for index in range(parts_count):
        start = index * max_seconds
        out_path = out_dir / f"{path.stem}_p{index + 1}.mp3"
        code, err = await _run_ffmpeg(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                f"{start:.2f}",
                "-t",
                f"{max_seconds:.2f}",
                "-i",
                str(path),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "44100",
                "-b:a",
                _SPLIT_BITRATE,
                str(out_path),
            ]
        )
        if code != 0 or not out_path.exists() or out_path.stat().st_size == 0:
            logger.error(f"第 {index + 1}/{parts_count} 段切片失败：{err.strip()[:200]}")
            return []
        parts.append(out_path)

    logger.debug(f"音频已切成 {len(parts)} 段（总时长 {duration:.1f}s）")
    return parts
