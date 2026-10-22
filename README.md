# QQAgent

QQ 语音机器人：AstrBot 插件 + RVC 翻唱后端 + CosyVoice2 情感语音，三部分合成一个项目。

## 仓库形态：单仓（已定）

**一个项目一个仓库，不按插件拆。** 判断依据是组件之间没有版本契约：

- 插件不 `import` 后端代码，只通过 HTTP 端口（7860 / 7870）通信，没有"依赖某个版本"这回事
- 自研插件只有一个消费方——本机这个 AstrBot 实例，没有外部使用者需要独立安装
- 维护者只有我们自己，不存在独立的 issue / PR 流程

反过来，跨组件改动是常态。比如"第一次唱歌等太久"这个优化必须同时改三处才成立：

| 文件 | 改动 | 缺了会怎样 |
|---|---|---|
| `components/astrbot_plugin_rvc_svc/main.py` | 截段前置到调 `/convert` 之前 | 后端仍然跑完整首 |
| `components/rvc_api/app_rvc.py` | 加 `start_seconds` / `max_seconds`，在 UVR 之前裁 | 参数传过去被忽略 |
| `scripts/launcher.py` | 启动后打一次短音频预热 | 首次仍付冷启动成本 |

分仓的话，这个功能会有三个提交点、中间存在版本不匹配的破坏性窗口，回滚还要三仓对齐。
单仓就是一次 `git revert`。

**什么时候该拆**（出现任一条再拆，现在都不成立）：

1. RVC 后端搬到远端 GPU 机（如 `192.168.3.136`）被多个应用共用，它就从"插件下游"变成独立服务
2. 某个插件要发布给别人用，需要能从 URL 安装
3. 有第二个人独立维护某个组件

方向上也安全：`git subtree split -P components/xxx` 可以把单个组件连历史一起抽出去，
反过来把多个仓库合并、恢复交织的时间线则很难。所以先单后拆。

**AstrBot 本体不入库。** 它是运行环境，有自己的升级节奏，塞进来只会变成没人跟进的 fork。

## 组件

| 目录 | 组件 | 运行位置 |
|---|---|---|
| `components/astrbot_plugin_rvc_svc/` | 语音/翻唱插件（`sing_song` / `send_voice` 两个 LLM 工具），上游 fork 后大改 | `D:\AstrBot\data\plugins\` |
| `components/astrbot_plugin_relation/` | 关系记忆插件（记住和每个人的关系与熟悉度） | `D:\AstrBot\data\plugins\` |
| `components/astrbot_plugin_self_profile/` | 账号资料插件（查看/修改自己 QQ 资料、看对方资料卡） | `D:\AstrBot\data\plugins\` |
| `components/rvc_api/` | 7860 翻唱接口，`/convert`（UVR 分离 → RVC 推理 → 混音） | `D:\RVCSVC-API\RVCSVC-API\` |
| `components/cosyvoice/` | 7870 情感语音合成服务 | `D:\WorkBuddy\QQAgent\cosyvoice2\` |
| `scripts/` | 服务编排、启动脚本、部署脚本、诊断工具 | `D:\AstrBot\` |
| `scripts/tools/` | QQ 音乐扫码登录、人设改名、会员链路测试等一次性工具 | `D:\AstrBot\tools\` |
| `config/` | 插件配置、人设、音色参考音频、第三方插件清单 | `D:\AstrBot\` |

第三方插件（`favourpro` / `music` / `qzone` / `proactive_chat`）有自己的上游仓库，
不入库，版本与地址记在 `config/third_party_plugins.md`。

## 服务拓扑

| 服务 | 端口 | 作用 |
|---|---|---|
| AstrBot | 6185 | 机器人主体 |
| NapCat | 6099 | OneBot v11 协议端 |
| RVC WebUI | 7897 | 换音色推理（说话链路已不再经过它） |
| CosyVoice2 | 7870 | 情感语音合成，说话链路的上游 |
| RVC 转换 API | 7860 | 翻唱接口 |

- 说话：LLM → CosyVoice2(7870) → mp3 → QQ 语音条
- 唱歌：LLM 工具 `sing_song` → 7860 `/convert` → 截段 → QQ 语音条

## 改代码的流程

仓库是唯一真源，运行目录是部署目标。**不要直接在运行目录里改**，否则会和仓库漂移。

```bash
# 1. 在仓库里改
# 2. 同步到运行位置
python scripts/deploy.py

# 3. 按提示重启对应服务
# 4. 测试通过后提交
git add -A && git commit -m "..."
```

只同步某个组件：`python scripts/deploy.py plugin`（可选 `plugin` / `rvc_api` / `cosyvoice` / `scripts`）。

只检查有没有漂移、不写文件：`python scripts/deploy.py --check`。如果运行目录里有比仓库更新的文件，脚本会警告——那通常意味着你在运行目录直接改过，先搬回仓库再同步。

## 一键启动

```bash
scripts/一键启动服务.bat
```

按 `launcher.py` 里的顺序拉起全部服务，等端口 ready 后才报成功。

## 踩过的坑

- `start_astrbot.bat` 必须 `set PYTHONPATH=` 和 `set PYTHONSTARTUP=`。外部注入的 `sitecustomize.py` 会劫持 `Path.unlink` 做 safe-delete，导致 torch 加载权重解包、pip 收尾时崩掉。
- 插件目录下不能放备份文件（`_backup` 之类），AstrBot 会当插件扫描并报 `ImportError`。备份放 `D:\AstrBot\_backup\`。
- 说话链路必须给插件传 `prompt_wav` 的**路径字符串**，传 tensor 会在 `torchaudio.load` 崩。
- CosyVoice 环境固定用 `cosyvoice2\venv310`，原来的 conda 环境 `cosyvoice` 已报废。

## 不入库的东西

模型权重（CosyVoice2 4.8G、UVR 121M）、venv、`wheels/`、音频中间产物、日志、插件运行时缓存。见 `.gitignore`。
