import os
import shutil
import sqlite3
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
DB = str(HOME / "data" / "data_v4.db")
MD = str(HOME / "personas" / "阿鱼_v2.md")

NEW = """你的实际能力，说实话：
能做的：看对方发来的文字和图片、说话、想起你们以前聊过的事。
还有一件：你能改自己的名字和签名。
做不到的，一件也别答应：主动发消息给他（他不说话，你就一点动静都没有）、
拍照、发照片、发语音、打电话、翻他手机、看他电脑。
被问起就照实说，语气平常，像说"我手机没电了"那样，不道歉也不卖惨。
对方说要给你装个功能、开个口子，你说"那你弄吧，弄好了我试试"，
别说"好呀我会了"——还没装就是还没有。

名字和签名：
名字是你自己的，想换就换，不用等他点头。
他要是问"你想改名吗"，你真觉得该换，就换成你想叫的，然后顺口告诉他。
别拿改名当表演，一次对话改一次就够了。
签名也一样，想到一句就换上，一句就够。

"""


def patch(text: str) -> str | None:
    start = text.find("你的实际能力，说实话：")
    end = text.find("节奏：")
    if start == -1 or end == -1 or end < start:
        return None
    return text[:start] + NEW + text[end:]


shutil.copy(DB, DB + ".bak_persona")

con = sqlite3.connect(DB)
cur = con.cursor()
cur.execute("select system_prompt from personas where persona_id='阿鱼'")
old = cur.fetchone()[0]
new = patch(old)
if new is None:
    raise SystemExit("DB 里找不到锚点，未修改")
cur.execute(
    "update personas set system_prompt=? where persona_id='阿鱼'", (new,)
)
con.commit()
cur.execute("select length(system_prompt) from personas where persona_id='阿鱼'")
print("DB 人格长度:", old.__len__(), "->", cur.fetchone()[0])
con.close()

with open(MD, encoding="utf-8") as f:
    md = f.read()
md_new = patch(md)
if md_new is None:
    print("MD 未找到锚点，跳过")
else:
    shutil.copy(MD, MD + ".bak")
    with open(MD, "w", encoding="utf-8") as f:
        f.write(md_new)
    print("MD 已更新:", len(md), "->", len(md_new))
