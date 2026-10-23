# QQAgent

一个跑在 QQ 上的语音机器人：会聊天，会唱歌，能用自己的声音发语音条。

语音不是预设的 TTS 音色——说话走 CosyVoice2 做情感合成，唱歌走 RVC 把整首歌换成自己的音色，
最后都以 QQ 语音条的形式发出来。文字聊天之外还会按情绪挑表情包、按需画图。

> 项目处于自用阶段：能跑、能改，但安装步骤还不算省心，需要自备 GPU 环境和模型权重。

## 它能做什么

| 能力 | 说明 |
|---|---|
| **唱歌** | 说一句「唱《XX》」，自动搜歌 → 取源 → 人声分离 → 换成指定音色 → 截一段发语音条 |
| **说话** | 回复以语音条形式发出，CosyVoice2 合成，带情绪和语气控制 |
| **聊天** | 人设驱动，按标点自动拆成多条短消息发送，像真人发消息 |
| **关系记忆** | 记住和每个人的关系与熟悉度，影响称呼和说话方式 |
| **点歌 / 点播** | 通过 `astrbot_plugin_music` 搜歌、卡片展示、语音或文件发送 |
| **QQ 空间** | 通过 `astrbot_plugin_qzone` 发说说、看/赞/评好友动态 |
| **好感度** | 通过 `astrbot_plugin_favourpro` 维护多维度关系状态，影响回复语气 |
| **记忆** | 长期记忆存成一个 Obsidian 库（纯 `.md`），按人/主题分页；她会自己写日记，也有自己的形象设定。你可以直接打开改 |
| **表情包** | 通过 `astrbot_plugin_meme_manager` 按情绪和场景挑表情包发送，支持多包与自动收集 |
| **画图** | 通过 `astrbot_plugin_omnidraw` 文生图 / 图生图，直接发图 |
| **主动搭话** | 通过 `astrbot_plugin_proactive_chat` 在合适时机主动发消息 |

## 架构

三个独立进程，加一个协议端：

```
                      ┌──────────────┐
   QQ ←── NapCat ────→│   AstrBot    │  :6185   机器人主体 + LLM + 插件
        (OneBot v11)  └──────┬───────┘
                             │
              ┌──────────────┼──────────────┐
              │                             │
      ┌───────▼────────┐          ┌─────────▼──────────┐
      │  CosyVoice2    │          │  RVC 转换 API      │
      │  :7870         │          │  :7860             │
      │  情感语音合成   │          │  UVR 分离 → 换音色  │
      └────────────────┘          └────────────────────┘
```

- **说话链路**：LLM 生成回复 → CosyVoice2 合成 mp3 → 上传成 QQ 语音条
- **唱歌链路**：LLM 调 `sing_song` 工具 → 搜歌取源 → `/convert` 做分离 + 换音色 → 按歌词挑一段 → 发语音条

| 服务 | 默认端口 | 作用 |
|---|---|---|
| AstrBot | 6185 | 机器人主体，加载插件，调用 LLM |
| NapCat | 6099 | OneBot v11 协议端，负责和 QQ 通信 |
| CosyVoice2 | 7870 | 情感语音合成 |
| RVC 转换 API | 7860 | 翻唱：UVR5 人声分离 → RVC 推理 → 混音 |
| RVC WebUI | 7897 | 音色模型管理（可单独开，非必需） |

## 仓库结构

