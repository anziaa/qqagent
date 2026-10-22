# 第三方插件清单

有上游仓库的插件分两类：**没改过源码的不入库**（换机器按地址重装即可），
**改过源码的入库**（否则重装或换机器就丢改动）。

## 未入库（原版依赖）

| 插件 | 版本 | 作者 | 仓库 |
| --- | --- | --- | --- |
| `astrbot_plugin_proactive_chat` | v1.2.6 | DBJD-CR | <https://github.com/Pancakes-Labs/astrbot_plugin_proactive_chat> |
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
| `astrbot_plugin_meme_manager` | <https://github.com/anka-afk/astrbot_plugin_meme_manager> (v5.2.4) | `components/astrbot_plugin_meme_manager/` |
| `astrbot_plugin_music` | <https://github.com/Zhalslar/astrbot_plugin_music> | `components/astrbot_plugin_music/` |
| `astrbot_plugin_qzone` | <https://github.com/Zhalslar/astrbot_plugin_qzone> | `components/astrbot_plugin_qzone/` |
| `astrbot_plugin_rvc_svc` | <https://github.com/CCYellowStar2/astrbot_plugin_rvc_svc> | `components/astrbot_plugin_rvc_svc/` |

### meme_manager 的本地改动

基线是上游 **v5.2.4**。改动文件：`mixins/event_handlers.py`、`mixins/commands.py`、
`backend/auto_collect.py`、`backend/packs/resolver.py`、`_conf_schema.json`。

1. **`/表情管理 添加表情` 存不下本地图片**（上游 bug）。
   入参图片的 `img.url` 是 AstrBot 落盘后的 Windows 路径（`D:\...\data\temp\media_*.jpg`），
   上游不判断直接 `aiohttp.get(url)`，框架抛 `NonHttpUrlClientError`，
   错误信息里反斜杠被编码成 `%5C`。现在只有 `http(s)://` 才走 HTTP，
   其余一律交给 `Image.convert_to_file_path()`（本地路径 / `file://` / base64 都能处理）。
2. **假成功提示**：全部图片保存失败时上游仍回「✅ 已经成功收录了 0 张新表情」。
   现在 0 张成功会明确报失败，部分失败会附「另有 N 张保存失败」。
3. **多包绑定下 `/添加表情` 只认主包分类**：插件读取（提示词、发图）会合并所有绑定包的
   分类，写入却只认主包，于是非主包的分类会被判成「无效类别」（顺带修了那句指错路的
   `/查看表情包` 提示）。现在校验用合并映射，并按"哪 个包声明了这个分类"把图写回那个包。
   仍然只操作主包的指令：`添加/重命名/删除/清空指定类型`——这类管理动作想动哪个包，
   先在 WebUI 里把那个包设为默认。
4. **自动收集把图片一律写进固定包**：`auto_collect.target_pack_id` 只要填了值，`submit()`
   就只加载那一个包的分类映射，模型根本看不到其它包（例如人设包）的分类，收进来的图也
   全落那个包。现在留空时会用**合并映射**识别，并按「分类由哪个包声明」落盘
   （`resolver.resolve_pack_context()` 新增 `category_packs` 返回值，`AutoCollectJob`
   带上它，`_process_job` 据此选归属包，并补一次归属包的重复检查）。
   `target_pack_id` 现在只是**兜底包**：分类无法确定（待审分类）时用它。
   填写该值的旧行为保留——固定只识别、只写入该包。

升级上游时先 clone 新版到临时目录，`diff -rq` 出差异，把上面这些改动重新打上。
运行目录里的 `.git` 是当初 `git clone` 留下的，**只用来对比上游**——不要在运行目录
`git pull` 或手改代码，那里会被 `scripts/deploy.py` 覆盖。

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
