# QQAgent

一个跑在 QQ 上的语音机器人：会聊天，会唱歌，能用自己的声音发语音条。

语音不是预设的 TTS 音色——说话走 CosyVoice2 做情感合成，唱歌走 RVC 把整首歌换成自己的音色，
最后都以 QQ 语音条的形式发出来。

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
| **主动搭话** | 通过 `astrbot_plugin_proactive_chat` 在合适时机主动发消息 |

## 架构

三个独立进程，加一个协议端：

```
                      ┌──────────────┐
   QQ ←── NapCat ────→│   AstrBot    │  :6185  机器人主体 + LLM + 插件
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
  astrbot_plugin_relation/      关系记忆插件（自研）
  astrbot_plugin_self_profile/  账号资料插件（自研）
  rvc_api/                      7860 翻唱后端（Gradio 接口）
  cosyvoice/                    7870 语音合成服务
scripts/
  launcher.py                   按顺序拉起全部服务，等端口就绪
  deploy.py                     把仓库源码同步到运行位置
  tools/                        QQ 音乐登录、人设改名等一次性脚本
config/
  *.example.json                插件配置模板（复制成 *.json 再改）
  personas/example.md           人格示例
```

`config/` 里真正生效的配置、实际使用的人格、音色参考音频都不入库——它们含 QQ 号、
私人对话和声纹，换成自己的一份就行。

标 ★ 的是**在上游基础上改过源码**的插件，所以源码直接放在本仓库里。没改动过的插件
（`proactive_chat`）不在这里，按下面的安装说明从 AstrBot 插件市场装原版即可。

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
- **唱歌链路重写**：接入自己的搜歌源，整首成品缓存 + 按歌词选段截取，只发十几秒。
- **补 ffmpeg PATH**：AstrBot 自带 ffmpeg 在子进程里找不到，显式注入路径。
- **合并转发节点不再硬编码机器人 QQ**，改为从事件取 `self_id`。

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

**5. 装插件**

本仓库里的四个插件（`rvc_svc` / `music` / `qzone` / `favourpro`）和两个自研插件
（`relation` / `self_profile`）用部署脚本同步：

```bash
# 通过环境变量指定 AstrBot 的位置
set ASTRBOT_HOME=D:\AstrBot
python scripts/deploy.py --check   # 只看差异，不写文件
python scripts/deploy.py           # 实际同步
```

没改动过的 `proactive_chat` 不用从本仓库拿，直接在 AstrBot 插件管理里按名字安装原版
（[上游地址](https://github.com/Pancakes-Labs/astrbot_plugin_proactive_chat)）。

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

## 为什么要源码同步，而不是直接跑在仓库里

组件的运行时目录各自绑死了自己的东西：RVC 后端的 cwd 里放着 `venv`、`uvr5/` 模型和
`output/`；CosyVoice2 目录里是 4.8G 权重和它自己的 venv；插件的路径还和 AstrBot 的
缓存目录相关。所以约定**仓库是源码唯一真源，运行目录只作为部署目标**，改完代码同步一次。

`deploy.py --check` 会顺带检查漂移：如果运行目录里有比仓库更新的文件，说明有人直接在
运行目录改过，脚本会警告——这时候应该先搬回仓库，否则下次同步会覆盖掉。

## 已知限制

- **首次唱歌慢**：第一次要加载 hubert、rmvpe、音色模型和索引，冷启动 1 分钟以上；
  而且当前实现是把整首歌处理完才截取片段，一首 3 分钟的歌要做完 3 分钟的活。
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
| `astrbot_plugin_proactive_chat` | DBJD-CR | 主动消息 | 未改 | [仓库](https://github.com/Pancakes-Labs/astrbot_plugin_proactive_chat) |

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
| `astrbot_plugin_relation/`、`astrbot_plugin_self_profile/` | 随本仓库 AGPL-3.0 |

转发或二次分发时请一并保留这些许可证文件与上游署名。

## 免责声明

- 翻唱涉及音乐著作权，**仅供个人学习与技术研究**，不要用于商业用途或大规模分发。
- 音色模型请使用你有权使用的声音。**不要冒用他人声音**——这可能违反法律和平台规则。
- 不得用于骚扰、诈骗或其他违法用途，使用前请遵守 QQ 平台的自动化规范。
