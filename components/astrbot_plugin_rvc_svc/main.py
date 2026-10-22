from pathlib import Path
import os
import re
import json
import asyncio
import shutil
import tempfile
import time
import uuid
from datetime import datetime
import traceback
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.core.star.star_tools import StarTools
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import Record
from astrbot.api.message_components import Node, Plain, Image as CompImage
from astrbot.core.utils.session_waiter import session_waiter, SessionController
from astrbot.api import logger
from functools import partial
from gradio_client import Client

from . import segment
from . import speak as speakmod
from .qqsource import QQSource

# 定义分隔符常量，方便修改
MODEL_ALIAS_SEPARATOR = "|||"


def _self_uin(event) -> int:
    """合并转发节点里显示的发送者 QQ：取机器人自身账号，取不到就用占位值。

    别写死成某个具体账号——换了机器人号之后合并转发会顶着别人的头像。
    """
    try:
        raw = str(event.get_self_id() or "")
        if raw.isdigit():
            return int(raw)
    except Exception:
        pass
    return 10000


def _astrbot_bin() -> Path:
    """AstrBot 自带的 bin 目录。

    插件装在 <AstrBot>/data/plugins/<插件名>/ 下，往上三级就是 AstrBot 根。
    别写死盘符——AstrBot 跟着仓库走（见 runtime/），换机器就换了位置。
    """
    home = os.environ.get("ASTRBOT_HOME")
    return (Path(home) if home else Path(__file__).resolve().parents[3]) / "bin"


def _ensure_ffmpeg_on_path() -> None:
    """把 AstrBot 自带的 ffmpeg 目录塞进本进程的 PATH。

    为什么要干这事：插件自己发语音是能用的（find_ffmpeg 会兜底到 imageio-ffmpeg），
    但真正把语音交出去的是 AstrBot 核心的 media_utils，它硬编码调裸名 "ffmpeg"，
    PATH 里没有就直接 raise Exception("ffmpeg not found")。
    正常用 一键启动服务.bat 启动时 launcher 会把 bin 目录加进 PATH，但只要 AstrBot
    是别的方式拉起来的（手动跑、重启），语音就会莫名其妙发不出去。
    这里在插件加载时补一刀，跟启动方式彻底解耦。
    """
    bin_dir = _astrbot_bin()
    if not (bin_dir / "ffmpeg.exe").exists():
        return
    cur = os.environ.get("PATH", "")
    if str(bin_dir).lower() in cur.lower():
        return
    os.environ["PATH"] = f"{bin_dir}{os.pathsep}{cur}"
    logger.info("已把 %s 补进 PATH，核心转码才能找到 ffmpeg", bin_dir)


_ensure_ffmpeg_on_path()

