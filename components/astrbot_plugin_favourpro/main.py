import asyncio
import json
import re
from contextvars import ContextVar
from pathlib import Path
from typing import Dict, Any, Optional

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api import AstrBotConfig, logger


# 只用于兜底清理：正常情况模型不应再输出这种块
_STATE_BLOCK_RE = re.compile(r"[\[【]\s*(?:Favour|Attitude|Relationship)\s*[:：].*?[\]】]", re.DOTALL)

# 标记「当前请求是插件自己的评估请求」，避免评估调用的 system_prompt 被注入语气说明
_EVAL_CTX: ContextVar[bool] = ContextVar("favorpro_in_eval", default=False)

# 评估提示词：只传状态 + 这一轮对话，要求返回 JSON
_EVAL_SYSTEM_PROMPT = """你是一个关系评估器。根据这一轮对话，更新对话者「阿鱼」对用户的内部状态。

规则：
1. 好感度范围 -100（极度厌恶）到 100（挚爱）。
2. 非对称：正面互动提升微小（+1~+3），负面互动下降更明显（-3~-10），单轮不超过 ±10。
3. attitude 是阿鱼对该用户的主观印象（20 字内），relationship 是关系定性（10 字内，如 陌生人/熟人/朋友）。
4. 印象和关系必须与好感度数值自洽。
5. 只输出 JSON，不要解释，不要代码块围栏：
{"favour": <int>, "attitude": "<str>", "relationship": "<str>"}
6. 如果这一轮平淡无信息，favour 保持不变，attitude/relationship 可用原值。"""


def _parse_eval_json(text: str) -> Optional[dict]:
    """从模型输出里抠出 JSON，容忍代码块围栏和前后废话。"""
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None




class FavourProManager:
    """
    好感度、态度与关系管理系统 (FavourPro)
    - 使用AI驱动的状态快照更新，而非增量计算。
    - 数据结构: {"user_id": {"favour": int, "attitude": str, "relationship": str}}
    """
    DEFAULT_STATE = {"favour": 0, "attitude": "中立", "relationship": "陌生人"}

    def __init__(self, data_path: Path):
        """
        初始化管理器，使用由插件主类提供的规范化数据路径。
        :param data_path: 插件的数据存储目录。
        """
        self.data_path = data_path
        self._init_path()
        self.user_data = self._load_data("user_data.json")

    def _init_path(self):
        """初始化数据目录"""
        self.data_path.mkdir(parents=True, exist_ok=True)

    def _load_data(self, filename: str) -> Dict[str, Any]:
        """加载用户状态数据"""
        path = self.data_path / filename
        if not path.exists():
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, TypeError):
            return {}

    def _save_data(self):
        """保存用户状态数据"""
        path = self.data_path / "user_data.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.user_data, f, ensure_ascii=False, indent=2)

    def get_user_state(self, user_id: str, session_id: Optional[str] = None) -> Dict[str, Any]:
        """获取用户的状态，如果不存在则返回默认状态"""
        key = f"{session_id}_{user_id}" if session_id else user_id
        return self.user_data.get(key, self.DEFAULT_STATE.copy())

    def update_user_state(self, user_id: str, new_state: Dict[str, Any], session_id: Optional[str] = None):
        """直接更新用户的状态"""
        key = f"{session_id}_{user_id}" if session_id else user_id
        # 确保好感度是整数
        if 'favour' in new_state:
            try:
                new_state['favour'] = int(new_state['favour'])
            except (ValueError, TypeError):
                # 如果转换失败，则保留旧值或默认值
                current_state = self.get_user_state(user_id, session_id)
                new_state['favour'] = current_state.get('favour', self.DEFAULT_STATE['favour'])

        self.user_data[key] = new_state
        self._save_data()