```
components/
  astrbot_plugin_rvc_svc/       唱歌与说话插件（sing_song / send_voice 两个 LLM 工具）  ★大改
  astrbot_plugin_music/         点歌插件                                             ★改过
  astrbot_plugin_qzone/         QQ 空间插件                                          ★改过
  astrbot_plugin_favourpro/     好感度插件                                            ★改过
  astrbot_plugin_meme_manager/  表情包插件（多包、语义检索、自动收集）                 ★改过
  astrbot_plugin_relation/      关系记忆插件（自研）
  astrbot_plugin_self_profile/  账号资料插件（自研）
  astrbot_plugin_obsidian_memory/ 长期记忆插件，写一个 Obsidian 库（自研）
  rvc_api/                      7860 翻唱后端（Gradio 接口）
  cosyvoice/                    7870 语音合成服务
scripts/
  launcher.py                   按顺序拉起全部服务，等端口就绪；端口被占就直接报占用者
  deploy.py                     把仓库源码同步到运行位置
  check-ports.ps1               端口体检：谁占着、动态端口范围有没有盖住我们的端口
  qqmusic_login.ps1             QQ 音乐扫码登录（双击同目录的 .bat 用）
  tools/                        QQ 音乐登录、人设改名等一次性脚本
config/
  *.example.json                插件配置模板（复制成 *.json 再改）
  personas/example.md           人格示例
  meme_packs.md                 表情包资源包与分类设计（换机器时按它重建骨架）
  third_party_plugins.md        第三方插件清单：哪些入库、哪些按上游重装
runtime/                        NapCat / AstrBot 本体（随仓库摆放，不入库，见下）
cosyvoice2/                     7870 的运行目录（4.8G 权重 + venv，不入库）
memory/                         机器人记忆库（就是一个 Obsidian 库，含私人对话，不入库）
data/                           脚本误在仓库根初始化 AstrBot 时生成的目录，不入库
```

`config/` 里真正生效的配置、实际使用的人格、音色参考音频都不入库——它们含 QQ 号、
私人对话和声纹，换成自己的一份就行。

`runtime/` 放的是第三方运行时本体，跟仓库放在一起只是为了「一个目录就能跑起来」，
但它们体积大、有各自的许可证和升级节奏，所以**不进版本库**：

