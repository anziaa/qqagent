# 第三方插件清单

这些插件有各自的上游仓库，不是我们写的，所以不入库。换机器时按下面的地址和版本用 AstrBot 插件管理装回来。

| 插件 | 版本 | 作者 | 仓库 |
|---|---|---|---|
| `astrbot_plugin_favourpro` | v1.0.4 | 天各一方 | https://github.com/Catfish872/astrbot_plugin_favourpro |
| `astrbot_plugin_music` | v2.2.0 | Zhalslar | https://github.com/Zhalslar/astrbot_plugin_music |
| `astrbot_plugin_proactive_chat` | v1.2.6 | DBJD-CR | https://github.com/Pancakes-Labs/astrbot_plugin_proactive_chat |
| `astrbot_plugin_qzone` | v3.1.3 | Zhalslar | https://github.com/Zhalslar/astrbot_plugin_qzone |

## 例外：rvc_svc 是 fork

`astrbot_plugin_rvc_svc` 上游是 https://github.com/CCYellowStar2/astrbot_plugin_rvc_svc ，
但本地已经改得面目全非（说话链路换 CosyVoice2、唱歌加缓存和截段、补 ffmpeg PATH 等），
**直接按上游认为是别人代码会丢改动**，所以它算我们的代码，已经入库在
`components/astrbot_plugin_rvc_svc/`。

## 如果这些第三方插件被魔改过

上面四个插件如果哪一个是改过源码才用的（不是原版），那它就不该按"依赖"处理，
得挪进 `components/` 一起版本控制。判断方法：和 upstream 对应 tag 对比，
有差异就是魔改了。
