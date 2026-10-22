"""QQ 音乐（会员）音源。

用本地保存的会员登录凭据换取 vkey 直链，能拿到 TXQQ 聚合站拿不到的原版曲目。

凭据由 `scripts/tools/qqmusic_login.py` 扫码生成，保存在
`<AstrBot>/data/qqmusic_cred.json`。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import ClassVar

import aiohttp
from astrbot.api import logger

from ..config import PluginConfig
from ..model import Platform, Song
from .base import BaseMusicPlayer

try:
    from qqmusic_api import Client, Credential
    from qqmusic_api.modules.song import SongFileInfo, SongFileType

    _LIB_OK = True
except Exception:  # pragma: no cover - 依赖缺失时插件仍应能加载
    Client = None  # type: ignore[assignment]
    Credential = None  # type: ignore[assignment]
    SongFileInfo = None  # type: ignore[assignment]
    SongFileType = None  # type: ignore[assignment]
    _LIB_OK = False

# purl 是相对路径，需要拼 CDN 前缀。两台主机路径完全相同，但可用性/速度随网络环境翻转
# （实测同一条直链、同一个 vkey）：
#   直连：isure 200 / 8.2 MB/s，dl 200 / 0.2 MB/s
#   挂 VPN：isure 403（出口 IP 不在大陆），dl 200 / 0.2 MB/s
# 所以先探 isure，不通再退回 dl。
STREAM_BASES = (
    "https://isure.stream.qqmusic.qq.com/",
    "https://dl.stream.qqmusic.qq.com/",
)
# 主机选择结果缓存时长：VPN 开关状态不会秒变，不必每首歌都探
_BASE_CACHE_TTL = 120
COVER_TMPL = "https://y.qq.com/music/photo_new/T002R300x300M000{}.jpg"

# 局域网抖动会导致偶发 TimeoutNetworkError，取链接做几次重试
_RETRY = 3
_RETRY_DELAY = 1.5


class QQMusicVipMusic(BaseMusicPlayer):
    """QQ 音乐（会员登录态）"""

    platform: ClassVar[Platform] = Platform(
        name="qqvip",
        display_name="QQ音乐(会员)",
        keywords=["绿钻点歌", "Q音点歌", "会员点歌"],
    )

    def __init__(self, config: PluginConfig):
        super().__init__(config)
        # .../data/plugins/astrbot_plugin_music/core/platform/x.py -> .../data
        self._cred_path = Path(__file__).resolve().parents[4] / "qqmusic_cred.json"
        self._cred = None
        self._client = None
        self._media_mids: dict[str, str] = {}
        self._base_cache: tuple[float, str] | None = None
        if not _LIB_OK:
            logger.warning("qqmusic-api-python 未安装，QQ音乐(会员) 音源不可用")
        else:
            self._init_client()

    # ---------- 凭据 ----------

    def _init_client(self) -> None:
        try:
            data = json.loads(self._cred_path.read_text(encoding="utf-8"))
            self._cred = Credential(**data)
        except FileNotFoundError:
            logger.warning(f"未找到 QQ 音乐凭据 {self._cred_path}，音源不可用")
            return
        except Exception as e:
            logger.error(f"读取 QQ 音乐凭据失败: {e}")
            return

        if not self._cred.is_valid():
            logger.warning("QQ 音乐凭据无效（musicid/musickey 缺失），需要重新扫码登录")
            self._cred = None
            return

        self._client = Client(self._cred)

    def _save_cred(self) -> None:
        if not self._cred:
            return
        try:
            self._cred_path.write_text(
                json.dumps(self._cred.model_dump(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"保存 QQ 音乐凭据失败: {e}")

    async def _ensure_cred(self) -> bool:
        """尽量保持凭据可用，必要时尝试刷新。"""
        if not self._client or not self._cred:
            return False
        try:
            if await self._client.login.check_expired(self._cred):
                logger.info("QQ 音乐凭据已过期，尝试刷新")
                self._cred = await self._client.login.refresh_credential(self._cred)
                self._save_cred()
        except Exception as e:
            logger.warning(f"QQ 音乐凭据刷新失败: {e}")
        return True

    # ---------- 内部请求 ----------

    async def _call(self, factory, what: str):
        """带重试地执行一次异步请求。"""
        last_error = None
        for attempt in range(_RETRY):
            try:
                return await factory()
            except Exception as e:
                last_error = e
                if attempt < _RETRY - 1:
                    await asyncio.sleep(_RETRY_DELAY)
        logger.error(f"QQ音乐(会员) {what} 失败: {type(last_error).__name__}: {last_error}")
        return None

    @staticmethod
    def _artist_text(song) -> str:
        singers = getattr(song, "singer", None) or []
        names = [getattr(s, "name", "") for s in singers if getattr(s, "name", "")]
        return "/".join(names)

    # ---------- 基类接口 ----------

    async def fetch_songs(
        self, keyword: str, limit: int = 5, extra: str | None = None
    ) -> list[Song]:
        if not await self._ensure_cred():
            return []

        resp = await self._call(
            lambda: self._client.search.search_by_type(keyword, num=limit),
            "搜索",
        )
        if resp is None:
            return []

        raw = getattr(resp, "song", None) or []
        songs: list[Song] = []
        for item in raw[:limit]:
            mid = getattr(item, "mid", None)
            if not mid:
                continue
            file_info = getattr(item, "file", None)
            media_mid = getattr(file_info, "media_mid", "") or mid
            self._media_mids[mid] = media_mid

            album = getattr(item, "album", None)
            album_mid = getattr(album, "mid", "") if album else ""
            interval = getattr(item, "interval", 0) or 0

            songs.append(
                Song(
                    id=mid,
                    source="qqvip",
                    name=getattr(item, "name", None),
                    artists=self._artist_text(item),
                    duration=interval * 1000 if interval else None,
                    cover_url=COVER_TMPL.format(album_mid) if album_mid else None,
                    note="QQ音乐会员",
                )
            )
        return songs

    async def _pick_stream_base(self, purl: str) -> str:
        """选一台能用的 CDN 主机（见 STREAM_BASES 注释）"""
        now = time.time()
        if self._base_cache and self._base_cache[0] > now:
            return self._base_cache[1]

        base = STREAM_BASES[1]
        try:
            async with self.session.get(
                STREAM_BASES[0] + purl,
                headers={"Range": "bytes=0-0"},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status in (200, 206):
                    base = STREAM_BASES[0]
                else:
                    logger.debug(f"isure 主机不可用（HTTP {resp.status}），改用 dl 主机")
        except Exception as e:
            logger.debug(f"isure 主机探测失败（{type(e).__name__}），改用 dl 主机")

        self._base_cache = (now + _BASE_CACHE_TTL, base)
        return base

    async def fetch_extra(self, song: Song) -> Song:
        """补齐直链与封面。"""
        if not await self._ensure_cred():
            return song

        if not song.audio_url:
            media_mid = self._media_mids.get(song.id) or song.id
            resp = await self._call(
                lambda: self._client.song.get_song_urls(
                    file_info=[SongFileInfo(mid=song.id, media_mid=media_mid)],
                    file_type=SongFileType.MP3_320,
                ),
                f"获取直链({song.id})",
            )
            info = resp.data[0] if resp is not None and resp.data else None
            if info is not None and info.purl:
                song.audio_url = await self._pick_stream_base(info.purl) + info.purl
            elif info is not None:
                # 常见于需要单独购买数字专辑的曲目：只有试听片段或直接无权限
                logger.warning(
                    f"【{song.name}】无完整链接（result={info.result}），"
                    "可能需单独购买数字专辑"
                )

        return song

    async def fetch_lyrics(self, song: Song) -> Song:
        if song.lyrics or not await self._ensure_cred():
            return song
        resp = await self._call(
            lambda: self._client.lyric.get_lyric(song.id), f"获取歌词({song.id})"
        )
        lyric = getattr(resp, "lyric", None) if resp is not None else None
        if isinstance(lyric, str) and lyric.strip():
            song.lyrics = lyric
        return song

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                pass
        await super().close()