@register("FavourPro", "天各一方", "一个由AI驱动的、包含好感度、态度和关系的多维度交互系统", "1.0.4")
class FavourProPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # 获取规范的数据目录并传递给 Manager
        data_dir = StarTools.get_data_dir()
        self.manager = FavourProManager(data_dir)

        # 后台评估任务，按「会话+用户」去重，防止同一人连发时堆叠
        self._eval_tasks: Dict[str, Any] = {}

    @property
    def session_based(self) -> bool:
        """
        动态地从配置中读取 session_based 的值，以支持热加载。
        框架会直接返回最终的布尔值。
        """
        # 直接获取布尔值，如果配置项不存在，则默认为 False
        value = self.config.get("session_based", False)
        # 确保返回的是布尔类型
        return bool(value)

    def _get_session_id(self, event: AstrMessageEvent) -> Optional[str]:
        """根据配置决定是否返回会话ID"""
        return event.unified_msg_origin if self.session_based else None

    @filter.on_llm_request()
    async def add_context_prompt(self, event: AstrMessageEvent, req: ProviderRequest):
        """只注入「此刻对这个人的感觉」，让语气自然受状态影响；不要求输出任何标记。"""
        # 插件自己发起的评估请求不需要注入，也不该被注入
        if _EVAL_CTX.get():
            return

        user_id = event.get_sender_id()
        session_id = self._get_session_id(event)

        state = self.manager.get_user_state(user_id, session_id)

        # 关键：把历史里旧版本留下的状态块洗掉，否则模型会照着历史继续模仿
        scrubbed = self._scrub_contexts(req.contexts)

        # 注入当前状态
        context_prompt = (
            f"[当前状态] 你与该用户的关系是：{state['relationship']}，"
            f"好感度为 {state['favour']}，"
            f"你对他的印象是：{state['attitude']}。"
        )

        # 只告诉它「此刻怎么想」，不提任何字段格式，避免反向提示
        tone_prompt = (
            "[内心状态] 以上是你此刻对这个人的真实感受，只用来决定你说话的语气和态度。\n"
            "回复里只写你真正想说的话，不要附加任何状态注记、数值记录或结构化字段。\n"
            "语气参考：75~100 热情主动；40~74 积极友好；-10~39 客气但有距离；"
            "-50~-11 冷淡简短；-100~-51 尖锐，甚至拒绝回应。"
        )
        req.system_prompt += f"\n{context_prompt}\n{tone_prompt}"
        if scrubbed:
            logger.debug(f"[favorpro] 已从历史上下文中清洗 {scrubbed} 处旧状态块")

    @staticmethod
    def _trim(text: str) -> str:
        """删掉文本里的状态块，没有则原样返回。"""
        return _STATE_BLOCK_RE.sub("", text).strip()

    @staticmethod
    def _scrub_contexts(contexts) -> int:
        """把历史消息里残留的旧状态块删掉，返回清洗处数。

        模型是靠「历史里有这个格式」学出来的，不清历史光靠提示词压不住。
        注意：AstrBot 的 req.contexts 是 list[dict]，content 既可能是 str，
        也可能是 [{"type":"text","text":...}] —— 四种组合都要覆盖。
        """
        if not contexts:
            return 0
        cleaned = 0
        for msg in contexts:
            try:
                if isinstance(msg, dict):
                    val = msg.get("content")
                    if isinstance(val, str):
                        if _STATE_BLOCK_RE.search(val):
                            msg["content"] = FavourProPlugin._trim(val)
                            cleaned += 1
                    elif isinstance(val, list):
                        for part in val:
                            if isinstance(part, dict):
                                t = part.get("text")
                                if isinstance(t, str) and _STATE_BLOCK_RE.search(t):
                                    part["text"] = FavourProPlugin._trim(t)
                                    cleaned += 1
                            else:
                                t = getattr(part, "text", None)
                                if isinstance(t, str) and _STATE_BLOCK_RE.search(t):
                                    part.text = FavourProPlugin._trim(t)
                                    cleaned += 1
                    continue

                val = getattr(msg, "content", None)
                if isinstance(val, str):
                    if _STATE_BLOCK_RE.search(val):
                        msg.content = FavourProPlugin._trim(val)
                        cleaned += 1
                elif isinstance(val, list):
                    for part in val:
                        t = getattr(part, "text", None)
                        if isinstance(t, str) and _STATE_BLOCK_RE.search(t):
                            part.text = FavourProPlugin._trim(t)
                            cleaned += 1
            except Exception:
                continue
        return cleaned

    @filter.on_llm_response()
    async def on_llm_resp(self, event: AstrMessageEvent, resp: LLMResponse):
        """回复产生后：先兜底清掉万一出现的标记，再丢一个后台任务去评估关系。"""
        text = resp.completion_text or ""

        # 兜底：历史里还有老格式，模型可能模仿。真出现就删掉并告警
        if _STATE_BLOCK_RE.search(text):
            resp.completion_text = _STATE_BLOCK_RE.sub("", text).strip()
            logger.warning("[favorpro] 回复中出现状态块标记，已清除（模型在模仿旧格式）")

        self._spawn_eval(event, resp.completion_text or "")

    def _spawn_eval(self, event: AstrMessageEvent, reply_text: str):
        """后台跑评估，不阻塞这条回复的发送。"""
        if not self.config.get("auto_eval", True):
            return
        if not reply_text.strip():
            return
        key = f"{event.unified_msg_origin}_{event.get_sender_id()}"
        running = self._eval_tasks.get(key)
        if running and not running.done():
            logger.debug("[favorpro] 上一轮评估还没结束，跳过本次")
            return
        task = asyncio.create_task(self._evaluate(event, reply_text))
        self._eval_tasks[key] = task
        task.add_done_callback(lambda t, k=key: self._eval_tasks.pop(k, None))

    async def _evaluate(self, event: AstrMessageEvent, reply_text: str):
        """单独调一次轻量模型，只为了算出好感/印象/关系。"""
        user_id = event.get_sender_id()
        session_id = self._get_session_id(event)
        try:
            user_text = (getattr(event, "message_str", "") or "").strip()
            if not user_text:
                return
            provider = self.context.get_using_provider(umo=event.unified_msg_origin)
            if provider is None:
                logger.warning("[favorpro] 拿不到 provider，本轮不评估")
                return

            state = self.manager.get_user_state(user_id, session_id)
            prompt = (
                f"当前状态：好感度 {state['favour']}，印象：{state['attitude']}，"
                f"关系：{state['relationship']}\n"
                f"用户说：{user_text[:500]}\n"
                f"阿鱼回：{reply_text[:500]}\n"
                "给出更新后的状态 JSON。"
            )
            tok = _EVAL_CTX.set(True)
            try:
                resp = await provider.text_chat(
                    prompt=prompt,
                    system_prompt=_EVAL_SYSTEM_PROMPT,
                    contexts=[],
                )
            finally:
                _EVAL_CTX.reset(tok)
            data = _parse_eval_json(resp.completion_text or "")
            if not data:
                logger.warning(f"[favorpro] 评估结果不是 JSON，已跳过: {(resp.completion_text or '')[:80]}")
                return

            new_state = dict(state)
            if "favour" in data:
                try:
                    new_state["favour"] = max(-100, min(100, int(data["favour"])))
                except (TypeError, ValueError):
                    pass
            if data.get("attitude"):
                new_state["attitude"] = str(data["attitude"]).strip()[:30]
            if data.get("relationship"):
                new_state["relationship"] = str(data["relationship"]).strip()[:20]

            self.manager.update_user_state(user_id, new_state, session_id)
            logger.debug(
                f"[favorpro] 评估完成 {user_id}: 好感 {state['favour']} -> {new_state['favour']}，"
                f"印象 {new_state['attitude']}，关系 {new_state['relationship']}",
            )
        except Exception as e:  # 后台任务不能把异常抛给主流程
            logger.warning(f"[favorpro] 评估失败: {e}")

    # ------------------- 管理员命令 -------------------

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """检查事件发送者是否为AstrBot管理员"""
        return event.role == "admin"

    @filter.command("查询好感")
    async def admin_query_status(self, event: AstrMessageEvent, user_id: str):
        """(管理员) 查询指定用户的状态"""
        if not self._is_admin(event):
            yield event.plain_result("错误：此命令仅限管理员使用。")
            return

        session_id = self._get_session_id(event)
        state = self.manager.get_user_state(user_id.strip(), session_id)

        response_text = (
            f"用户 {user_id} 的状态：\n"
            f"好感度：{state['favour']}\n"
            f"关系：{state['relationship']}\n"
            f"态度：{state['attitude']}"
        )
        yield event.plain_result(response_text)

    @filter.command("设置好感")
    async def admin_set_favour(self, event: AstrMessageEvent, user_id: str, value: str):
        """(管理员) 设置指定用户的好感度"""
        if not self._is_admin(event):
            yield event.plain_result("错误：此命令仅限管理员使用。")
            return

        try:
            favour_value = int(value)
        except ValueError:
            yield event.plain_result("错误：好感度值必须是一个整数。")
            return

        user_id = user_id.strip()
        session_id = self._get_session_id(event)
        current_state = self.manager.get_user_state(user_id, session_id)
        current_state['favour'] = favour_value
        self.manager.update_user_state(user_id, current_state, session_id)

        yield event.plain_result(f"成功：用户 {user_id} 的好感度已设置为 {favour_value}。")

    @filter.command("设置印象")
    async def admin_set_attitude(self, event: AstrMessageEvent, user_id: str, *, attitude: str):
        """(管理员) 设置指定用户的印象。支持带空格的文本。"""
        if not self._is_admin(event):
            yield event.plain_result("错误：此命令仅限管理员使用。")
            return

        user_id = user_id.strip()
        attitude = attitude.strip()
        session_id = self._get_session_id(event)
        current_state = self.manager.get_user_state(user_id, session_id)
        current_state['attitude'] = attitude
        self.manager.update_user_state(user_id, current_state, session_id)

        yield event.plain_result(f"成功：用户 {user_id} 的态度已设置为 '{attitude}'。")

    @filter.command("设置关系")
    async def admin_set_relationship(self, event: AstrMessageEvent, user_id: str, *, relationship: str):
        """(管理员) 设置指定用户的关系。支持带空格的文本。"""
        if not self._is_admin(event):
            yield event.plain_result("错误：此命令仅限管理员使用。")
            return

        user_id = user_id.strip()
        relationship = relationship.strip()
        session_id = self._get_session_id(event)
        current_state = self.manager.get_user_state(user_id, session_id)
        current_state['relationship'] = relationship
        self.manager.update_user_state(user_id, current_state, session_id)

        yield event.plain_result(f"成功：用户 {user_id} 的关系已设置为 '{relationship}'。")

    @filter.command("重置好感")
    async def admin_reset_user_status(self, event: AstrMessageEvent, user_id: str):
        """(管理员) 重置指定用户的全部状态为默认值"""
        if not self._is_admin(event):
            yield event.plain_result("错误：此命令仅限管理员使用。")
            return

        user_id = user_id.strip()
        session_id = self._get_session_id(event)
        
        # 为了确保操作的是正确的键，我们先获取当前状态
        # get_user_state 内部会处理 session_id，我们无需手动拼接 key
        state = self.manager.get_user_state(user_id, session_id)

        # 如果用户原本就不存在，也会得到默认状态，直接更新即可
        self.manager.update_user_state(user_id, self.manager.DEFAULT_STATE.copy(), session_id)
        
        yield event.plain_result(f"成功：用户 {user_id} 的状态已重置为默认值。")

    @filter.command("重置负面")
    async def admin_reset_negative_favour(self, event: AstrMessageEvent):
        """(管理员) 重置所有好感度为负数的用户状态"""
        if not self._is_admin(event):
            yield event.plain_result("错误：此命令仅限管理员使用。")
            return
        
        # 找出所有好感度<0的用户key
        keys_to_reset = [
            key for key, state in self.manager.user_data.items() 
            if state.get('favour', 0) < 0
        ]

        if not keys_to_reset:
            yield event.plain_result("信息：没有找到任何好感度为负的用户。")
            return

        # 遍历并重置
        for key in keys_to_reset:
            self.manager.user_data[key] = self.manager.DEFAULT_STATE.copy()
        
        self.manager._save_data()
        yield event.plain_result(f"成功：已重置 {len(keys_to_reset)} 个好感度为负的用户。")

    @filter.command("重置全部")
    async def admin_reset_all_users(self, event: AstrMessageEvent):
        """(管理员) 重置所有用户的状态数据"""
        if not self._is_admin(event):
            yield event.plain_result("错误：此命令仅限管理员使用。")
            return

        user_count = len(self.manager.user_data)
        self.manager.user_data.clear()
        self.manager._save_data()
        
        yield event.plain_result(f"成功：已清空并重置全部 {user_count} 个用户的状态数据。")

    @filter.command("好感排行")
    async def admin_favour_ranking(self, event: AstrMessageEvent, num: str = "10"):
        """(管理员) 显示好感度最高的N个用户"""
        if not self._is_admin(event):
            yield event.plain_result("错误：此命令仅限管理员使用。")
            return
        
        try:
            limit = int(num)
            if limit <= 0:
                raise ValueError
        except ValueError:
            yield event.plain_result("错误：排行数量必须是一个正整数。")
            return

        if not self.manager.user_data:
            yield event.plain_result("当前没有任何用户数据。")
            return

        # 按好感度降序排序
        sorted_users = sorted(
            self.manager.user_data.items(),
            key=lambda item: item[1].get('favour', 0),
            reverse=True
        )

        response_lines = [f"好感度 TOP {limit} 排行榜："]
        for i, (user_key, state) in enumerate(sorted_users[:limit]):
            line = (
                f"{i + 1}. 用户: {user_key}\n"
                f"   - 好感: {state['favour']}, 关系: {state['relationship']}, 印象: {state['attitude']}"
            )
            response_lines.append(line)
        
        yield event.plain_result("\n".join(response_lines))

    @filter.command("负好感排行")
    async def admin_negative_favour_ranking(self, event: AstrMessageEvent, num: str = "10"):
        """(管理员) 显示好感度最低的N个用户"""
        if not self._is_admin(event):
            yield event.plain_result("错误：此命令仅限管理员使用。")
            return

        try:
            limit = int(num)
            if limit <= 0:
                raise ValueError
        except ValueError:
            yield event.plain_result("错误：排行数量必须是一个正整数。")
            return

        if not self.manager.user_data:
            yield event.plain_result("当前没有任何用户数据。")
            return
            
        # 按好感度升序排序
        sorted_users = sorted(
            self.manager.user_data.items(),
            key=lambda item: item[1].get('favour', 0)
        )
        
        response_lines = [f"好感度 BOTTOM {limit} 排行榜："]
        for i, (user_key, state) in enumerate(sorted_users[:limit]):
            line = (
                f"{i + 1}. 用户: {user_key}\n"
                f"   - 好感: {state['favour']}, 关系: {state['relationship']}, 印象: {state['attitude']}"
            )
            response_lines.append(line)
            
        yield event.plain_result("\n".join(response_lines))

    async def terminate(self):
        """插件终止时，取消未完成的后台评估并保存数据"""
        for task in list(self._eval_tasks.values()):
            if not task.done():
                task.cancel()
        self._eval_tasks.clear()
        self.manager._save_data()
