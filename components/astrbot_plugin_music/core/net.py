"""网络辅助：为 QQ 音乐 CDN 绕开系统的 fake-ip 劫持。

背景：这台机器上装了 Clash 类工具，开启 TUN + fake-ip 模式后
`dl.stream.qqmusic.qq.com` 会被解析成 198.18.0.x 的假地址，流量被丢到海外节点：
- 单连接只有约 0.2 MB/s，一首 10 MB 的歌要 53 秒
- 命中不同节点时直接连接超时
- 同一首曲目换成 `isure.stream` 主机还会 403（vkey 绑定大陆出口 IP）

实测用 DoH 拿到的大陆 CDN 真实 IP 直连，多路并行可到约 1 MB/s 且稳定。
所以这里给下载器挂一个自定义 resolver：只有 QQ 音乐相关域名走 DoH，其余保持系统解析。
"""

import asyncio
import ipaddress
import json
import socket
import time
import urllib.request

import aiohttp
from astrbot.api import logger

# 命中的域名后缀：只有这些域名在「系统解析拿到 fake-ip」时才改走 DoH
DOH_SUFFIXES = (".qqmusic.qq.com", ".tcdn.qq.com", ".dcloudstc.com", ".apdcdn.com")
DOH_ENDPOINT = "https://223.5.5.5/resolve?name={name}&type=A"
CACHE_TTL = 300  # 秒
# Clash 等工具的 fake-ip 默认段
FAKE_NETS = (
    ipaddress.ip_network("198.18.0.0/16"),
    ipaddress.ip_network("198.19.0.0/16"),
)


def _is_fake_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in FAKE_NETS)


class QQMusicDoHResolver(aiohttp.abc.AbstractResolver):
    """系统 DNS 正常就跟着系统走；解析到 fake-ip 时才用 DoH 拿真实 IP"""

    def __init__(self) -> None:
        self._fallback = aiohttp.resolver.DefaultResolver()
        self._cache: dict[str, tuple[float, str]] = {}

    async def resolve(
        self, host: str, port: int = 0, family: int = socket.AF_INET
    ) -> list[dict]:
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
            self._cache[host] = (time.time() + CACHE_TTL, ip)
        return ip

    @staticmethod
    def _doh_sync(host: str) -> str | None:
        try:
            # 显式禁用代理：DoH 走系统直连才能拿到大陆节点
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({})
            )
            with opener.open(DOH_ENDPOINT.format(name=host), timeout=5) as resp:
                data = json.loads(resp.read().decode())
            ips = [
                a["data"] for a in data.get("Answer", []) if a.get("type") == 1
            ]
            if ips:
                return ips[-1]
        except Exception as e:
            logger.warning(f"DoH 解析 {host} 失败，回退系统 DNS：{e}")
        return None
