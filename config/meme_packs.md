# 表情包资源包结构

`meme_manager` 的数据在 `runtime/astrbot/data/plugin_data/meme_manager/`（不入库，图片是运行数据）。
这里记录**包与分类的设计**，换机器或重装时按本文重建骨架，图再重新放。

## 已绑定（`selection_rules.json` 的 default 规则）

```json
{ "rules": [ { "id": "default", "scope": "default",
               "pack_ids": ["ayu-001", "official-001"] } ] }
```

**顺序即优先级**：第一个是主包（默认写入目标），两者都参与提示词与发图。
改完要重载插件才生效。

| 包 | id | 用途 | 来源 |
| --- | --- | --- | --- |
| 阿鱼 | `ayu-001` | 她本人出镜的图，与社区包分开存放/备份 | 自建 |
| 官方表情包1号 | `official-001` | 通用情绪梗图，19 个分类 413 张 | 资源广场安装 |

## 阿鱼包分类（`ayu-001/memes/<分类>/`）

| 分类键 | 什么时候用 | 现有图 |
| --- | --- | --- |
| `ayu-hello` | 打招呼/自我介绍：被问「你是谁」「长什么样」，需要露出真实模样 | 1 |
| `ayu-selfie` | 日常自拍/生活照：说明自己在干嘛、在哪、状态如何 | 0 |
| `ayu-cute` | 撒娇卖萌：求关注、想要什么、被哄（≤1 次/对话） | 0 |
| `ayu-shy` | 害羞：被夸好看、被撩、被看穿 | 0 |
| `ayu-meme` | 带字梗图：自嘲、整活、锐评 | 1 |

### 形象锚定句

每个 `ayu-*` 分类的描述末尾都追加同一句外貌锚点，否则视觉模型不知道「阿鱼」是谁，
既不会选这些分类，也可能把别的角色误填进来：

> 判定同一角色的依据是发色与形体，不是服装：Q版深蓝渐变长发少女，头顶呆毛、
> 头侧鲸鱼鳍状耳、身后鲸鱼尾、蓝色大眼；换装或换画风仍算她。

- 强调**不看服装**，因为换装/换画风是常态，按女仆装判断会漏。
- 改这句要同步改全部 5 个分类，`memes_data.json` 与 `manifest.json` 两处都改。
- `ayu-hello` 里的 `ayu_hello_01.jpg` 是形象基准图（840×840）。**图片不入库**
  （角色立绘版权不明，且属运行时数据），重建时手动放回该目录即可。

分类描述写在 `ayu-001/memes_data.json` 与 `manifest.json` 的 `categories` 里，**两处要一致**。
描述是给模型看的「什么时候用」，不是给用户看的说明。

## 设计规则（踩过坑）

- **分类的粒度 = 用途的粒度**，不是画面的粒度。选图是「按描述挑分类 → 类内随机抽」，
  所以同一类里的图必须能表达同一个意思；否则随机抽出来的图会文不对题。
- **空分类不会进提示词**。`main.py` 注入前会过滤掉目录里没有任何图片的分类，
  所以新分类在放进第一张图之前，模型不知道它存在。
- **分类键不能和官方包重名**。合并映射时主包优先，同名会让两个包的同名分类互相干扰
  （发图时按名字跨包收集，会把她的图和社区图混在一起随机）。
- **和自己的通用分类划清边界**：`ayu-meme` 的描述里明确写了「纯吐槽别人用 baka，
  通用幽默用 fool」，否则它会和官方分类抢同一批场景。
- 新增/改名分类后必须重载插件；`添加分类` 之类的管理指令只作用于**主包**。

## 重建骨架

```python
import json
from pathlib import Path

pack = Path("data/plugin_data/meme_manager/packs/ayu-001")
cats = {"ayu-hello": "...", "ayu-selfie": "...", "ayu-cute": "...",
        "ayu-shy": "...", "ayu-meme": "..."}   # 每条的末尾都追加形象锚定句
for c in cats:
    (pack / "memes" / c).mkdir(parents=True, exist_ok=True)
(pack / "memes_data.json").write_text(json.dumps(cats, ensure_ascii=False, indent=2), encoding="utf-8")
manifest = json.loads((pack / "manifest.json").read_text(encoding="utf-8"))
manifest["categories"] = {k: {"description": v} for k, v in cats.items()}
(pack / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
```

还要在 `registry.json` 的 `installed_packs` 里加一条 `{"id": "ayu-001", "name": "阿鱼", "enabled": true, ...}`。
