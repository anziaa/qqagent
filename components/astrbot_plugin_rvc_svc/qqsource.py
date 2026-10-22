"""QQ 音乐会员音源：搜索 / 取直链 / 下载（供 sing_song 工具使用）。

为什么不用插件自带的网易云链路：
`wyy.xhily.com`、`163api.qijieya.cn`、`api.vkeys.cn`、`biliplayer` 这几个公共
下载源已经全部失效（404 / 502 / 空 data），RVC 后端拿不到音频就没法唱。
而 QQ 音乐会员凭据链路是通的（`scripts/tools/qqmusic_login.py` 扫码生成，
保存在 `<AstrBot>/data/qqmusic_cred.json`），所以这里直接复用。

CDN 主机选择、DoH 绕 fake-ip 的做法与 `astrbot_plugin_music/core/net.py` 一致：
isure 快但 bound 大陆出口 IP，挂代理会 403；dl 慢但稳。先探再定。
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import socket
import time
import urllib.request
from pathlib import Path

import aiohttp
from astrbot.api import logger

try:
    from qqmusic_api import Client, Credential
    from qqmusic_api.modules.song import SongFileInfo, SongFileType

    _LIB_OK = True
except Exception:  # pragma: no cover
    Client = None  # type: ignore[assignment]
    Credential = None  # type: ignore[assignment]
    SongFileInfo = None  # type: ignore[assignment]
    SongFileType = None  # type: ignore[assignment]
    _LIB_OK = False

# 插件装在 <AstrBot>/data/plugins/<插件名>/ 下，往上两级就是 data/
# （别写死盘符：这个仓库的 AstrBot 是跟着仓库走的，见 runtime/）
CRED_PATH = Path(
    os.environ.get("QQMUSIC_CRED")
    or Path(__file__).resolve().parents[2] / "qqmusic_cred.json"
)
STREAM_BASES = (
    "https://isure.stream.qqmusic.qq.com/",
    "https://dl.stream.qqmusic.qq.com/",
)
_BASE_CACHE_TTL = 120
_RETRY = 3
_RETRY_DELAY = 1.2

DOH_SUFFIXES = (".qqmusic.qq.com", ".tcdn.qq.com", ".dcloudstc.com", ".apdcdn.com")
DOH_ENDPOINT = "https://223.5.5.5/resolve?name={name}&type=A"
_FAKE_NETS = (
    ipaddress.ip_network("198.18.0.0/16"),
    ipaddress.ip_network("198.19.0.0/16"),
)


def _is_fake_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in _FAKE_NETS)


class _DoHResolver(aiohttp.abc.AbstractResolver):
    """系统解析到 fake-ip 时，改用 DoH 拿大陆 CDN 的真实 IP"""

    def __init__(self) -> None:
        self._fallback = aiohttp.resolver.DefaultResolver()
        self._cache: dict[str, tuple[float, str]] = {}

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET):
        addrs = await self._fallback.resolve(host, port, family)
        if host.endswith(DOH_SUFFIXES) and any(
            _is_fake_ip(a.get("host", "")) for a in addrs
        ):
            ip = await self._doh(host)
            if ip:
                return [
                    {
                        "hostname": host,
                        "host": ip,
                        "port": port,
                        "family": socket.AF_INET,
                        "proto": 0,
                        "flags": 0,
                    }
                ]
        return addrs

    async def close(self) -> None:
        await self._fallback.close()

    async def _doh(self, host: str) -> str | None:
        hit = self._cache.get(host)
        if hit and hit[0] > time.time():
            return hit[1]
        ip = await asyncio.to_thread(self._doh_sync, host)
        if ip:
            self._cache[host] = (time.time() + 300, ip)
        return ip

    @staticmethod
    def _doh_sync(host: str) -> str | None:
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(DOH_ENDPOINT.format(name=host), timeout=5) as resp:
                data = json.loads(resp.read().decode())
            ips = [a["data"] for a in data.get("Answer", []) if a.get("type") == 1]
            if ips:
                return ips[-1]
        except Exception as e:
            logger.warning(f"DoH 解析 {host} 失败，回退系统 DNS：{e}")
        return None


class QQSource:
    """QQ 音乐会员音源（带凭据自动刷新）"""

    def __init__(self) -> None:
        self._client = None
        self._cred = None
        self._session: aiohttp.ClientSession | None = None
        self._base_cache: tuple[float, str] | None = None
        self._ready = False

    # ---------- 初始化 ----------

    def _init_client(self) -> bool:
        if not _LIB_OK:
            logger.warning("qqmusic-api-python 未安装，翻唱取源不可用")
            return False
        try:
            data = json.loads(CRED_PATH.read_text(encoding="utf-8"))
            self._cred = Credential(**data)
        except FileNotFoundError:
            logger.warning(f"未找到 QQ 音乐凭据 {CRED_PATH}")
            return False
        except Exception as e:
            logger.error(f"读取 QQ 音乐凭据失败: {e}")
            return False
        if not self._cred.is_valid():
            logger.warning("QQ 音乐凭据无效，需要重新扫码登录")
            return False
        self._client = Client(self._cred)
        return True

    async def _ensure(self) -> bool:
        if not self._ready:
            self._ready = self._init_client()
        if not self._ready or self._client is None:
            return False
        try:
            if await self._client.login.check_expired(self._cred):
                logger.info("QQ 音乐凭据已过期，尝试刷新")
                self._cred = await self._client.login.refresh_credential(self._cred)
                CRED_PATH.write_text(
                    json.dumps(self._cred.model_dump(), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
        except Exception as e:
            logger.warning(f"QQ 音乐凭据刷新失败: {e}")
        return True

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=180, connect=10, sock_read=60),
                connector=aiohttp.TCPConnector(resolver=_DoHResolver(), limit=16),
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                pass

    async def _call(self, factory, what: str):
        last = None
        for attempt in range(_RETRY):
            try:
                return await factory()
            except Exception as e:
                last = e
                if attempt < _RETRY - 1:
                    await asyncio.sleep(_RETRY_DELAY)
        logger.error(f"QQ音乐 {what} 失败: {type(last).__name__}: {last}")
        return None

    @staticmethod
    def _artists(item) -> str:
        singers = getattr(item, "singer", None) or []
        return "/".join(getattr(s, "name", "") for s in singers if getattr(s, "name", ""))

    # ---------- 对外接口 ----------

    async def search(self, keyword: str, limit: int = 8) -> list[dict]:
        """按歌名搜索，返回 [{id, name, artists, media_mid, interval}]"""
        if not await self._ensure():
            return []
        resp = await self._call(
            lambda: self._client.search.search_by_type(keyword, num=limit), "搜索"
        )
        if resp is None:
            return []
        out: list[dict] = []
        for item in (getattr(resp, "song", None) or [])[:limit]:
            mid = getattr(item, "mid", None)
            if not mid:
                continue
            file_info = getattr(item, "file", None)
            out.append(
                {
                    "id": mid,
                    "name": getattr(item, "name", "") or "",
                    "artists": self._artists(item),
                    "media_mid": getattr(file_info, "media_mid", "") or mid,
                    "interval": getattr(item, "interval", 0) or 0,
                }
            )
        return out

    async def _pick_base(self, purl: str) -> str:
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
        except Exception:
            pass
        self._base_cache = (now + _BASE_CACHE_TTL, base)
        logger.debug(f"翻唱取源使用 CDN: {base}")
        return base

    async def audio_url(self, song: dict) -> str | None:
        if not await self._ensure():
            return None
        resp = await self._call(
            lambda: self._client.song.get_song_urls(
                file_info=[
                    SongFileInfo(mid=song["id"], media_mid=song.get("media_mid") or song["id"])
                ],
                file_type=SongFileType.MP3_320,
            ),
            f"获取直链({song['id']})",
        )
        info = resp.data[0] if resp is not None and resp.data else None
        if info is None or not info.purl:
            logger.warning(f"【{song.get('name')}】取不到完整直链（可能需要单独购买）")
            return None
        return await self._pick_base(info.purl) + info.purl

    async def lyrics(self, song_id: str) -> str:
        if not await self._ensure():
            return ""
        resp = await self._call(lambda: self._client.lyric.get_lyric(song_id), "取歌词")
        lyric = getattr(resp, "lyric", None) if resp is not None else None
        return lyric if isinstance(lyric, str) else ""

    async def download(self, url: str, dest: Path) -> Path | None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(_RETRY):
            try:
                async with self.session.get(url) as resp:
                    if resp.status != 200:
                        logger.warning(f"下载 HTTP {resp.status}（第 {attempt + 1} 次）")
                        await asyncio.sleep(_RETRY_DELAY)
                        continue
                    buf = bytearray()
                    async for chunk in resp.content.iter_chunked(1 << 16):
                        buf.extend(chunk)
                if len(buf) < 100 * 1024:
                    logger.warning(f"下载内容过小（{len(buf)} 字节），重试")
                    await asyncio.sleep(_RETRY_DELAY)
                    continue
                await asyncio.to_thread(dest.write_bytes, bytes(buf))
                logger.info(f"已下载源曲: {dest.name} ({len(buf) / 1048576:.1f} MB)")
                return dest
            except Exception as e:
                logger.warning(f"下载失败（第 {attempt + 1} 次）: {type(e).__name__}: {e}")
                await asyncio.sleep(_RETRY_DELAY)
        return None