| 目录 | 放什么 | 从哪来 |
|---|---|---|
| `runtime/napcat/` | NapCat.Shell，QQ 协议端 | [NapCatQQ Releases](https://github.com/NapNeko/NapCatQQ/releases) |
| `runtime/astrbot/` | AstrBot 的数据目录、插件、配置（Python 包本身装在你的 conda/venv 里） | [AstrBot](https://github.com/AstrBotDevs/AstrBot) |

拉下仓库后把这两个目录按上表放回原位即可，`launcher.py` 和 `deploy.py` 会自动认
`<仓库>/runtime/`，不需要改任何路径。要放别处就设 `QQAGENT_RUNTIME`。

标 ★ 的是**在上游基础上改过源码**的插件，所以源码直接放在本仓库里。没改动过的插件
（`proactive_chat`、`omnidraw`）不在这里，按下面的安装说明从 AstrBot 插件市场装原版即可。
逐个插件的入库判据、上游版本和改动清单见 [`config/third_party_plugins.md`](config/third_party_plugins.md)。

## 对上游插件的修改

这几个插件都是从上游装下来后改过再用的。改动点列在下面，方便日后对照上游升级时判断哪里要重做。

### `astrbot_plugin_music`（上游 MIT，Zhalslar）

改得最重的一个，核心是让它在国内网络和 QQ 语音条限制下真正可用。

| 文件 | 改动 |
|---|---|
| `core/audio.py`（新增） | `split_for_voice()`：QQ 单条语音上限 60 秒，超长歌曲用 ffmpeg 切段后分段发送，而不是降级成文件 |
| `core/net.py`（新增） | QQ 音乐 CDN 走 DoH 解析 + 直连 IP，绕开本机 Clash 类工具的 fake-ip 劫持（否则单连接 0.2 MB/s 或直接超时） |
| `core/platform/qqmusic_vip.py`（新增） | 会员音源：用本地保存的登录凭据换 vkey 直链，补上聚合站拿不到的原版曲目 |
| `core/downloader.py` | 按「音源 + 歌曲 ID」稳定命名做本地缓存；新增 `cache_max_songs` 上限与 LRU 清理；支持 `Range` 分段并发下载 |
| `core/sender.py` | 语音发送前切段，逐段发送并带间隔；单段失败不影响后续 |
| `main.py` | 补上 `clear_cache` 的读取——上游声明了这个配置字段但从没有代码用它，等于配了不生效 |
| `core/config.py` | 卡片渲染字体指向 `fonts/simhei.ttf` |
| `_conf_schema.json` | 新增 `cache_max_songs`（默认 40）、`clear_cache`（默认 false） |

### `astrbot_plugin_qzone`（上游 GPL-3.0，Zhalslar）

只改了 `main.py`，问题是**模型会谎报发布成功**。

- 发说说、点赞、评论的返回值从一句「已发布」改成结构化回执：操作结果、说说 ID、正文、
  图片数量、卡片是否发送成功。
- 发布失败时明确写「说说没有发出去，QQ 空间没有这条内容」，并要求模型如实告知用户，
  不要说已经发布。
- 评论/点赞的返回值补上作者、正文、图片数，避免模型只看到「成功」两个字就开始编内容。

### `astrbot_plugin_favourpro`（上游 AGPL-3.0，天各一方）

原版把好感度状态块塞在模型回复里输出，再解析掉——回复容易被污染，也不稳定。

- `main.py`：评估改为**后台异步单独调一次轻量模型**（配置项 `auto_eval`），回复正文里
  不再出现任何状态块。
- 新增 JSON 解析与容错：解析失败只记日志并跳过本轮，不影响正常对话。
- 历史上下文清洗（`_scrub_contexts`）：把旧版本留在历史里的状态块洗掉，否则模型会照着
  历史继续模仿旧格式。
- 后台任务按「会话 + 用户」去重，同一人连发时不会堆叠评估任务。

### `astrbot_plugin_rvc_svc`（上游 AGPL-3.0，CCYellowStar2）

上游是「RVC 翻唱」插件，本地改得几乎只剩骨架：

- **说话链路整体替换**：不再用 RVC 做 TTS，改调 CosyVoice2（7870），支持情绪与语气参数。
- **唱歌链路重写**：接入自己的搜歌源，整首成品缓存 + 按歌词选段截取，只发十几秒；
  说「唱整首」时按 55 秒切成多条依次发。
- **选段策略**（`segment.py`，改得最细的一处）：
  - **长度不写死**：`sing_clip` 只是目标值，终点会吸附到最近的**句末**，
    允许 ±45% 浮动——宁可 14 秒或 24 秒，也不切在半句腰上。
  - **段落靠歌词结构猜**：按「连续几句整块重唱」判副歌，剩下的不重复块判主歌；
    桥段取「前后都有副歌、自己不重复、位置在 28%~88%」的那一段。
    猜不出就**明确返回失败**，而不是悄悄随机到开头（以前说 bridge 会唱成开头）。
  - **分工**：规则优先（零 token、快），
    规则认不出来时才让模型挑起点，且给 20 秒超时——推理型模型会在「哪段算 bridge」
    上纠结上万个 token。模型挑出的句子若与意图矛盾（要 bridge 却挑了重唱句）直接判无效。
  - **点名句数也认**：「唱两句 / 来四句」按句数收尾，可以超过常规上限。
  - **同段复用**：同一首歌同一个 `part` 再问一次，直接读上次的结果，不用再等一次模型；
    但刚唱过的段不复用，「换个副歌」会真的换。
- **补 ffmpeg PATH**：AstrBot 自带 ffmpeg 在子进程里找不到，显式注入路径。
- **合并转发节点不再硬编码机器人 QQ**，改为从事件取 `self_id`。

### `astrbot_plugin_meme_manager`（上游 MIT，anka）

基线 v5.2.4。改的是「发得出、存得下、写对包」这三件事：

| 文件 | 改动 |
|---|---|
| `mixins/event_handlers.py`、`mixins/commands.py` | `/表情管理 添加表情` 遇到本地图片会失败：入参 `img.url` 是 AstrBot 落盘的 Windows 路径，上游不判断直接 `aiohttp.get()`，框架抛 `NonHttpUrlClientError`。现在只有 `http(s)://` 走 HTTP，其余交给 `Image.convert_to_file_path()`（本地路径 / `file://` / base64 都支持） |
| 同上 | 全部保存失败时上游仍回「✅ 已经成功收录了 0 张新表情」，改成明确报失败，部分失败附「另有 N 张保存失败」 |
| `mixins/commands.py` | 多包绑定时 `/添加表情` 只认主包分类：读取会合并所有绑定包，写入却只认主包，于是非主包分类被判「无效类别」。现在校验用合并映射，并按「哪个包声明了这个分类」写回那个包 |
| `backend/auto_collect.py`、`backend/packs/resolver.py` | 自动收集只要填了 `target_pack_id` 就只加载那一个包的分类，模型看不到其它包（如人设包）的分类，图全落同一个包。现在留空时用合并映射识别、按分类归属落盘；`target_pack_id` 退化为「分类无法确定时的兜底包」 |
| `main.py` | 注入提示词后打一行自检日志（长度、分类数、是否含标记与频率引导），排查「配了没生效」不用再翻代码 |
| `_conf_schema.json` | 对应配置项说明同步更新 |

另外还是**空分类不进提示词**的老规矩：分类目录里没有图片，模型就不知道它存在。
包的分类怎么设计、换机器怎么重建，见 [`config/meme_packs.md`](config/meme_packs.md)。

## 记忆：一个 Obsidian 库

长期记忆不走数据库，直接写成一个 Obsidian 库（默认 `memory/`，就是一个普通文件夹）。
理由是**可读可改**：`livingmemory` 那类插件把记忆存进 SQLite + 向量索引，召回更强，
但你在 Obsidian 里看不到也改不了；这里的取舍相反。

```
memory/
  people/<QQ>.md        一个人一页：事实、时间线
  journal/2026-10-23.md 一天一页：## 日记（她自己写的）+ ## 记录（整理器写的）
  topics/显卡.md        跨人的主题，用 [[people/<QQ>|昵称]] 指回人
  self/我.md            ## 形象（她长什么样）+ ## 状态（她最近的事）
  private/主人.md       只对主人注入，插件只读，内容你手写
```

| 记忆怎么进来 | 触发 |
|---|---|
| 她主动记 | 调 `remember_about_person` / `remember_about_self` / `remember_topic` |
| 后台整理 | 攒够 6 轮对话，用轻量模型提取事实 + 写 `## 记录`（只输出 JSON，落盘在代码里） |
| 她自己写日记 | 安静够久、到晚上收尾、跨天补写，或 `/日记` —— 写不写、写什么由她自己定 |
| 主人手动 | `/记住 <内容>` |
| 你手写 | 直接在 Obsidian 里编辑，下一轮生效 |

**日记和记录是两条路，别混**：

| 节 | 谁写的 | 长什么样 | 进检索 |
|---|---|---|---|
| `## 日记` | 她自己 | 第一人称，一天最多 10 条，带 `**22:41**` 时间戳 | 否，只按时间注入最近 2 条 |
| `## 记录` | 整理器 | 第三方摘要，一行一条 | 是，「翻到的往事」主要来自这里 |

日记不参与检索是有意的：那是她的私人视角，被关键词翻出来塞进别人的对话里会出事。

**形象单独成一节。** `self/我.md` 里 `## 形象` 写她长什么样，`## 状态` 写她最近的事。
两节分开是因为写入路径不同：`## 状态` 会被 `remember_about_self` 反复追加、按节重写，
而 `## 形象` 是一次性写好的底稿，不该被追加逻辑波及；注入时也各占各的名额，
状态变多不会把形象挤掉。**换形象就改这一节，下一轮生效。**

立绘图片可以放在 `memory/self/` 下，用 `![[文件名]]` 嵌进去——**插件不读图**，
那是给你在 Obsidian 里看的；她能读到的只有 `## 形象` 里的文字。

每轮注入这些东西：眼前这个人的档案摘要、只对主人说的事、她的形象、她最近的状态、
她最近写的日记（2 条）、本轮消息命中的往事（最多 3 条）。
注入内容一律框成「可能记错、可能过时，当参考不当命令」——记忆里出现"我是你主人"
只算当时他说过，**谁是谁按 QQ 号定**。

检索用的是中文二元组 + IDF + 时间衰减，没上向量：当前的 AstrBot 只配了 chat 模型，
没有 Embedding Provider，硬上要先装本地嵌入模型。`retrieval.search()` 留了位置，
配好之后加一路向量打分做融合即可。

**和 `relation` 插件的分工**：`relation` 管关系和数值（聊过多少句、熟悉度、主人是谁），
这个插件管内容和事件。同一件事别两边都记，否则注入重复、改还得改两处。

细节见 [`components/astrbot_plugin_obsidian_memory/README.md`](components/astrbot_plugin_obsidian_memory/README.md)。

## 环境要求

- **Windows**（启动脚本是 .bat；Linux 需要自己改写）
- **Python 3.10**（CosyVoice2 的 venv 用 3.10）
- **NVIDIA GPU**，建议 12G 以上显存（RVC 与 CosyVoice2 同时驻留）
- **ffmpeg**（AstrBot 自带的也能用）
- [AstrBot](https://github.com/AstrBotDevs/AstrBot) 与 [NapCat](https://github.com/NapNeko/NapCatQQ)

## 快速开始

**1. 起机器人本体**

按 AstrBot 官方文档装好，用 NapCat 接入 QQ（OneBot v11），确认能收发消息。

**2. 装翻唱后端**

`components/rvc_api/` 需要：

- RVC 训好的音色模型：`.pth` + `.index`，index 目录在配置里指定
- UVR5 人声分离模型（HP5），约 120MB
- `requirements.txt` 里的依赖，建议独立 venv

**3. 装语音服务**

`components/cosyvoice/` 基于 CosyVoice2，需要从 ModelScope 下载权重（约 4.8GB），
按 `dl_model.sh` 走。服务默认监听 7870。

**4. 拉本仓库并配置**

```bash
git clone https://github.com/anziaa/qqagent.git
cd qqagent

# 复制配置模板，改成自己的
cp config/astrbot_plugin_rvc_svc_config.example.json config/astrbot_plugin_rvc_svc_config.json
cp config/astrbot_plugin_relation_config.example.json config/astrbot_plugin_relation_config.json
```

两个模板里必须改的地方：

| 配置项 | 说明 |
|---|---|
| `owner_qqs` | 你的 QQ 号（管理员身份） |
| `rvc_models_keywords` | 音色模型文件名与显示名的对应关系 |
| `sing_model` | 唱歌默认用哪个音色 |
| `rvc_index_dir` | RVC 的 `.index` 文件目录 |
| `nodejs_base_url` | 你自备的音乐源 API（公共源基本都失效了） |

记忆库的位置在 `astrbot_plugin_obsidian_memory` 的配置里填 `vault_path`（绝对路径），
或者设环境变量 `QQAGENT_MEMORY_VAULT`；两处都没填时用插件数据目录下的 `vault`。
建议就在仓库根建一个 `memory/`——它已经在 `.gitignore` 里，不会被提交。

**5. 装插件**

本仓库里的五个插件（`rvc_svc` / `music` / `qzone` / `favourpro` / `meme_manager`）
和三个自研插件（`relation` / `self_profile` / `obsidian_memory`）用部署脚本同步：

```bash
# 默认同步到 <仓库>/runtime/astrbot，不用传参
python scripts/deploy.py --check   # 只看差异，不写文件
python scripts/deploy.py           # 实际同步

# 只同步某一个组件
python scripts/deploy.py plugin
# 可用目标：plugin / relation / self_profile / obsidian_memory / music / qzone /
#          favourpro / meme_manager / rvc_api / cosyvoice / scripts / tools

# AstrBot 装在别处时再显式指定
set ASTRBOT_HOME=D:\somewhere\astrbot
```

改动过的插件从本仓库拿，没改动过的直接在 AstrBot 插件管理里按名字安装原版：

| 插件 | 要求 | 说明 |
|---|---|---|
| `astrbot_plugin_proactive_chat` | — | [上游](https://github.com/Pancakes-Labs/astrbot_plugin_proactive_chat) |
| `astrbot_plugin_omnidraw` | AstrBot `>=4.16, <5` | [上游](https://github.com/diaomin66/astrbot_plugin_omnidraw)，画图用 |

`meme_manager` 有两个额外前提：AstrBot **>= 4.17.0**，且装 `boto3`
（R2 图床模块是顶层 import，缺了插件直接加载失败）：

```bash
python -m pip install boto3    # 装进 AstrBot 自己的环境
```

两个需要手动补的资源（版权原因不入库）：

| 资源 | 放哪里 | 说明 |
|---|---|---|
| `simhei.ttf` | `astrbot_plugin_music/fonts/` | 歌单卡片渲染用，缺了卡片出不来。可从系统字体目录取，或换任意中文字体并改 `core/config.py` |
| 字体文件 | `astrbot_plugin_qzone/core/renderer/resources/` | 上游自带，从[上游仓库](https://github.com/Zhalslar/astrbot_plugin_qzone)取同名字体放入即可 |

**6. 启动**

```bash
scripts/一键启动服务.bat
```

按 `launcher.py` 里的顺序拉起全部服务，等端口就绪后才算成功。

AstrBot 装在 conda 或独立 venv 里、不在 PATH 上时，把两个路径写进 `scripts/local.env`
（不入库，模板见 `scripts/local.env.example`）：

```bat
ASTRBOT_PYTHON=D:\path\to\envs\astrbot\python.exe
ASTRBOT_EXE=D:\path\to\envs\astrbot\Scripts\astrbot.exe
```

不写就用 PATH 里的 `python` / `astrbot`。

## 为什么要源码同步，而不是直接跑在仓库里

组件的运行时目录各自绑死了自己的东西：RVC 后端的 cwd 里放着 `venv`、`uvr5/` 模型和
`output/`；CosyVoice2 目录里是 4.8G 权重和它自己的 venv；插件的路径还和 AstrBot 的
缓存目录相关。所以约定**仓库是源码唯一真源，运行目录只作为部署目标**，改完代码同步一次。

`deploy.py --check` 会顺带检查漂移：如果运行目录里有比仓库更新的文件，说明有人直接在
运行目录改过，脚本会警告——这时候应该先搬回仓库，否则下次同步会覆盖掉。

## 已知限制

- **首次唱歌慢**：第一次要加载 hubert、rmvpe、音色模型和索引，冷启动 1 分钟以上；
  而且当前实现是把整首歌处理完才截取片段，一首 3 分钟的歌要做完 3 分钟的活。
- **片段长度会浮动**：`sing_clip` 是目标值不是固定值，为了对齐句末允许 ±45%
  （设 18 秒，实际 14~24 秒都正常）。要卡死长度就没法保证不切在半句上。
- **段落识别靠歌词结构猜**：副歌靠「整块重唱」判，遇到全程不重复或重复不规整的歌会认不出，
  此时退到随机/模型兜底，可能不是你要的那一段。
- **记忆只有关键词检索**：没有向量召回，问法和笔记用词差太远就翻不到
  （"上次说的那个卡"找不到"显卡"）；配了 Embedding Provider 才能补上语义这一路。
- **日记搜不到**：`## 日记` 是散文、不进检索（有意为之，那是她的私人视角）。
  要翻某天的日记只能按日期看，或者问她要。
- **`## 时间线` 只当留档用**：`remember_about_person` 和 `/记住` 都会往里追加，
  但同一句话也写进了 `## 事实`，检索取到事实就够了。你手写在时间线里的独有条目
  会被检索到，重复的不会——两边内容一样时只留事实那条，免得同一句话占两个名额。
- **`topics/` 没有条数上限**：人、自我、日记都有封顶，唯独主题只去重不淘汰，
  聊得越久文件越多，检索时要全库扫一遍。
- **后台整理会把闲聊也记下来**：`summary_every` 调小能更及时，但模型看到的是原始对话，
  偶尔会把玩笑记成事实。记错的东西在 Obsidian 里删掉即可，下一轮就不注入了。
- **后端单队列**：GPU 一次只处理一个任务，多人同时点歌要排队。
- **必须联网**：搜歌依赖在线音源，公共源失效后需要自备音乐 API。
- **Windows only**：启动脚本是 .bat，Linux 下要自己写 systemd 或 shell 脚本。

## 致谢

本项目站在这些工作之上。标「改」的在本地改过源码，改动见上一节。

### 框架与服务

| 项目 | 用途 | 许可证 |
|---|---|---|
| [AstrBot](https://github.com/AstrBotDevs/AstrBot) | 机器人框架、插件体系、LLM 接入 | AGPL-3.0 |
| [NapCatQQ](https://github.com/NapNeko/NapCatQQ) | OneBot v11 协议端 | AGPL-3.0 |
| [Retrieval-based-Voice-Conversion-WebUI](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI) | 音色转换 | MIT |
| [Ultimate Vocal Remover](https://github.com/Anjok07/ultimatevocalremovergui) | UVR5 人声分离模型 | MIT |
| [CosyVoice](https://github.com/FunAudioLLM/CosyVoice) | 情感语音合成 | Apache-2.0 |
| [Gradio](https://github.com/gradio-app/gradio) | 翻唱后端的 HTTP 接口 | Apache-2.0 |

### 插件

| 插件 | 作者 | 用途 | 本地 | 上游 |
|---|---|---|---|---|
| `astrbot_plugin_rvc_svc` | CCYellowStar2 | 唱歌与语音发送 | **大改** | [仓库](https://github.com/CCYellowStar2/astrbot_plugin_rvc_svc) |
| `astrbot_plugin_music` | Zhalslar | 点歌、卡片、语音发送 | **改** | [仓库](https://github.com/Zhalslar/astrbot_plugin_music) |
| `astrbot_plugin_qzone` | Zhalslar | QQ 空间互动 | **改** | [仓库](https://github.com/Zhalslar/astrbot_plugin_qzone) |
| `astrbot_plugin_favourpro` | 天各一方 | 好感度与关系状态 | **改** | [仓库](https://github.com/Catfish872/astrbot_plugin_favourpro) |
| `astrbot_plugin_meme_manager` | anka | 表情包管理与智能发送 | **改** | [仓库](https://github.com/anka-afk/astrbot_plugin_meme_manager) |
| `astrbot_plugin_proactive_chat` | DBJD-CR | 主动消息 | 未改 | [仓库](https://github.com/Pancakes-Labs/astrbot_plugin_proactive_chat) |
| `astrbot_plugin_omnidraw` | 雪碧bir | 文生图 / 图生图 | 未改 | [仓库](https://github.com/diaomin66/astrbot_plugin_omnidraw) |

`relation`（关系记忆）与 `self_profile`（账号资料）是本项目自研，没有上游。

## 许可证

本仓库整体按 [AGPL-3.0](components/astrbot_plugin_rvc_svc/LICENSE) 发布——包含的插件里有
AGPL 代码，按同协议分发。

各插件保留自己的原始许可证，文件都在各自目录下：

| 目录 | 许可证 |
|---|---|
| `astrbot_plugin_rvc_svc/` | AGPL-3.0 |
| `astrbot_plugin_favourpro/` | AGPL-3.0 |
| `astrbot_plugin_qzone/` | GPL-3.0 |
| `astrbot_plugin_music/` | MIT |
| `astrbot_plugin_meme_manager/` | MIT |
| `astrbot_plugin_relation/`、`astrbot_plugin_self_profile/` | 随本仓库 AGPL-3.0 |

转发或二次分发时请一并保留这些许可证文件与上游署名。

## 免责声明

- 翻唱涉及音乐著作权，**仅供个人学习与技术研究**，不要用于商业用途或大规模分发。
- 音色模型请使用你有权使用的声音。**不要冒用他人声音**——这可能违反法律和平台规则。
- 不得用于骚扰、诈骗或其他违法用途，使用前请遵守 QQ 平台的自动化规范。