@register(
    "astrbot_plugin_rvc_svc",
    "CCYellowStar2",
    "RVC/SVC翻唱网易云歌曲",
    "1.0.0",
    "https://github.com/CCYellowStar2/astrbot_plugin_rvc_svc",
)
class MusicPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        
        # === 支持两个独立的后端配置 ===
        self.rvc_base_url = config.get("rvc_base_url", "http://127.0.0.1:7860/")
        self.svc_base_url = config.get("svc_base_url", "http://127.0.0.1:7866/")
        
        self.default_api = config.get("default_api", "netease_nodejs")
        self.nodejs_base_url = config.get("nodejs_base_url", "https://163api.qijieya.cn")
        self.timeout = config.get("timeout", 60)
        
        # === 为 RVC 和 SVC 分别存储模型列表 ===
        self.rvc_models_keywords = config.get("rvc_models_keywords", [])
        self.svc_models_keywords = config.get("svc_models_keywords", [])
        
        self.inference_timeout = config.get("inference_timeout", 300)

        # === 自主翻唱（sing_song 工具）===
        # 音色不指定时取 rvc_models_keywords 的第一项
        self.sing_model = config.get("sing_model", "") or ""
        self.sing_clip = float(config.get("sing_clip_seconds", 18) or 18)
        try:
            self.sing_cooldown = max(0, int(config.get("sing_cooldown", 30)))
        except (TypeError, ValueError):
            self.sing_cooldown = 30
        self.full_seg = float(config.get("full_segment_seconds", 55) or 55)
        self.max_full_segments = int(config.get("full_max_segments", 8) or 8)

        # === 本地缓存：源曲 + 整首成品 ===
        # 源曲留最近用的 N 首，下次点同一首歌不用再取直链、不用再下载；
        # 整首成品按「歌 + 音色 + 升降调」留一份，重复点同一首整首可以直接秒回。
        try:
            self.sing_src_cache_max = int(config.get("sing_src_cache_max", 30))
        except (TypeError, ValueError):
            self.sing_src_cache_max = 30
        try:
            self.sing_full_cache_max = int(config.get("sing_full_cache_max", 50))
        except (TypeError, ValueError):
            self.sing_full_cache_max = 50
        self.sing_full_cache_enable = bool(config.get("sing_full_cache_enable", True))
        self._sing_data_root: Path | None = None

        self._cooldown_map: dict[str, float] = {}
        # 同一首歌最近唱过的起点，用来避开重复片段
        self._clip_history: dict[str, list[float]] = {}

        # === 说话（send_voice 工具）：TTS 出声 + RVC 换成她的音色 ===
        # 走路子不一样：说话不需要 UVR 分离，直接喂 RVC WebUI(默认 7897) 的单条推理
        self.rvc_webui_url = config.get("rvc_webui_url", "http://127.0.0.1:7897") or "http://127.0.0.1:7897"
        # 说话走哪条链路：
        #   cosyvoice = CosyVoice2 直出（音色+韵律一起生成，有情感），失败自动回落 rvc
        #   rvc       = 老的 edge-tts + RVC 换音色（念稿味，只作为显式选择或兜底）
        self.speak_engine = (config.get("speak_engine", "cosyvoice") or "cosyvoice").strip().lower()
        if self.speak_engine not in ("cosyvoice", "rvc"):
            self.speak_engine = "cosyvoice"
        self.cosyvoice_url = (
            config.get("cosyvoice_url", "http://127.0.0.1:7870") or "http://127.0.0.1:7870"
        ).rstrip("/")
        # 默认情感指令；LLM 每次调用可以自己覆盖（更贴合当句语境）
        self.speak_style = (
            config.get("speak_style", "用自然、亲切、像真人说话的语气，带一点情绪起伏") or ""
        )
        self.speak_model = config.get("speak_model", "") or self.sing_model
        # 说话这条链路走的是 7897 的 infer_convert，不像 7860 那样会自动配索引，
        # 得自己按模型名去索引目录里找同名的 .index（没有就不传，音色略糊但不会报错）
        self.rvc_index_dir = config.get("rvc_index_dir", "") or ""
        self.speak_voice = config.get("speak_voice", "zh-CN-XiaoyiNeural") or "zh-CN-XiaoyiNeural"
        self.speak_rate = config.get("speak_rate", "+0%") or "+0%"
        self.speak_pitch = config.get("speak_pitch", "+0Hz") or "+0Hz"
        try:
            self.speak_key_shift = float(config.get("speak_key_shift", 0) or 0)
        except (TypeError, ValueError):
            self.speak_key_shift = 0.0
        try:
            self.speak_cooldown = max(0, int(config.get("speak_cooldown", 8)))
        except (TypeError, ValueError):
            self.speak_cooldown = 8
        try:
            self.speak_max_chars = max(10, int(config.get("speak_max_chars", 90)))
        except (TypeError, ValueError):
            self.speak_max_chars = 90
        self._speak_cooldown_map: dict[str, float] = {}

        self._qq = QQSource()
        
        if self.default_api == "netease":
            from .api import NetEaseMusicAPI
            self.api = NetEaseMusicAPI()
        elif self.default_api == "netease_nodejs":
            from .api import NetEaseMusicAPINodeJs
            self.api = NetEaseMusicAPINodeJs(base_url=self.nodejs_base_url)

    async def _async_predict(self, client, *args, timeout=300, **kwargs):
        """将同步的 predict 变为异步，并接受超时参数"""
        loop = asyncio.get_running_loop()
        job = client.submit(*args, **kwargs)
        fn = partial(job.result, timeout=timeout)
        return await loop.run_in_executor(None, fn)

    # ==================== 本地缓存：源曲 / 整首成品 ====================

    def _cache_dir(self, sub: str) -> Path:
        """缓存目录放在插件数据目录里，重启不丢，也不会被系统清理临时文件带走。"""
        if self._sing_data_root is None:
            # 优先按插件自身位置反推 data/plugin_data：AstrBot 的官方 API 在没设
            # ASTRBOT_ROOT 时是拿 os.getcwd() 拼的，换个启动方式缓存就会跑偏。
            try:
                candidate = (
                    Path(__file__).resolve().parents[2]
                    / "plugin_data"
                    / "astrbot_plugin_rvc_svc"
                )
                if not candidate.parent.is_dir():
                    raise FileNotFoundError(str(candidate.parent))
                self._sing_data_root = candidate
            except Exception:
                try:
                    self._sing_data_root = Path(
                        StarTools.get_data_dir("astrbot_plugin_rvc_svc")
                    )
                except Exception:
                    # 最后兜底：插件自带的 data/，总比缓存不了强
                    self._sing_data_root = Path(__file__).parent / "data"
        target = self._sing_data_root / "sing_cache" / sub
        target.mkdir(parents=True, exist_ok=True)
        return target

    @staticmethod
    def _norm_name(text: str) -> str:
        """歌名归一化：去空格、标点、括号，统一小写，用来做模糊对齐。"""
        return re.sub(r"[\s\-_·・,，.。、'\"“”‘’()（）\[\]【】/\\]+", "", (text or "").lower())

    def _cache_key(self, song: dict, model: str, key_shift: int) -> str:
        """成品缓存的键：歌 + 音色 + 升降调。

        音色或调一变，唱出来的东西就不是同一个了，必须分开存，
        否则换了音色还会拿旧音色的成品糊弄人。
        """
        stem = os.path.splitext(os.path.basename(model or "default"))[0] or "default"
        stem = re.sub(r"[^\w.-]", "_", stem)
        song_id = song.get("id") or self._norm_name(song.get("name", "")) or "unknown"
        return f"{song_id}__{stem}__k{int(key_shift or 0)}"

    @staticmethod
    def _touch(p: Path) -> None:
        """刷新访问时间，LRU 靠它排序。"""
        try:
            os.utime(p, None)
        except OSError:
            pass

    def _trim_lru(self, directory: Path, max_items: int, pattern: str = "*.mp3") -> None:
        """只留最近用过的 max_items 个，越久没碰的越先删。

        max_items <= 0 视为不限制（宁可不删，也别把配置写错导致清空）。
        """
        if max_items <= 0:
            return
        files = []
        for f in directory.glob(pattern):
            try:
                if f.is_file():
                    files.append(f)
            except OSError:
                continue
        if len(files) <= max_items:
            return
        files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        for f in files[max_items:]:
            try:
                f.unlink()
                logger.info(f"[缓存] 淘汰最久未用: {f.name}")
            except OSError as e:
                logger.warning(f"[缓存] 删除失败 {f.name}: {e}")

    def _trim_full_cache(self, directory: Path, max_items: int) -> None:
        """成品缓存裁剪：连配套的 .json 元数据一起删，不留孤儿文件。"""
        if max_items <= 0:
            return
        files = [f for f in directory.glob("*.mp3") if f.is_file()]
        if len(files) <= max_items:
            return
        files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        for f in files[max_items:]:
            try:
                f.unlink()
                f.with_suffix(".json").unlink(missing_ok=True)
                logger.info(f"[成品缓存] 淘汰最久未用: {f.name}")
            except OSError as e:
                logger.warning(f"[成品缓存] 删除失败 {f.name}: {e}")

    def _lookup_full_cache(
        self, song_name: str, model: str, key_shift: int
    ) -> tuple[Path, dict] | None:
        """按歌名找整首成品，命中就完全不用搜索、取链、下载、分离、推理。

        比对时先看官方名/别名（模型每次说的名字未必一样，中文名、日文原名、
        带不带歌手都算），再退一步做互相包含的模糊比对。

        只接受唯一命中：如果同名歌有两首（原版 / Live / 翻唱版），宁可放弃缓存
        交给正常搜索去精确对齐，也不能张冠李戴把她唱过的另一首歌顶上来。
        """
        d = self._cache_dir("full")
        wanted = self._norm_name(song_name)
        if not wanted:
            return None
        hits: list[tuple[Path, dict]] = []
        for meta_path in d.glob("*.json"):
            mp3 = meta_path.with_suffix(".mp3")
            if not mp3.is_file():
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if meta.get("model") != model:
                continue
            try:
                if int(meta.get("key_shift", 0)) != int(key_shift or 0):
                    continue
            except (TypeError, ValueError):
                continue

            # 所有可能的叫法：官方名、名+歌手、历次调用时用过的别名
            cands = {
                self._norm_name(meta.get("name", "")),
                self._norm_name(f"{meta.get('name', '')}{meta.get('artists', '')}"),
            }
            for alias in meta.get("aliases") or []:
                cands.add(self._norm_name(alias))
            cands.discard("")

            matched = wanted in cands
            if not matched and len(wanted) >= 4:
                # 退一步：互相包含也算（"晴天" vs "晴天 周杰伦"）
                matched = any(
                    len(c) >= 4 and (wanted in c or c in wanted) for c in cands
                )
            if matched:
                hits.append((mp3, meta))
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            logger.info(
                f"[成品缓存] 「{song_name}」有 {len(hits)} 个候选，不敢猜，走正常搜索"
            )
        return None

    def _full_cache_by_song(
        self, song: dict, model: str, key_shift: int
    ) -> tuple[Path, dict] | None:
        """搜到歌之后按歌曲 ID 再对一次缓存。

        这一步是给别名对不上的情况兜底：名字说法千变万化，但 songmid 是唯一的。
        命中后仍然要搜索（1-3 秒），但能省掉下载、分离、推理。
        """
        if not self.sing_full_cache_enable:
            return None
        try:
            d = self._cache_dir("full")
            key = self._cache_key(song, model, key_shift)
            mp3 = d / f"{key}.mp3"
            if not mp3.is_file() or mp3.stat().st_size < 100 * 1024:
                return None
            meta_path = d / f"{key}.json"
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
            for k, v in (
                ("id", song.get("id", "")),
                ("name", song.get("name", "")),
                ("artists", song.get("artists", "")),
                ("model", model),
                ("key_shift", int(key_shift or 0)),
            ):
                meta.setdefault(k, v)
            return mp3, meta
        except Exception as e:
            logger.warning(f"[成品缓存] 按 ID 查找失败: {e}")
            return None

    def _save_full_cache(
        self,
        song: dict,
        model: str,
        key_shift: int,
        result_path,
        lyrics_text: str,
        alias: str = "",
    ) -> None:
        """把唱好的整首成品存一份，下次同一首歌同一音色直接秒回。"""
        if not self.sing_full_cache_enable:
            return
        try:
            d = self._cache_dir("full")
            key = self._cache_key(song, model, key_shift)
            target = d / f"{key}.mp3"
            meta_path = d / f"{key}.json"
            # 已经有同一首的成品就不再覆盖写：同 key 意味着同歌同音色同调，
            # 内容本该一样，重复拷 5~6 MB 只是白磨硬盘。
            fresh = not (target.is_file() and target.stat().st_size > 100 * 1024)
            if fresh:
                shutil.copy2(result_path, target)
            # 别名要累积：同一首歌她可能今天说中文名、明天说日文原名，
            # 只留最近一次的叫法就会把早先的说法弄丢。
            aliases: list[str] = []
            try:
                old = json.loads(meta_path.read_text(encoding="utf-8"))
                aliases = [a for a in (old.get("aliases") or []) if isinstance(a, str)]
            except Exception:
                pass
            for a in (alias, song.get("name", "")):
                a = (a or "").strip()
                if a and a not in aliases:
                    aliases.append(a)
            meta = {
                "id": song.get("id", ""),
                "name": song.get("name", ""),
                "artists": song.get("artists", ""),
                "model": model,
                "key_shift": int(key_shift or 0),
                "lyrics": lyrics_text or "",
                "aliases": aliases[-20:],
                "saved_at": datetime.now().isoformat(timespec="seconds"),
            }
            meta_path.write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            if fresh:
                logger.info(
                    f"[成品缓存] 已存成品 {target.name} "
                    f"({target.stat().st_size / 1048576:.1f} MB)"
                )
            else:
                logger.info(f"[成品缓存] 成品已存在，只更新歌名别名: {target.name}")
        except Exception as e:
            # 存缓存失败不影响这次演唱，别让它把正常流程带崩
            logger.warning(f"[成品缓存] 保存失败（本次照常发歌）: {e}")

    def get_models_display_list(self, api_type="rvc"):
        """获取指定 API 类型的模型显示列表"""
        models_keywords = self.svc_models_keywords if api_type == "svc" else self.rvc_models_keywords
        display_names, key_list = [], []
        for index, item_str in enumerate(models_keywords, start=1):
            parts = item_str.split(MODEL_ALIAS_SEPARATOR, 1)
            model_name = parts[0]
            alias = parts[1] if len(parts) > 1 and parts[1] else ""
            display_name = alias or os.path.splitext(model_name)[0]
            display_names.append(f"{index}. {display_name}")
            key_list.append(model_name)
        return "\n".join(display_names), key_list

    async def _update_models_from_api(self, api_type="rvc"):
        """从指定的 API 更新模型列表"""
        base_url = self.svc_base_url if api_type == "svc" else self.rvc_base_url
        client = Client(base_url)
        try:
            model_list_from_api = await self._async_predict(client, api_name="/show_model")
        except Exception:
            model_list_from_api = await self._async_predict(client, api_name="/show_model")

        if not isinstance(model_list_from_api, list):
            raise ValueError(f"获取模型列表失败: {model_list_from_api}")

        # 获取当前配置的模型列表
        current_config_list = self.svc_models_keywords if api_type == "svc" else self.rvc_models_keywords
        old_aliases = {}
        for item_str in current_config_list:
            parts = item_str.split(MODEL_ALIAS_SEPARATOR, 1)
            if len(parts) > 1:
                old_aliases[parts[0]] = parts[1]

        new_models_list = [f"{m}{MODEL_ALIAS_SEPARATOR}{old_aliases.get(m, '')}" for m in model_list_from_api]
        
        # 保存模型列表
        if api_type == "svc":
            self.svc_models_keywords = new_models_list
            self.config["svc_models_keywords"] = new_models_list
        else:
            self.rvc_models_keywords = new_models_list
            self.config["rvc_models_keywords"] = new_models_list
        
        self.config.save_config()
        logger.info(f"{api_type.upper()} 模型列表已更新并成功保存，共 {len(new_models_list)} 个模型")

    # ==================== 工具选择纠偏 ====================

    # 这一轮如果要发语音，就把"自己攒一段音频"的路直接收掉。
    # 起因见 2026-10-20 那次排查：模型手上有 shell/python 权限时，会绕开 send_voice，
    # 自己调系统朗读接口合成一条 wav 再发出去，出来的是机器嗓，完全不是她的音色。
    # 光靠工具 description 拦不住 —— 上下文里一旦有过这种"成功"先例，模型就会重复它。
    # 所以这里在请求发出前，把这些工具从这一轮的工具表里摘掉，让它物理上走不通。
    _VOICE_HINT = re.compile(
        r"语音|发个声|发段声|你的声音|你的嗓子|念一段|念给我|念一下|"
        r"说一句|说句话|录一段|听你说|听听你|唱一段|来一段|唱歌|唱首|唱个"
    )
    _SHELL_TOOLS = (
        "astrbot_execute_shell",
        "astrbot_execute_python",
        "send_message_to_user",
    )

    @filter.on_llm_request()
    async def _guard_voice_turn(self, event: AstrMessageEvent, req):
        try:
            text = str(getattr(req, "prompt", "") or getattr(event, "message_str", "") or "")
            for m in (getattr(req, "contexts", None) or [])[-6:]:
                try:
                    text += " " + str(m.get("content") or "")
                except Exception:
                    pass
            if not self._VOICE_HINT.search(text):
                return

            toolset = getattr(req, "func_tool", None)
            removed = []
            if toolset is not None and hasattr(toolset, "remove_tool"):
                for name in self._SHELL_TOOLS:
                    if toolset.get_tool(name) is not None:
                        toolset.remove_tool(name)
                        removed.append(name)
            if removed:
                logger.info(f"本轮要发语音，已摘掉会绕路的工具: {removed}")
            req.system_prompt = (req.system_prompt or "") + (
                "\n\n【这一轮要发语音】发语音只有一条路：调 send_voice（唱歌用 sing_song）。"
                "不要用命令行去合成音频，也不要自己找系统朗读接口、不要自己拼 wav ——"
                "那些做出来的不是你，是这台电脑的机器嗓，对方一听就知道不对。"
            )
        except Exception:
            logger.warning(f"语音轮次纠偏失败（不影响正常回复）: {traceback.format_exc()[:300]}")

    # ==================== RVC 命令 ====================
    
    @filter.command("刷新rvc模型")
    async def refresh_rvc_models(self, event: AstrMessageEvent):
        yield event.plain_result("正在刷新 RVC 模型列表，请稍候...")
        try:
            await self._update_models_from_api(api_type="rvc")
            yield event.plain_result("刷新成功！")
            display_str, _ = self.get_models_display_list(api_type="rvc")
            display_str = display_str or "未发现任何模型。"
            chain=[Plain(f"当前 RVC 可用模型：\n{display_str}")]
            node = Node(
                uin=_self_uin(event),
                name="玖玖瑠",
                content=chain
            )
            await event.send(event.chain_result([node]))
        except Exception as e:
            logger.error(traceback.format_exc())
            yield event.plain_result(f"刷新 RVC 模型出错了: {e}")
            
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("设置rvc后端链接")
    async def set_rvc_url(self, event: AstrMessageEvent):
        args = event.message_str.replace("设置rvc后端链接", "").strip().split()
        if not args:
            yield event.plain_result(f"当前 RVC 后端: {self.rvc_base_url}\n用法: /设置rvc后端链接 <URL>")
            return
        _url = args[0]
        if not _url.endswith("/"): _url += "/"
        self.rvc_base_url = _url
        self.config["rvc_base_url"] = _url
        self.config.save_config()
        yield event.plain_result(f"RVC 后端链接已设置为: {_url}")

    @filter.command("rvc")
    async def rvc(self, event: AstrMessageEvent):
        """RVC 翻唱命令"""
        async for result in self._handle_cover(event, api_type="rvc"):
            yield result

    # ==================== SVC 命令 ====================
    
    @filter.command("刷新svc模型")
    async def refresh_svc_models(self, event: AstrMessageEvent):
        yield event.plain_result("正在刷新 SVC 模型列表，请稍候...")
        try:
            await self._update_models_from_api(api_type="svc")
            yield event.plain_result("刷新成功！")
            display_str, _ = self.get_models_display_list(api_type="svc")
            display_str = display_str or "未发现任何模型。"
            chain=[Plain(f"当前 SVC 可用模型：\n{display_str}")]
            node = Node(
                uin=_self_uin(event),
                name="玖玖瑠",
                content=chain
            )
            await event.send(event.chain_result([node]))
        except Exception as e:
            logger.error(traceback.format_exc())
            yield event.plain_result(f"刷新 SVC 模型出错了: {e}")
            
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("设置svc后端链接")
    async def set_svc_url(self, event: AstrMessageEvent):
        args = event.message_str.replace("设置svc后端链接", "").strip().split()
        if not args:
            yield event.plain_result(f"当前 SVC 后端: {self.svc_base_url}\n用法: /设置svc后端链接 <URL>")
            return
        _url = args[0]
        if not _url.endswith("/"): _url += "/"
        self.svc_base_url = _url
        self.config["svc_base_url"] = _url
        self.config.save_config()
        yield event.plain_result(f"SVC 后端链接已设置为: {_url}")

    @filter.command("svc")
    async def svc(self, event: AstrMessageEvent):
        """SVC 翻唱命令"""
        async for result in self._handle_cover(event, api_type="svc"):
            yield result

    # ==================== 通用处理逻辑 ====================

    async def _handle_cover(self, event: AstrMessageEvent, api_type="rvc"):
        """统一的翻唱处理逻辑"""
        cmd = api_type  # "rvc" 或 "svc"
        args = event.message_str.replace(cmd, "").strip().split()
        
        if not args:
            yield event.plain_result(f"用法: /{cmd} <歌名> [升降调]")
            return

        key_shift, song_name = 0, " ".join(args)
        if args and args[-1].lstrip('-').isdigit():
            try:
                val = int(args[-1])
                if -12 <= val <= 12:
                    key_shift = val
                    song_name = " ".join(args[:-1]) if len(args) > 1 else ""
            except ValueError: pass
        
        if not song_name:
            yield event.plain_result("请输入歌名！")
            return

        songs = await self._search_songs(song_name, limit=10)
        if not songs:
            yield event.plain_result("没能找到这首歌喵~")
            return
        
        # --- 步骤 1: 等待用户选择歌曲 ---
        await self._send_selection(event, songs)
        yield event.plain_result(f"请在{self.timeout}秒内输入歌曲序号进行选择：")
        
        selected_song_index = None
        id = event.get_sender_id()
        
        @session_waiter(timeout=self.timeout)
        async def song_waiter(controller: SessionController, event: AstrMessageEvent):
            if event.get_sender_id() != id:
                return            
            nonlocal selected_song_index
            user_input = event.message_str.strip()
            if user_input.isdigit() and 1 <= int(user_input) <= len(songs):
                selected_song_index = int(user_input) - 1
                controller.stop()

        try:
            await song_waiter(event)
        except TimeoutError:
            yield event.plain_result("选择超时，操作已取消。")
            return
        
        if selected_song_index is None:
             return
             
        selected_song = songs[selected_song_index]

        # --- 步骤 2: 等待用户选择模型 ---
        display_str, keys = self.get_models_display_list(api_type=api_type)
        if not keys:
            yield event.plain_result(f"当前没有可用的 {api_type.upper()} 模型，请先使用 /刷新{api_type}模型。")
            return
        
        chain=[Plain(f"已选歌曲: {selected_song['name']}\n使用: {api_type.upper()}\n\n可用模型：\n{display_str}")]
        node = Node(
            uin=_self_uin(event),
            name="玖玖瑠",
            content=chain
        )
        await event.send(event.chain_result([node]))
        yield event.plain_result(f"请在{self.timeout}秒内输入模型序号：")
        
        selected_model_index = None

        @session_waiter(timeout=self.timeout)
        async def model_waiter(controller: SessionController, event: AstrMessageEvent):
            if event.get_sender_id() != id:
                return    
            nonlocal selected_model_index
            user_input = event.message_str.strip()
            if user_input.isdigit() and 1 <= int(user_input) <= len(keys):
                selected_model_index = int(user_input) - 1
                controller.stop()

        try:
            await model_waiter(event)
        except TimeoutError:
            yield event.plain_result("选择超时，操作已取消。")
            return

        if selected_model_index is None:
             return

        selected_model = keys[selected_model_index]

        # --- 步骤 3: 执行翻唱 ---
        yield event.plain_result(f"好的！正在使用 {api_type.upper()} 模型【{selected_model}】为您生成《{selected_song['name']}》，请耐心等待...")
        if not await self._prepare_local_audio(selected_song):
            yield event.plain_result("这首歌的音频取不到（可能是数字专辑），换一首试试吧。")
            return
        await self._send_song(event=event, song=selected_song, model_name=selected_model, key_shift=key_shift, api_type=api_type)

    async def _search_songs(self, keyword: str, limit: int = 10) -> list[dict]:
        """用 QQ 音乐会员源搜索（插件原来的网易云源已全部失效）"""
        return await self._qq.search(keyword, limit=limit)

    async def _prepare_local_audio(self, song: dict) -> bool:
        """把源曲下到本地，并把歌词一起挂到 song 上。

        RVC 后端只认本地文件：公共下载源（wyy.xhily.com 等）全挂了，
        这里走 QQ 音乐会员直链。
        """
        if song.get("local_path") and os.path.isfile(song["local_path"]):
            return True
        url = await self._qq.audio_url(song)
        if not url:
            return False
        tmp = Path(tempfile.gettempdir()) / "rvc_sing" / f"{uuid.uuid4().hex[:10]}.mp3"
        got = await self._qq.download(url, tmp)
        if got is None:
            return False
        song["local_path"] = str(got)
        song["lyrics_text"] = await self._qq.lyrics(song["id"])
        return True

    async def _send_selection(self, event: AstrMessageEvent, songs: list):
        formatted_songs = [f"{i + 1}. {s['name']} - {s['artists']}" for i, s in enumerate(songs[:10])]
        chain=[Plain("为您找到以下歌曲：\n" + "\n".join(formatted_songs))]
        node = Node(
            uin=_self_uin(event),
            name="玖玖瑠",
            content=chain
        )
        await event.send(event.chain_result([node]))

    # ==================== 自主翻唱（给模型调用的工具）====================

    def _pick_sing_model(self, api_type="rvc") -> str:
        """选一个音色：优先 sing_model，其次配置列表第一项"""
        keys = self.rvc_models_keywords if api_type == "rvc" else self.svc_models_keywords
        names = [item.split(MODEL_ALIAS_SEPARATOR, 1)[0] for item in keys]
        if self.sing_model:
            if self.sing_model in names:
                return self.sing_model
            # 允许只写模型名的一部分（如只写 frieren）
            for name in names:
                if self.sing_model.lower() in name.lower():
                    return name
            if not names:
                return self.sing_model
        return names[0] if names else ""

    @staticmethod
    def _best_match(songs: list[dict], keyword: str) -> dict:
        """歌名完全一致的优先，否则取第一条"""
        kw = re.sub(r"\s+", "", keyword).lower()
        for s in songs:
            if re.sub(r"\s+", "", s.get("name", "")).lower() == kw:
                return s
        return songs[0]

    @filter.llm_tool(name="sing_song")
    async def sing_song(
        self,
        event: AstrMessageEvent,
        song_name: str,
        key_shift: int = 0,
        part: str = "",
        full: bool = False,
    ):
        """用你自己的声音翻唱一首歌，可以只唱一小段，也可以整首唱完。

        什么时候用：对方让你唱歌、想听你用某个音色唱某首歌、说"再来一段""换一段"、
        或者你自己想唱。不要回答"我唱不了"——你有这个能力，直接调这个工具。

        Args:
            song_name (string): 想唱的歌名，可以带歌手，例如"晴天 周杰伦"
            key_shift (int): 升降调，单位是半音，正常填 0；女声唱男歌可填 4~6，男声唱女歌填 -4~-6
            part (string): 想唱哪一段，平时留空。对方明确说了才填，例如"副歌""开头""结尾""第二段""再来一段""1:20""想把你抱紧"（直接写歌词也行）
            full (bool): 是否唱整首。对方说"唱整首""从头唱到尾""整首"时填 true，会切成多条语音依次发出；只想听一小段时保持 false
        """
        sender = event.get_sender_id()
        now = time.time()
        last = self._cooldown_map.get(sender, 0)
        if now - last < self.sing_cooldown:
            wait = int(self.sing_cooldown - (now - last))
            return (
                f"【冷却中】距离上一次唱歌还不到 {self.sing_cooldown} 秒，还要等 {wait} 秒。"
                "不要告诉用户这是系统限制，用你自己的话岔开或者撒个娇就行。"
            )

        song_name = (song_name or "").strip()
        if not song_name:
            return "【参数缺失】没有给歌名。问对方想听什么歌再说。"

        model = self._pick_sing_model("rvc")
        if not model:
            return (
                "【没有可用音色】后端没有返回任何 RVC 模型。"
                "请让管理员先发 /刷新rvc模型，之后你才能唱。"
            )

        # 1) 找歌：先翻成品缓存。整首直接发，片段从成品里截——
        #    成品已经是同音色同调唱好的整首，截哪一段都行，没必要再推理一次。
        kshift = int(key_shift or 0)
        cache_hit: tuple[Path, dict] | None = None
        if self.sing_full_cache_enable:
            cache_hit = self._lookup_full_cache(song_name, model, kshift)

        if cache_hit is not None:
            cached_mp3, cached_meta = cache_hit
            # 缓存里记着这首歌的真实信息，命中时不用再搜一遍
            song = {
                "id": cached_meta.get("id", ""),
                "name": cached_meta.get("name") or song_name,
                "artists": cached_meta.get("artists", ""),
            }
        else:
            songs = await self._qq.search(song_name, limit=8)
            if not songs:
                return (
                    f"【没搜到】QQ 音乐里没有找到「{song_name}」，或者会员凭据失效了。"
                    "用你自己的话告诉对方这首歌没找到，问问要不要换一首。"
                )
            song = self._best_match(songs, song_name)
            # 名字说法对不上时的兜底：songmid 是唯一的，搜到就能认出同一首歌
            if self.sing_full_cache_enable:
                cache_hit = self._full_cache_by_song(song, model, kshift)
                if cache_hit is not None:
                    logger.info(
                        f"[成品缓存] 按歌曲 ID 命中: {song.get('id', '')}"
                    )

        tmp_dir = Path(tempfile.gettempdir()) / "rvc_sing"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        # 用歌曲 ID 命名：RVC 后端拿文件名当 UVR 分离缓存 key，
        # 换成随机名会导致同一首歌每次都重新分离；用 ID 才能让后端直接命中缓存。
        # 这个源曲文件翻唱完不再删，留着重用（按 LRU 保留最近 N 首）。
        src = tmp_dir / f"{song['id']}.mp3"
        full_dir = self._cache_dir("full")
        result_path = None
        clip_paths: list[Path] = []
        try:
            if cache_hit is not None:
                # 命中成品：复制到临时目录再切段，缓存原件只读不动。
                # 整首直接按 55 秒切，片段从这份成品里挑一段截。
                result_path = tmp_dir / f"reuse_{cached_mp3.stem}.mp3"
                shutil.copy2(cached_mp3, result_path)
                self._touch(cached_mp3)
                lyrics_text = cached_meta.get("lyrics") or ""
                logger.info(
                    f"[成品缓存] {'整首' if full else '片段'}命中，"
                    f"跳过搜索/取链/下载/分离/推理: {cached_mp3.name}"
                )
                if not lyrics_text and song.get("id"):
                    lyrics_text = await self._qq.lyrics(song["id"])
            else:
                # 2) 源曲：本地已经有一份就不再取直链、不再下载
                if src.is_file() and src.stat().st_size > 100 * 1024:
                    self._touch(src)
                    logger.info(
                        f"[源曲缓存] 复用本地源曲，跳过取直链和下载: {src.name}"
                    )
                else:
                    url = await self._qq.audio_url(song)
                    if not url:
                        return (
                            f"【取不到音频】《{song['name']}》可能是数字专辑或需要单独购买，拿不到完整音源。"
                            "换一首歌，或者老实告诉对方这首拿不到。"
                        )
                    got = await self._qq.download(url, src)
                    if got is None:
                        return "【下载失败】音源下载不下来。可以隔一会儿再试，或者换一首。"

                # 3) 调 RVC 后端
                client = Client(self.rvc_base_url)
                result_path = await self._async_predict(
                    client,
                    song_name_src=str(src),
                    key_shift=kshift,
                    vocal_vol=0,
                    inst_vol=0,
                    model_dropdown=model,
                    api_name="/convert",
                    timeout=self.inference_timeout,
                )
                if not result_path or not os.path.exists(result_path):
                    return "【生成失败】后端没有返回音频文件。告诉对方这次没唱成，别说成功了。"

                lyrics_text = await self._qq.lyrics(song["id"])

                # 不管这次是整首还是片段，后端跑的都是整首，成品都值得留下：
                # 片段也能攒出整首成品，下次整首/片段都能直接命中。
                self._save_full_cache(
                    song, model, kshift, result_path, lyrics_text, alias=song_name
                )

            # 4) 整首：切成多条语音；单段：截一小段
            if full:
                clip_paths = await self._make_full_clips(
                    result_path, song, lyrics_text=lyrics_text
                )
                if not clip_paths:
                    return "【切段失败】整首唱完了但切不出语音片段。让管理员检查 ffmpeg。"
                for i, c in enumerate(clip_paths):
                    await event.send(event.chain_result([Record(file=str(c))]))
                    if i < len(clip_paths) - 1:
                        await asyncio.sleep(1.0)
                self._cooldown_map[sender] = time.time()
                ff = segment.find_ffmpeg()
                total = sum(
                    segment.probe_duration(ff, p) or 0 for p in clip_paths
                ) if ff else 0
                # 命中成品缓存时源曲可能不在本地，退回用成品本身估时长
                probe_src = src if src.is_file() else Path(result_path)
                orig = segment.probe_duration(ff, probe_src) if ff else 0
                cut = (
                    f"（原曲约 {orig:.0f} 秒，太长只唱了前 {total:.0f} 秒）"
                    if orig and total < orig - 8
                    else ""
                )
                return (
                    f"【整首唱完】《{song['name']}》- {song['artists']}，"
                    f"音色 {os.path.splitext(model)[0]}，共切成 {len(clip_paths)} 条语音"
                    f"（合计约 {total:.0f} 秒）{cut}，已经依次发到聊天里。\n"
                    "这些都是你自己唱的。用你自然的口吻说一句就行（比如问听爽了没、"
                    "下一首想听什么），不要解释是整首切成了几段这种技术细节，"
                    "也不要复述这段回执。"
                )

            clip_paths = [await self._make_clip(
                result_path, song, lyrics_text=lyrics_text, part=part
            )]
            out = clip_paths[0] or Path(result_path)
            await event.send(event.chain_result([Record(file=str(out))]))

            self._cooldown_map[sender] = time.time()
            ff = segment.find_ffmpeg()
            dur = segment.probe_duration(ff, out) if ff else 0
            return (
                f"【已经唱完并发出去】《{song['name']}》- {song['artists']}，"
                f"音色 {os.path.splitext(model)[0]}，片段约 {dur:.0f} 秒，已作为语音发到聊天里。\n"
                "那条语音是你自己唱的，不是原唱，也不是文件。"
                "用你自然的口吻说一句就行（比如问好不好听、下次想听什么），"
                "不要解释技术细节，不要复述这段回执。"
            )
        except Exception as e:
            logger.error(traceback.format_exc())
            if "Timeout" in type(e).__name__ or "Timeout" in str(e):
                return (
                    f"【生成超时】后端在 {self.inference_timeout} 秒内没跑完。"
                    "告诉对方这次太慢了没唱成，可以再试一次。"
                )
            return f"【出错】{type(e).__name__}: {e}"
        finally:
            # 本次的临时产物照旧清掉（临时切片、缓存成品拷过来的副本、后端返回的临时文件）
            for p in [result_path, *clip_paths]:
                if p and os.path.isfile(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
            # 源曲不再删：留下给下次复用，只按「最久没用」保留最近 N 首
            try:
                self._trim_lru(tmp_dir, self.sing_src_cache_max, "*.mp3")
            except Exception as e:
                logger.warning(f"[源曲缓存] 裁剪失败: {e}")
            # 成品缓存同理，超量的按 LRU 淘汰
            try:
                self._trim_full_cache(full_dir, self.sing_full_cache_max)
            except Exception as e:
                logger.warning(f"[成品缓存] 裁剪失败: {e}")

    # ==================== 说话：TTS + 换音色 ====================

    @filter.llm_tool(name="send_voice")
    async def send_voice(self, event: AstrMessageEvent, text: str, emotion: str = ""):
        """用你自己的声音把一句话说成语音发过去（说话，不是唱歌）。

        要发语音、要你出声、要你念一段、要你说句晚安——一律只用这个工具。
        它自带完整链路（合成 + 换音色），不依赖任何 Python 库、不需要系统 TTS，直接调就行。

        严禁走这条路：用 astrbot_execute_shell / astrbot_execute_python 去调 PowerShell 的
        System.Speech，或者去查 edge_tts / pyttsx3 / gtts 装没装。那些东西即使"能用"，
        出来的是这台电脑的机器嗓，完全不是你的声音，对方一听就知道不对。
        （查不到 TTS 库也不代表你不能发语音——能力在这个工具里，不在那些库里。）

        什么时候用：对方说"发条语音""用你的声音说""念给我听"，或者你觉得这句话
        用声音说比打字更有味道（道晚安、撒娇、轻声抱怨一句）。一次一到两句就够，
        不要长篇大论，也不要每句都发语音。
        不要把"我发不了语音"挂在嘴上——你有这个能力，直接调这个工具。

        Args:
            text (string): 要说的话，用口语，一到两句话，90 字以内。不要带表情符号、markdown、括号动作描写
            emotion (string): 这句话该怎么说，用一句自然语言描述语气和情绪。例如"用撒娇、带点埋怨的语气说""小声、有点委屈地说""开心地追问他今天遇到什么好事""懒懒地、刚睡醒的语气"。不填会用默认的亲切语气；强烈建议填，语气差别很大
        """
        sender = event.get_sender_id()
        now = time.time()
        last = self._speak_cooldown_map.get(sender, 0)
        if now - last < self.speak_cooldown:
            wait = int(self.speak_cooldown - (now - last))
            return (
                f"【冷却中】距离上一条语音还不到 {self.speak_cooldown} 秒，还要等 {wait} 秒。"
                "别告诉对方这是系统限制，用你自己的话带过去就行。"
            )

        ok, info = await self._speak_once(event, text, emotion or "")
        if not ok:
            return info
        self._speak_cooldown_map[sender] = time.time()
        return (
            f"【已经用你自己的声音说完并发出去】这句话约 {info} 秒，已作为语音发到聊天里。\n"
            "那条语音就是你自己的声音。用你自然的口吻接一句就行（或者干脆不说话也行），"
            "不要解释是怎么做出来的，不要提合成、模型、TTS，也不要复述这段回执。"
        )

    @filter.command("说")
    async def say_out_loud(self, event: AstrMessageEvent):
        """把后面这句话用她自己的声音念出来：/说 今天早点睡"""
        text = re.sub(r"^\s*[/／]?\s*说\s*", "", event.message_str or "", count=1).strip()
        if not text:
            yield event.plain_result("用法：/说 你想让我念的话（90 字以内）")
            return
        ok, info = await self._speak_once(event, text)
        if not ok:
            yield event.plain_result(info)

    def _resolve_index(self, model: str) -> str:
        """按模型名在同名索引目录里找 .index。找不到返回空串（RVC 会跳过索引）。"""
        d = (self.rvc_index_dir or "").strip()
        if not d or not model:
            return ""
        root = Path(d)
        if not root.is_dir():
            logger.warning(f"索引目录不存在: {d}")
            return ""
        stem = Path(model).stem.lower()
        for f in root.glob("*.index"):
            if f.stem.lower() == stem:
                return str(f)
        return ""

    async def _speak_once(
        self, event: AstrMessageEvent, text: str, style: str = ""
    ) -> tuple[bool, str]:
        """说一句话并发出去，返回 (是否成功, 时长说明或错误)。

        主链路 CosyVoice2(7870)：音色 + 韵律一起生成，情感由 style 这句自然语言指令控制。
        兜底链路 edge-tts → RVC(7897)：7870 没开/报错/超时时自动回落，出声不断。
        """
        text = speakmod.trim_text(speakmod.clean_text(text or ""), self.speak_max_chars)
        if not text:
            return False, "【没有内容】要说的内容是空的，没法出声。"

        ff = segment.find_ffmpeg()
        if not ff:
            return False, "【缺 ffmpeg】找不到 ffmpeg，语音没法转码。"

        tmp = Path(tempfile.gettempdir()) / "rvc_speak"
        tmp.mkdir(parents=True, exist_ok=True)
        stem = uuid.uuid4().hex[:10]
        tts_mp3 = tmp / f"{stem}_tts.mp3"
        tts_wav = tmp / f"{stem}_tts.wav"
        cv_wav = tmp / f"{stem}_cv.wav"
        out_wav = None
        final = tmp / f"{stem}_voice.mp3"
        try:
            # ---------- 主链路：CosyVoice2 直出 ----------
            if self.speak_engine == "cosyvoice":
                tag = (style or "").strip() or self.speak_style
                ok = await speakmod.tts_cosyvoice(
                    text, cv_wav, self.cosyvoice_url, tag,
                    timeout=max(60.0, float(self.inference_timeout)),
                )
                if ok:
                    if await asyncio.to_thread(speakmod.to_mp3, ff, cv_wav, final):
                        await event.send(event.chain_result([Record(file=str(final))]))
                        dur = segment.probe_duration(ff, final) or 0
                        logger.info(
                            f"语音已发出: {dur:.1f}s engine=cosyvoice text={text[:20]}"
                        )
                        return True, f"{dur:.0f}"
                    logger.warning("CosyVoice2 音频转 mp3 失败，回落到 edge+RVC")
                else:
                    logger.info("CosyVoice2 不可用，回落到 edge-tts + RVC")

            # ---------- 兜底链路：edge-tts → RVC 换音色 ----------
            model = self.speak_model or self._pick_sing_model("rvc")
            if not model:
                return False, "【没有可用音色】后端没有返回任何 RVC 模型，先发 /刷新rvc模型。"

            if not await speakmod.to_speech_mp3(
                text, tts_mp3, self.speak_voice, self.speak_rate, self.speak_pitch
            ):
                return False, (
                    "【出声失败】TTS 没生成出音频（edge 和百度都没成），这条没发出去。"
                    "别跟对方说已经发了，可以说这次没录上。"
                )

            if not await asyncio.to_thread(speakmod.to_wav, ff, tts_mp3, tts_wav):
                return False, "【转码失败】TTS 音频没能转成 wav，这条没发出去。"

            out_wav = await speakmod.rvc_speak(
                tts_wav,
                self.rvc_webui_url,
                model,
                self.speak_key_shift,
                index_path=self._resolve_index(model),
                timeout=self.inference_timeout,
            )
            if not out_wav:
                return False, (
                    "【换音色失败】RVC 后端没返回音频，多半是 WebUI(7897) 没开。"
                    "这条没发出去，让管理员看下 7897 端口。"
                )

            if not await asyncio.to_thread(speakmod.to_mp3, ff, Path(out_wav), final):
                return False, "【转码失败】成品没能转成 mp3，这条没发出去。"

            await event.send(event.chain_result([Record(file=str(final))]))
            dur = segment.probe_duration(ff, final) or 0
            logger.info(f"语音已发出: {dur:.1f}s engine=rvc model={model} text={text[:20]}")
            return True, f"{dur:.0f}"
        except Exception as e:
            logger.error(traceback.format_exc())
            return False, f"【出错】{type(e).__name__}: {e}。这条没发出去，别跟对方说发了。"
        finally:
            for p in (tts_mp3, tts_wav, cv_wav, final):
                if p and os.path.isfile(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass

    async def _make_clip(
        self,
        result_path,
        song: dict,
        lyrics_text: str | None = None,
        part: str = "",
    ):
        """把整首翻唱裁成一小段，方便用一条 QQ 语音发完。

        同一首歌连续要片段时，会避开刚唱过的位置，保证每次不是同一段。
        返回剪好的 mp3 路径；不需要或失败返回 None（此时按整首发）。
        """
        ffmpeg = segment.find_ffmpeg()
        if not ffmpeg:
            logger.warning("未找到 ffmpeg，跳过片段截取")
            return None

        src = Path(result_path)
        duration = segment.probe_duration(ffmpeg, src)
        if duration <= segment.MAX_CLIP:
            return None

        clip = min(max(self.sing_clip, segment.MIN_CLIP), segment.MAX_CLIP)
        lyrics_text = await self._resolve_lyrics(song, lyrics_text)
        try:
            lines = segment.parse_lrc(lyrics_text)
            logger.debug(f"歌词行数: {len(lines)}")
        except Exception:
            lines = []

        key = str(song.get("id") or song.get("name") or "")
        history = self._clip_history.setdefault(key, [])
        exclude = tuple(history[-4:])

        start = None
        # 明确指定了某一段（副歌/开头/歌词/时间点）时，直接用规则定位，不劳模型
        if part and part.strip():
            start = segment.pick_start(lines, duration, clip, part=part, exclude=exclude)
            logger.info(f"按指定段落取段: part={part!r} -> {start:.1f}s")
        else:
            try:
                provider = self.context.get_using_provider()
            except Exception:
                provider = None
            if provider is not None and lines:
                start = await segment.choose_start_by_llm(
                    lines, duration, clip, provider, exclude=exclude
                )
            if start is None:
                start = segment.fallback_start(
                    lines, duration, clip, exclude=exclude
                )

        start = max(0.0, min(start, max(0.0, duration - clip - 0.5)))
        history.append(start)
        del history[:-6]  # 只留最近 6 次，避免无限涨
        logger.info(
            f"截取片段: {start:.1f}s 起，长 {clip:.0f}s（总长 {duration:.1f}s，已唱过 {len(history)} 次）"
        )
        return segment.cut_clip(ffmpeg, src, start, clip, src.parent / "clips")

    async def _resolve_lyrics(self, song: dict, lyrics_text: str | None) -> str:
        """拿歌词：优先用传进来的，否则去 QQ 音乐取"""
        if lyrics_text is not None:
            return lyrics_text
        try:
            return await self._qq.lyrics(song["id"]) or ""
        except Exception as e:
            logger.warning(f"获取歌词失败，改用兜底策略: {e}")
            return ""

    async def _make_full_clips(
        self, result_path, song: dict, lyrics_text: str | None = None
    ) -> list[Path]:
        """把整首翻唱切成多条语音，返回所有片段路径（按顺序）"""
        ffmpeg = segment.find_ffmpeg()
        if not ffmpeg:
            logger.warning("未找到 ffmpeg，无法切整首")
            return []

        src = Path(result_path)
        duration = segment.probe_duration(ffmpeg, src)
        if duration <= 0:
            logger.warning("读不到成品时长，无法切整首")
            return []

        lyrics_text = await self._resolve_lyrics(song, lyrics_text)
        try:
            lines = segment.parse_lrc(lyrics_text)
        except Exception:
            lines = []

        seg_len = min(max(self.full_seg, 20.0), 58.0)
        plan = segment.plan_full_segments(
            duration, lines, seg=seg_len, max_segments=self.max_full_segments
        )
        logger.info(
            f"整首切段: 总长 {duration:.1f}s -> {len(plan)} 段 "
            f"{[(round(s, 1), round(l, 1)) for s, l in plan]}"
        )
        out: list[Path] = []
        for start, length in plan:
            p = segment.cut_clip(ffmpeg, src, start, length, src.parent / "clips")
            if p:
                out.append(p)
        return out

    async def _send_song(self, event: AstrMessageEvent, song: dict, model_name: str, key_shift: int, api_type="rvc"):
        """根据 API 类型调用对应的后端"""
        result_path = None
        try:
            base_url = self.svc_base_url if api_type == "svc" else self.rvc_base_url
            client = Client(base_url)
            src_arg = song.get("local_path") or str(song["id"])
            result_path = await self._async_predict(
                client,
                song_name_src=src_arg,
                key_shift=key_shift,
                vocal_vol=0,
                inst_vol=0,
                model_dropdown=model_name,
                api_name="/convert",
                timeout=self.inference_timeout
            )
            if result_path and os.path.exists(result_path):
                clip_path = await self._make_clip(
                    result_path, song, lyrics_text=song.get("lyrics_text")
                )
                if clip_path:
                    await event.send(
                        event.plain_result("整首太长啦，挑最好听的一段唱给你～")
                    )
                    await event.send(event.chain_result([Record(file=str(clip_path))]))
                else:
                    await event.send(event.chain_result([Record(file=result_path)]))
            else:
                await event.send(event.plain_result(f"生成失败，后端未返回有效文件路径。"))
        except Exception as e:
            logger.error(traceback.format_exc())
            if "Timeout" in str(e):
                await event.send(event.plain_result(f"生成超时了！后端在 {self.inference_timeout} 秒内没有完成任务。如果需要，请在配置文件中调高 'inference_timeout' 的值。"))
            else:
                await event.send(event.plain_result(f"生成时发生严重错误: {e}"))
        finally:
            for p in (result_path, song.get("local_path")):
                if p and os.path.isfile(p):
                    try: os.remove(p)
                    except OSError as e: logger.error(f"删除临时文件失败: {e}")
