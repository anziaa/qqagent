import asyncio
import os
import re
import uuid
from pathlib import Path

import aiofiles
import aiohttp

from astrbot.api import logger

from .config import PluginConfig
from .net import QQMusicDoHResolver

# QQ 音乐 CDN（dl.stream.qqmusic.qq.com）对单条连接限速，实测约 0.2 MB/s，
# 一首 10 MB 的歌要 53 秒；拆成多路 Range 请求并行能到 1 MB/s 以上。
# 小文件不值得拆，直接单连接流式下载。
CHUNK_BYTES = 2 * 1024 * 1024
MAX_PARALLEL = 6
_RETRY = 3
_RETRY_DELAY = 1.0
# 单请求超时：连接 10s、读 30s，避免 CDN 边缘节点卡住拖垮整次下载
_TIMEOUT = aiohttp.ClientTimeout(total=90, connect=10, sock_read=30)


class Downloader:
    """下载器"""

    def __init__(self, config: PluginConfig):
        self.cfg = config
        self.songs_dir = self.cfg.songs_dir
        self.session = aiohttp.ClientSession(
            proxy=self.cfg.http_proxy,
            timeout=_TIMEOUT,
            connector=aiohttp.TCPConnector(
                resolver=QQMusicDoHResolver(),
                limit=16,
                ttl_dns_cache=0,
            ),
        )

    async def close(self):
        await self.session.close()

    async def download_image(self, url: str, close_ssl: bool = False) -> bytes | None:
        """下载图片"""
        url = url.replace("https://", "http://") if close_ssl else url
        try:
            async with self.session.get(url) as response:
                img_bytes = await response.read()
                return img_bytes
        except Exception as e:
            logger.error(f"图片下载失败: {e}")

    @staticmethod
    def _cache_name(cache_key: str) -> str:
        """把缓存键洗成合法文件名（歌曲名里常有斜杠、问号）。"""
        safe = re.sub(r"[^\w.-]", "_", cache_key or "").strip("._")
        return safe or uuid.uuid4().hex

    def _trim_cache(self) -> None:
        """按最近访问时间保留最近 N 首，超出部分删掉。

        cache_max_songs <= 0 视为不限制，避免配置写错把缓存清空。
        """
        max_items = getattr(self.cfg, "cache_max_songs", 0) or 0
        if max_items <= 0:
            return
        try:
            files = [f for f in self.songs_dir.glob("*.mp3") if f.is_file()]
            if len(files) <= max_items:
                return
            files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
            for f in files[max_items:]:
                f.unlink(missing_ok=True)
                logger.info(f"[歌曲缓存] 淘汰最久未用：{f.name}")
        except Exception as e:
            logger.warning(f"[歌曲缓存] 清理失败：{e}")

    async def download_song(
        self, url: str, cache_key: str | None = None
    ) -> Path | None:
        """下载歌曲，返回保存路径

        - 支持 Content-Length 且体积较大的走多路 Range 并行
        - 其余情况单连接流式下载
        - 整体重试 3 次（CDN 边缘节点偶发超时）

        cache_key：稳定的歌曲标识（形如 "qq_003jmQh20aRW8r"）。传了就走本地缓存：
        已有文件直接返回、不再下载；下载完按 LRU 只保留最近 N 首。
        不传则沿用原来的随机名（等于不缓存）。
        """
        self.songs_dir.mkdir(parents=True, exist_ok=True)
        if cache_key:
            file_path = self.songs_dir / f"{self._cache_name(cache_key)}.mp3"
            try:
                if file_path.is_file() and file_path.stat().st_size > 100 * 1024:
                    os.utime(file_path, None)  # 刷新访问时间，LRU 才不会淘汰它
                    logger.debug(f"命中本地歌曲缓存：{file_path.name}")
                    return file_path
            except OSError:
                pass
        else:
            file_path = self.songs_dir / f"{uuid.uuid4().hex}.mp3"
        last_err = ""

        for attempt in range(1, _RETRY + 1):
            try:
                async with self.session.get(url) as response:
                    if response.status != 200:
                        last_err = f"HTTP {response.status}"
                        logger.warning(
                            f"歌曲下载失败（第 {attempt}/{_RETRY} 次）：{last_err}"
                        )
                        await asyncio.sleep(_RETRY_DELAY)
                        continue

                    total = int(response.headers.get("Content-Length") or 0)
                    accepts_range = "bytes" in (
                        response.headers.get("Accept-Ranges") or ""
                    ).lower()

                    if total >= CHUNK_BYTES and accepts_range:
                        logger.debug(
                            f"大文件走并行下载：{total / 1024 / 1024:.1f} MB"
                        )
                        # 离开 with 后再并发，避免占着这条连接
                    else:
                        async with aiofiles.open(file_path, "wb") as f:
                            async for chunk in response.content.iter_chunked(64 * 1024):
                                await f.write(chunk)
                        logger.debug(f"歌曲下载完成，保存在：{file_path}")
                        self._trim_cache()
                        return file_path

                if await self._download_ranges(url, total, file_path):
                    logger.debug(f"歌曲下载完成（并行），保存在：{file_path}")
                    self._trim_cache()
                    return file_path

                last_err = "分块下载未完成"
                await asyncio.sleep(_RETRY_DELAY)

            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                logger.warning(f"歌曲下载失败（第 {attempt}/{_RETRY} 次）：{last_err}")
                await asyncio.sleep(_RETRY_DELAY)

        logger.error(f"歌曲下载失败，已重试 {_RETRY} 次：{last_err}")
        return None

    async def _download_ranges(self, url: str, total: int, file_path: Path) -> bool:
        """按 Range 分块并行下载，各块按偏移直接写入同一文件"""
        try:
            with open(file_path, "wb") as f:
                f.truncate(total)
        except OSError as e:
            logger.error(f"预分配文件失败: {e}")
            return False

        parts = (total + CHUNK_BYTES - 1) // CHUNK_BYTES
        semaphore = asyncio.Semaphore(MAX_PARALLEL)

        async def fetch(index: int) -> bool:
            start = index * CHUNK_BYTES
            end = min(start + CHUNK_BYTES, total) - 1
            expected = end - start + 1
            headers = {"Range": f"bytes={start}-{end}"}
            async with semaphore:
                for _ in range(_RETRY):
                    try:
                        async with self.session.get(url, headers=headers) as response:
                            if response.status not in (200, 206):
                                raise RuntimeError(f"HTTP {response.status}")
                            data = await response.read()
                        if len(data) != expected:
                            raise RuntimeError(f"长度不符 {len(data)}/{expected}")
                        async with aiofiles.open(file_path, "r+b") as f:
                            await f.seek(start)
                            await f.write(data)
                        return True
                    except Exception as e:
                        logger.warning(f"第 {index + 1}/{parts} 块下载失败：{e}")
                        await asyncio.sleep(_RETRY_DELAY)
            return False

        results = await asyncio.gather(*(fetch(i) for i in range(parts)))
        if all(results) and file_path.stat().st_size == total:
            return True

        try:
            file_path.unlink(missing_ok=True)
        except OSError:
            pass
        return False
