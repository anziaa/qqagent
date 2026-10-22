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
| **扩展** | 好感度、QQ 空间、点歌、主动搭话等第三方插件可叠加 |

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
  astrbot_plugin_rvc_svc/       唱歌与说话插件，提供 sing_song / send_voice 两个 LLM 工具
  astrbot_plugin_relation/      关系记忆插件
  astrbot_plugin_self_profile/  账号资料插件
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

**5. 同步到运行位置**

如果三个组件各自装在别处，用部署脚本把仓库源码推过去：

```bash
# 通过环境变量指定运行位置
set ASTRBOT_HOME=D:\AstrBot
set RVCSVC_HOME=D:\RVCSVC-API\RVCSVC-API
set COSYVOICE_HOME=D:\cosyvoice2

python scripts/deploy.py --check   # 只看差异，不写文件
python scripts/deploy.py           # 实际同步
```

也可以手动把 `components/` 下的目录拷到对应位置——`deploy.py` 只是省事的工具，不是必需品。

**6. 启动**

```bash
scripts/一键启动服务.bat
```

按 `launcher.py` 里的顺序拉起全部服务，等端口就绪后才算成功。

## 为什么要源码同步，而不是直接跑在仓库里

三个组件的运行时目录各自绑死了自己的东西：RVC 后端的 cwd 里放着 `venv`、`uvr5/` 模型和
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

本项目站在这些工作之上：

- [AstrBot](https://github.com/AstrBotDevs/AstrBot) — 机器人框架与插件体系
- [NapCat](https://github.com/NapNeko/NapCatQQ) — OneBot v11 协议端
- [RVC-Project](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI) — 音色转换
- [UVR5](https://github.com/Anjok07/ultimatevocalremovergui) — 人声分离模型
- [CosyVoice2](https://github.com/FunAudioLLM/CosyVoice) — 语音合成
- `astrbot_plugin_rvc_svc` 的上游是 [CCYellowStar2](https://github.com/CCYellowStar2/astrbot_plugin_rvc_svc)，
  本地已做大量修改（说话链路换成 CosyVoice2、唱歌加缓存与截段、音源链路重写）
- 第三方插件：`favourpro`（天各一方）、`music` / `qzone`（Zhalslar）、`proactive_chat`（DBJD-CR）

## 许可证

[AGPL-3.0](components/astrbot_plugin_rvc_svc/LICENSE)。插件部分源自 AGPL 项目，按同协议发布。

## 免责声明

- 翻唱涉及音乐著作权，**仅供个人学习与技术研究**，不要用于商业用途或大规模分发。
- 音色模型请使用你有权使用的声音。**不要冒用他人声音**——这可能违反法律和平台规则。
- 不得用于骚扰、诈骗或其他违法用途，使用前请遵守 QQ 平台的自动化规范。
