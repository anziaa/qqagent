"""QQ 音乐扫码登录，把会员登录态存成本地凭据。

用法：
    python qqmusic_login.py

运行后会生成 qq_login_qr.png，用手机 QQ 音乐 App 扫码确认，
凭据保存到 AstrBot 的 data/qqmusic_cred.json。

说明：这是第三方库调用 QQ 音乐接口，非官方用法，账号存在被风控的可能，
建议不要用主号，或控制调用频率。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from qqmusic_api import Client
from qqmusic_api.models.login import QRCodeLoginEvents, QRLoginType

TOOLS_DIR = Path(__file__).resolve().parent


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
QR_PATH = TOOLS_DIR / "qq_login_qr.png"
CRED_PATH = HOME / "data" / "qqmusic_cred.json"
STATUS_LOG = TOOLS_DIR / "login_status.log"

POLL_INTERVAL = 2
TIMEOUT_SECONDS = 180


def log(msg: str) -> None:
    line = f"{msg}"
    print(line, flush=True)
    with STATUS_LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


async def main() -> int:
    STATUS_LOG.write_text("", encoding="utf-8")
    async with Client() as client:
        qr = await client.login.get_qrcode(QRLoginType.QQ)
        QR_PATH.write_bytes(qr.data)
        log(f"QR_SAVED {QR_PATH} mime={qr.mimetype}")

        waited = 0
        last_event = None
        error_streak = 0
        while waited < TIMEOUT_SECONDS:
            try:
                result = await client.login.check_qrcode(qr)
                error_streak = 0
            except Exception as e:  # 单次轮询失败不应中断整个登录
                error_streak += 1
                log(f"POLL_ERROR {type(e).__name__}: {e}")
                if error_streak >= 5:
                    log("FAILED 连续 5 次轮询失败")
                    return 5
                await asyncio.sleep(POLL_INTERVAL)
                waited += POLL_INTERVAL
                continue
            if result.event != last_event:
                last_event = result.event
                log(f"EVENT {result.event.name}")
            if result.event == QRCodeLoginEvents.DONE:
                cred = result.credential
                if cred is None:
                    log("ERROR 登录成功但凭据为空")
                    return 2
                CRED_PATH.parent.mkdir(parents=True, exist_ok=True)
                CRED_PATH.write_text(
                    json.dumps(cred.model_dump(), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                log(f"LOGIN_OK musicid={cred.musicid} -> {CRED_PATH}")
                return 0
            if result.event in (
                QRCodeLoginEvents.TIMEOUT,
                QRCodeLoginEvents.REFUSE,
            ):
                log(f"FAILED {result.event.name}")
                return 3
            await asyncio.sleep(POLL_INTERVAL)
            waited += POLL_INTERVAL

        log("FAILED 超时（二维码已过期，重新运行即可）")
        return 4


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
