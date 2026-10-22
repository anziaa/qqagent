"""端到端自检：会员音源 搜索 -> 取直链 -> 下载 -> 切片

用法（必须带 ffmpeg 的 PATH）：
  env -u PYTHONPATH PATH="<AstrBot>/bin:$PATH" python tools/test_vip_chain.py 晴天
"""

import asyncio
import os
import sys
from pathlib import Path


def _astrbot_home() -> Path:
    """AstrBot 根目录：环境变量 > 跟着本文件走。

    两个落脚点：仓库 <repo>/scripts/tools/、部署 <AstrBot>/tools/。
    """
    if os.environ.get("ASTRBOT_HOME"):
        return Path(os.environ["ASTRBOT_HOME"])
    here = Path(__file__).resolve()
    if (here.parents[1] / "data").is_dir():      # <AstrBot>/tools/x.py
        return here.parents[1]
    return here.parents[2] / "runtime" / "astrbot"


HOME = _astrbot_home()
PLUGIN = HOME / "data" / "plugins" / "astrbot_plugin_music"
sys.path.insert(0, str(PLUGIN))

from core.audio import probe_duration, split_for_voice  # noqa: E402
from core.downloader import Downloader  # noqa: E402
from core.platform.qqmusic_vip import QQMusicVipMusic  # noqa: E402


class StubCfg:
    http_proxy = None
    songs_dir = HOME / "data" / "temp" / "astrbot_plugin_music" / "songs"


async def main(keyword: str) -> None:
    player = QQMusicVipMusic(StubCfg())
    songs = await player.fetch_songs(keyword, 1)
    if not songs:
        print("搜索无结果")
        return
    song = await player.fetch_extra(songs[0])
    print(f"歌曲: {song.name} - {song.artists}")
    print(f"直链: {(song.audio_url or '(空)')[:110]}")

    dl = Downloader(StubCfg())
    path = await dl.download_song(song.audio_url) if song.audio_url else None
    if not path:
        print("下载失败")
        return
    print(f"下载: {round(path.stat().st_size / 1024 / 1024, 2)} MB | 时长 {round(await probe_duration(path) or 0, 2)}s")

    parts = await split_for_voice(path, StubCfg.songs_dir / "parts")
    print(f"切片: {len(parts)} 段 -> {[round(await probe_duration(p) or 0, 1) for p in parts]}")

    await dl.close()
    await player.close()


asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "晴天"))
