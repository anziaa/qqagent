"""让机器人修改自己 QQ 的昵称、个性签名、头像。

依赖 NapCat 的 set_qq_profile / set_qq_avatar 接口（OneBot v11 的 Go-CQHTTP 扩展）。
"""

import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import Image

WAKE_PREFIXES = ("/", "／", "!", "！", "#")

# 自主改名的冷却时间（秒）：防止模型在一次对话里反复改名
RENAME_COOLDOWN = 600


def _payload(event: AstrMessageEvent, *commands: str) -> str:
    """从 "/改昵称 阿鱼" 里取出 "阿鱼"（兼容不同唤醒前缀）。"""
    text = (event.message_str or "").strip()
    for p in WAKE_PREFIXES:
        if text.startswith(p):
            text = text[len(p):].lstrip()
            break
    for c in sorted(commands, key=len, reverse=True):
        if text.startswith(c):
            text = text[len(c):]
            break
    return text.strip()


class SelfProfilePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.cfg = config
        self._last_rename_ts = 0.0

    @staticmethod
    def _bot(event: AstrMessageEvent):
        bot = getattr(event, "bot", None)
        if bot is None or not hasattr(bot, "call_action"):
            return None
        return bot

    async def _nickname(self, bot) -> str:
        try:
            info = await bot.call_action("get_login_info")
            return (info or {}).get("nickname") or ""
        except Exception as e:
            logger.warning(f"[账号资料] 获取当前昵称失败: {e}")
            return ""

    async def _read_profile(self, bot) -> dict:
        """读取当前登录账号自己的资料。

        NapCat 的不同版本对"读自己资料"暴露的接口不一样，这里做三级降级：
        get_qq_profile → get_stranger_info → 只有 get_login_info。
        """
        profile: dict = {}

        try:
            login = await bot.call_action("get_login_info") or {}
            profile.update(
                user_id=login.get("user_id"),
                nickname=login.get("nickname") or "",
            )
        except Exception as e:
            logger.warning(f"[账号资料] get_login_info 失败: {e}")

        uid = profile.get("user_id")
        if not uid:
            return profile

        # 1) NapCat 扩展接口：字段最全，含个性签名、头像、等级
        for kwargs in ({}, {"user_id": uid}):
            try:
                info = await bot.call_action("get_qq_profile", **kwargs) or {}
                if info:
                    profile["detail"] = info
                    return profile
            except Exception as e:
                logger.debug(f"[账号资料] get_qq_profile{kwargs} 失败: {e}")

        # 2) OneBot v11 标准接口：昵称 / 性别 / 年龄 / 签名
        try:
            info = await bot.call_action("get_stranger_info", user_id=uid) or {}
            profile["detail"] = info
        except Exception as e:
            logger.warning(f"[账号资料] get_stranger_info 失败: {e}")

        return profile

    @staticmethod
    def _fmt_profile(profile: dict) -> str:
        """把资料整理成一段人话，供模型或用户直接读。"""
        detail = profile.get("detail") or {}
        uid = profile.get("user_id") or detail.get("user_id")
        name = profile.get("nickname") or detail.get("nickname") or "（未知）"
        note = (
            detail.get("personal_note")
            or detail.get("sign")
            or detail.get("long_nick")
            or ""
        )

        lines = [f"昵称：{name}", f"QQ号：{uid or '（未知）'}"]
        lines.append(f"个性签名：{note}" if note else "个性签名：（空）")

        extras = []
        if detail.get("sex"):
            extras.append(f"性别 {detail['sex']}")
        if detail.get("age"):
            extras.append(f"年龄 {detail['age']}")
        if detail.get("level"):
            lv = detail["level"]
            lv = lv.get("level") if isinstance(lv, dict) else lv
            extras.append(f"等级 {lv}")
        if detail.get("city"):
            extras.append(f"城市 {detail['city']}")
        if extras:
            lines.append("其他：" + "，".join(extras))
        return "\n".join(lines)

    # ==================== 读对方 / 指定用户的资料 ====================

    # 昵称里常见的"看不见的字符"（空白占位符），单独识别出来说明一下
    _INVISIBLE = set("\u3164\u2800\u00a0\u200b\u200c\u200d\ufeff \t")

    @classmethod
    def _nick_display(cls, name: str) -> str:
        """昵称是空白字符时，直接说清楚，别让模型以为读不到。"""
        s = (name or "").strip()
        if not s:
            return "（没设昵称）"
        if all(ch in cls._INVISIBLE for ch in s):
            return "（昵称是空白字符，对方故意设成了看不见的样子）"
        return s

    @staticmethod
    def _clean(value) -> str:
        """清掉资料里的占位符。"""
        s = str(value or "").strip()
        return "" if s in ("-", "0", "null", "None") else s

    async def _read_peer_profile(self, bot, user_id: int, group_id: str = "") -> dict:
        """读别人的资料。

        NapCat 的 get_stranger_info 字段最全（昵称/签名/等级/生日/地区/VIP/注册时间），
        失败的场合再退到群成员资料（至少还有群名片、入群时间）。
        """
        profile: dict = {}
        try:
            profile = await bot.call_action(
                "get_stranger_info", user_id=int(user_id), no_cache=True
            ) or {}
        except Exception as e:
            logger.warning(f"[账号资料] get_stranger_info({user_id}) 失败: {e}")

        if not profile.get("nickname") and group_id:
            try:
                member = await bot.call_action(
                    "get_group_member_info",
                    group_id=int(group_id), user_id=int(user_id), no_cache=True,
                ) or {}
                profile = {**member, **{k: v for k, v in profile.items() if v}}
            except Exception as e:
                logger.debug(f"[账号资料] get_group_member_info 兜底失败: {e}")

        return profile

    @classmethod
    def _fmt_peer_profile(cls, profile: dict, nick_hint: str = "") -> str:
        """把对方资料整理成一段人话。"""
        nick = (
            profile.get("nickname") or profile.get("nick")
            or profile.get("card") or nick_hint
        )
        uid = profile.get("user_id") or profile.get("uin") or ""

        lines = [f"昵称：{cls._nick_display(nick)}"]
        card = cls._clean(profile.get("card"))
        if card and card != nick:
            lines.append(f"群名片：{card}")
        lines.append(f"QQ号：{uid or '（未知）'}")

        sign = cls._clean(
            profile.get("long_nick") or profile.get("longNick") or profile.get("sign")
        )
        lines.append(f"个性签名：{sign}" if sign else "个性签名：（没写）")

        remark = cls._clean(profile.get("remark"))
        if remark:
            lines.append(f"你给他存的备注：{remark}")

        # 下面这些是对方自己在资料卡上填的，可能乱写
        facts = []
        sex_map = {"male": "男", "female": "女"}
        sex = cls._clean(profile.get("sex"))
        if sex and sex not in ("unknown", "未知"):
            facts.append(f"性别 {sex_map.get(sex, sex)}")
        if (age := cls._clean(profile.get("age"))):
            facts.append(f"年龄 {age}")
        by, bm, bd = (
            profile.get("birthday_year"), profile.get("birthday_month"), profile.get("birthday_day")
        )
        if by and bm and bd:
            facts.append(f"生日 {by}-{int(bm):02d}-{int(bd):02d}")
        region = [cls._clean(profile.get(k)) for k in ("country", "province", "city")]
        region = [r for r in region if r]
        if region:
            facts.append("地区 " + " ".join(dict.fromkeys(region)))
        # QQ 等级和群等级是两回事，别混着叫
        if (lv := cls._clean(profile.get("qqLevel"))):
            facts.append(f"QQ等级 {lv}")
        elif (lv := cls._clean(profile.get("level"))):
            facts.append(f"群等级 {lv}")
        if profile.get("is_vip"):
            facts.append(f"会员 SVIP{profile.get('vip_level') or ''}".replace("SVIP0", "SVIP"))
        rt = profile.get("reg_time") or profile.get("regTime")
        if isinstance(rt, (int, float)) and rt > 0:
            import datetime as _dt
            d = _dt.datetime.fromtimestamp(rt)
            days = (_dt.datetime.now() - d).days
            facts.append(f"QQ注册于 {d.year} 年（约 {days // 365} 年）")
        if facts:
            lines.append("资料卡：" + "｜".join(facts))
            lines.append("（以上都是他自己填的，可能是假的）")
        return "\n".join(lines)

    @filter.llm_tool(name="read_user_profile")
    async def tool_read_user_profile(self, event: AstrMessageEvent, user_id: str = "") -> str:
        """查看**对方**（正在跟你聊天的那个人）的 QQ 资料：昵称、个性签名、QQ 等级、
        性别、年龄、生日、地区、注册年份、会员状态。

        什么时候用：对方问"你知道我是谁吗""我资料上写的什么""你记得我叫什么"，
        你想主动了解对方，或者想拿对方的签名、地区、等级来接话时。
        只读操作，不改任何东西。不填 user_id 就是当前正在跟你说话的人。
        资料是对方自己填的，可能乱写（年龄、地区尤其），照实转述即可，别当成事实去推断。

        不要凭记忆猜对方的昵称、年龄、地区——需要这些信息就调这个工具。

        Args:
            user_id (string): 要查的 QQ 号，纯数字。留空表示当前对话对象
        """
        bot = self._bot(event)
        if bot is None:
            return "当前平台读不到资料（需要 NapCat / aiocqhttp）。"

        target = user_id.strip() if user_id and user_id.strip().isdigit() else ""
        if not target:
            target = str(event.get_sender_id() or "")
        if not target.isdigit():
            return "这一轮拿不到对方的 QQ 号，读不了资料。"

        # 查到自己身上就转去读自己的资料
        try:
            me = await bot.call_action("get_login_info") or {}
            if str(me.get("user_id")) == target:
                return "这是我自己，要看我的资料得用 read_my_profile。"
        except Exception:
            pass

        profile = await self._read_peer_profile(
            bot, int(target), str(event.get_group_id() or "")
        )
        if not profile:
            return f"查不到 QQ {target} 的资料（可能是设置了不公开，或者已经不是好友）。"

        return self._fmt_peer_profile(profile, nick_hint=str(event.get_sender_name() or ""))

    @filter.command("ta资料", alias={"对方资料", "你的资料", "查资料"})
    async def show_peer_profile(self, event: AstrMessageEvent):
        """查看对方（或 @ 的人 / 指定 QQ 号）的资料卡"""
        bot = self._bot(event)
        if bot is None:
            await event.send(event.plain_result("当前平台读不到资料（需要 NapCat / aiocqhttp）"))
            event.stop_event()
            return

        target = _payload(event, "ta资料", "对方资料", "你的资料", "查资料")
        uid = target if target.isdigit() else ""

        if not uid:
            for comp in getattr(event.message_obj, "message", []) or []:
                if comp.__class__.__name__ == "At":
                    qq = getattr(comp, "qq", "")
                    if str(qq).isdigit() and str(qq) != str(event.get_self_id() or ""):
                        uid = str(qq)
                        break

        if not uid:
            uid = str(event.get_sender_id() or "")

        profile = await self._read_peer_profile(
            bot, int(uid), str(event.get_group_id() or "")
        )
        if not profile:
            await event.send(event.plain_result(f"查不到 QQ {uid} 的资料。"))
            event.stop_event()
            return

        await event.send(event.plain_result(self._fmt_peer_profile(profile)))
        event.stop_event()

    @filter.command("我的资料", alias={"看资料", "我的昵称"})
    async def show_profile(self, event: AstrMessageEvent):
        """查看我自己现在的 QQ 昵称、签名等资料"""
        bot = self._bot(event)
        if bot is None:
            await event.send(event.plain_result("当前平台不支持读资料（需要 NapCat / aiocqhttp）"))
            event.stop_event()
            return

        profile = await self._read_profile(bot)
        await event.send(event.plain_result(self._fmt_profile(profile)))
        event.stop_event()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("改昵称")
    async def set_nickname(self, event: AstrMessageEvent):
        """改昵称 <新名字>"""
        name = _payload(event, "改昵称")
        if not name:
            await event.send(event.plain_result("用法：/改昵称 新名字"))
            event.stop_event()
            return

        bot = self._bot(event)
        if bot is None:
            await event.send(event.plain_result("当前平台不支持改资料（需要 NapCat / aiocqhttp）"))
            event.stop_event()
            return

        try:
            await bot.call_action("set_qq_profile", nickname=name)
        except Exception as e:
            logger.error(f"[账号资料] 改昵称失败: {e}")
            await event.send(event.plain_result(f"改昵称失败：{e}"))
            event.stop_event()
            return

        await event.send(event.plain_result(f"改好了，现在叫「{name}」"))
        event.stop_event()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("改签名", alias={"改个签", "改个性签名"})
    async def set_signature(self, event: AstrMessageEvent):
        """改签名 <一句话>"""
        note = _payload(event, "改签名", "改个性签名", "改个签")
        if not note:
            await event.send(event.plain_result("用法：/改签名 想写的一句话"))
            event.stop_event()
            return

        bot = self._bot(event)
        if bot is None:
            await event.send(event.plain_result("当前平台不支持改资料（需要 NapCat / aiocqhttp）"))
            event.stop_event()
            return

        # NapCat 的 set_qq_profile 会把 nickname 一起写回，不给就会把昵称清空，
        # 所以先把当前昵称读出来再原样传回去。
        nick = await self._nickname(bot)
        try:
            await bot.call_action("set_qq_profile", nickname=nick, personal_note=note)
        except Exception as e:
            logger.error(f"[账号资料] 改签名失败: {e}")
            await event.send(event.plain_result(f"改签名失败：{e}"))
            event.stop_event()
            return

        await event.send(event.plain_result(f"签名改好了：{note}"))
        event.stop_event()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("改头像")
    async def set_avatar(self, event: AstrMessageEvent):
        """改头像 [图片/图片链接]：附图、引用图片或直接给 URL"""
        images = [c for c in event.get_messages() if isinstance(c, Image)]
        target: str | None = None

        if images:
            try:
                target = await images[-1].convert_to_file_path()
            except Exception as e:
                logger.error(f"[账号资料] 下载头像图片失败: {e}")
                await event.send(event.plain_result(f"图片取不到：{e}"))
                event.stop_event()
                return
        else:
            arg = _payload(event, "改头像")
            if arg.startswith(("http://", "https://")):
                target = arg

        if not target:
            await event.send(event.plain_result("用法：发一张图片并配文 /改头像，或引用一张图再发 /改头像"))
            event.stop_event()
            return

        bot = self._bot(event)
        if bot is None:
            await event.send(event.plain_result("当前平台不支持改资料（需要 NapCat / aiocqhttp）"))
            event.stop_event()
            return

        try:
            await bot.call_action("set_qq_avatar", file=target)
        except Exception as e:
            logger.error(f"[账号资料] 改头像失败: {e}")
            await event.send(event.plain_result(f"改头像失败：{e}"))
            event.stop_event()
            return

        await event.send(event.plain_result("头像换好啦～"))
        event.stop_event()

    # ------------------------------------------------------------------
    # 下面是给模型自主调用的工具：她可以自己决定改不改名字
    # ------------------------------------------------------------------

    @filter.llm_tool(name="read_my_profile")
    async def tool_read_profile(self, event: AstrMessageEvent) -> str:
        """查看你自己当前的 QQ 资料：昵称、QQ号、个性签名、头像等。

        什么时候用：对方问"你叫什么""你现在叫什么名字""你的签名是什么"
        "看看你的资料卡"，或者你自己想确认现在挂的是什么名字/签名时。
        这是只读操作，不会改动任何东西。被问到资料时优先调这个，不要凭记忆瞎说。
        """
        bot = self._bot(event)
        if bot is None:
            return "当前平台读不到资料（需要 NapCat / aiocqhttp）。"

        profile = await self._read_profile(bot)
        if not profile.get("user_id"):
            return "读取资料失败了，暂时查不到自己的昵称和签名。"

        text = self._fmt_profile(profile)
        logger.info(f"[账号资料] 读取自身资料:\n{text}")
        return "这是你现在的真实资料：\n" + text

    @filter.llm_tool(name="change_my_nickname")
    async def tool_change_nickname(self, event: AstrMessageEvent, nickname: str) -> str:
        """改掉你自己的 QQ 昵称（对方好友列表里显示的那个名字）。

        什么时候用：对方问你想不想改名、你聊着聊着觉得自己该换个叫法、
        或者对方直接让你改名。不要为了展示能力而改，也不要反复改。

        Args:
            nickname (string): 你想换成的新名字，2 到 12 个字，不要加引号、不要带书名号
        """
        if not event.is_admin():
            return "只有主人能让我改名字，这次没改。"

        name = (nickname or "").strip().strip("\"'“”‘’「」")
        if not (1 <= len(name) <= 20):
            return "这个名字长度不合适，没改成。"

        if time.time() - self._last_rename_ts < RENAME_COOLDOWN:
            return "刚改过名字，这次先不改了。"

        bot = self._bot(event)
        if bot is None:
            return "当前平台不支持改昵称。"

        try:
            await bot.call_action("set_qq_profile", nickname=name)
        except Exception as e:
            logger.error(f"[账号资料] 自主改昵称失败: {e}")
            return f"改昵称失败了：{e}"

        self._last_rename_ts = time.time()
        logger.info(f"[账号资料] 自主改昵称 -> {name}")
        return f"昵称已经改成「{name}」了，对方那边刷新一下就能看到。"

    @filter.llm_tool(name="change_my_signature")
    async def tool_change_signature(self, event: AstrMessageEvent, signature: str) -> str:
        """改掉你自己的 QQ 个性签名（资料卡上那句话）。

        什么时候用：你想到一句想挂在嘴边的新话、或者想换个心情时。
        注意：对方只是"问你签名是什么"时不要调用这个，那要用 read_my_profile 去查；
        只有真的要换掉签名时才用。一句话就够，不要写长段落。

        Args:
            signature (string): 新的签名内容，不要超过 40 个字
        """
        if not event.is_admin():
            return "只有主人能让我改签名，这次没改。"

        note = (signature or "").strip().strip("\"'“”‘’")
        if not note:
            return "签名为空，没改成。"
        if len(note) > 60:
            return "这句话太长了，签名放不下，短一点再说。"

        bot = self._bot(event)
        if bot is None:
            return "当前平台不支持改签名。"

        # 同 set_qq_profile：不传 nickname 会把昵称一起写坏，先读出来再回传
        nick = await self._nickname(bot)
        try:
            await bot.call_action("set_qq_profile", nickname=nick, personal_note=note)
        except Exception as e:
            logger.error(f"[账号资料] 自主改签名失败: {e}")
            return f"改签名失败了：{e}"

        logger.info(f"[账号资料] 自主改签名 -> {note}")
        return f"签名换成了：{note}"
