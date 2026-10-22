from .base import BaseMusicPlayer
from .ncm import NetEaseMusic
from .ncm_nodejs import NetEaseMusicNodeJS
from .qqmusic_vip import QQMusicVipMusic
from .txqq import TXQQMusic

__all__ = [
    "NetEaseMusic",
    "NetEaseMusicNodeJS",
    "BaseMusicPlayer",
    "QQMusicVipMusic",
    "TXQQMusic",
]

