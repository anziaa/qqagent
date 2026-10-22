# 第三方插件清单

这些插件有各自的上游仓库，**未改过源码**，所以不入库。
换机器时按下面的地址和版本用 AstrBot 插件管理装回来即可。

## 未入库（原版依赖）

| 插件 | 版本 | 作者 | 仓库 |
| --- | --- | --- | --- |
| `astrbot_plugin_proactive_chat` | v1.2.6 | DBJD-CR | <https://github.com/Pancakes-Labs/astrbot_plugin_proactive_chat> |
| `astrbot_plugin_meme_manager` | v5.2.4 | anka | <https://github.com/anka-afk/astrbot_plugin_meme_manager> |
| `astrbot_plugin_omnidraw` | v3.3.23 | 雪碧bir | <https://github.com/diaomin66/astrbot_plugin_omnidraw> |

安装方式（在 AstrBot 的 `data/plugins/` 下）：

```bash
git clone --depth 1 <仓库地址> <插件目录名>
```

- `meme_manager` 额外依赖 `boto3`（R2 图床模块顶层 import，缺了插件加载即失败）。
  装进 AstrBot 自己的环境：`D:/Anaconda_envs/envs/astrbot/python.exe -m pip install boto3`
- `meme_manager` 要求 AstrBot **>= 4.17.0**；`omnidraw` 要求 **>= 4.16, < 5**。

## 已入库（改过源码，见 components/）

下面这些上游仓库存在，但本地改动已经大到"按上游重装会丢功能"，因此按自己的代码版本控制：

| 插件 | 上游 | 入库位置 |
| --- | --- | --- |
| `astrbot_plugin_favourpro` | <https://github.com/Catfish872/astrbot_plugin_favourpro> | `components/astrbot_plugin_favourpro/` |
| `astrbot_plugin_music` | <https://github.com/Zhalslar/astrbot_plugin_music> | `components/astrbot_plugin_music/` |
| `astrbot_plugin_qzone` | <https://github.com/Zhalslar/astrbot_plugin_qzone> | `components/astrbot_plugin_qzone/` |
| `astrbot_plugin_rvc_svc` | <https://github.com/CCYellowStar2/astrbot_plugin_rvc_svc> | `components/astrbot_plugin_rvc_svc/` |

`rvc_svc` 的改动最大（说话链路换 CosyVoice2、唱歌加缓存与截段、ffmpeg PATH 探测），
基本可以视为自研。

## 判断旧插件该不该入库

```bash
# 必须带 tag，默认分支不代表发布版本
curl -sL -o up.tar.gz "https://api.github.com/repos/{owner}/{repo}/tarball/{tag}"
tar xzf up.tar.gz -C up_x --strip-components=1
diff -rq up_x "$PLUGIN_DIR" -x __pycache__ -x "*.pyc" -x ".git" -x "*.bak*" -x data
```

`*.bak_*` 是强信号（改之前留了备份），但不能只看它。
