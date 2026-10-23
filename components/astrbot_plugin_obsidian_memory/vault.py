# -*- coding: utf-8 -*-
"""Obsidian 记忆库的读写层。

vault 是唯一真源：这里只按固定格式读写 .md，不建数据库、不存第二份。

三条约定，改之前先想清楚：

1. **原子写**。先写 `.tmp` 再 `os.replace`，Obsidian 那边永远不会读到半截文件；
   写到一半崩了也只是留下一个 .tmp，原文件还是完整的。
2. **只动自己管的段落**。人格/事件写进 `## 事实`、`## 时间线`、`## 记录`，
   你在 Obsidian 里加的其它标题、段落、callout，重写时原样保留。
3. **路径不出 vault**。分类目录写死，文件名走 `_safe()` 清洗，
   不会因为一个奇怪的分组名跑到库外面去。
"""

from __future__ import annotations

import os
import re
from collections import OrderedDict
from datetime import date, datetime
from pathlib import Path

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)
_UNSAFE = re.compile(r"[^\w\u4e00-\u9fff.-]+")
_WS = re.compile(r"\s+")

FACT_SECTION = "事实"
TIMELINE_SECTION = "时间线"
JOURNAL_SECTION = "记录"
DIARY_SECTION = "日记"
SELF_SECTION = "状态"

# 记录里的一行长这样：`11:07 · [[people/10001|小明]]：聊了…`
_JOURNAL_TIME = re.compile(r"^\d{1,2}:\d{2}\s*·\s*")
_JOURNAL_LINK = re.compile(r"^\[\[[^\]]*\]\]\s*[：:]?\s*")


def _safe(name: str, fallback: str = "note", limit: int = 60) -> str:
    out = _UNSAFE.sub("_", (name or "").strip()).strip("._")
    return (out or fallback)[:limit]


def _clean(text: str, limit: int = 120) -> str:
    return _WS.sub(" ", (text or "").replace("\r", " ")).strip()[:limit]


def _prose(text: str, limit: int = 300) -> str:
    """清洗散文（日记用），和 `_clean` 的区别是**保留换行**。

    日记的质感在换行上，用 `_clean` 会把两段压成一行。顺手削掉模型爱加的
    列表符号和粗体：日记里出现 `- ` 还会被检索当条目收走，那是另一套东西。
    """
    out: list[str] = []
    for raw in (text or "").replace("\r", "").split("\n"):
        line = raw.strip()
        if not line:
            if out and out[-1] != "":
                out.append("")
            continue
        line = re.sub(r"^[-*+]\s+", "", line)
        line = line.replace("**", "").strip()
        if line:
            out.append(line)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out).strip()[:limit]


def diary_blocks(raw: str) -> list[str]:
    """按 `**HH:MM**` 切日记条目。

    特意不按空行切：一条日记里她自己分段是很正常的
    （"今天有点累。\n\n但他没催我。"），按空行切会把一条变成两条，
    读回来就多了一笔根本不存在的记录。时间戳才是真正的分隔符。
    """
    text = (raw or "").strip()
    if not text:
        return []
    parts = re.split(r"(?m)^\*\*\d{1,2}:\d{2}\*\*[ \t]*$", text)
    out: list[str] = []
    # 第一段可能是不带时间戳的手写内容，也算一条
    if parts and parts[0].strip():
        out.append(parts[0].strip())
    for part in parts[1:]:
        part = part.strip()
        if part:
            out.append(part)
    return out


def _unquote(value: str) -> str:
    value = (value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def atomic_write(path: Path, text: str) -> None:
    """写文件只有一个入口：临时文件 + 原子替换。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text if text.endswith("\n") else text + "\n")
    os.replace(tmp, path)


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    m = _FM_RE.match(text or "")
    if not m:
        return {}, text or ""
    fm: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        key, _, value = line.partition(":")
        fm[key.strip()] = _unquote(value.strip())
    return fm, (text or "")[m.end():]


def split_sections(body: str) -> tuple[str, "OrderedDict[str, str]"]:
    """按 `## 标题` 切段，返回 (标题行, {标题: 段落正文})。

    没有 `##` 的文件会得到空 dict，标题行是整段内容——调用方自己兜底。
    """
    parts = re.split(r"(?m)^## +(.+?)\s*$", body or "")
    head = (parts[0] or "").strip("\n")
    sections: "OrderedDict[str, str]" = OrderedDict()
    for i in range(1, len(parts) - 1, 2):
        sections[parts[i].strip()] = (parts[i + 1] or "").strip("\n")
    return head, sections


def build_doc(fm: dict[str, str], head: str, sections: "OrderedDict[str, str]") -> str:
    lines = ["---"]
    for key, value in fm.items():
        lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append("")
    if (head or "").strip():
        lines.append(head.strip())
        lines.append("")
    for title, body in sections.items():
        lines.append(f"## {title}")
        lines.append((body or "").strip("\n"))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def bullets(text: str) -> list[str]:
    """取段落里的 `- xxx` 列表项，其它行忽略。"""
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith(("- ", "* ")):
            item = line[2:].strip()
            if item and not item.startswith("_("):
                out.append(item)
    return out


def strip_date(item: str) -> str:
    """把 `2026-10-20 · 内容` 里的日期前缀去掉，只给提示词看内容。"""
    return re.sub(r"^\d{4}-\d{2}-\d{2}\s*[·:]\s*", "", item or "").strip()


class Vault:
    KINDS = ("people", "journal", "topics", "self", "private")

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        for kind in self.KINDS:
            (self.root / kind).mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, tuple[float, int, dict]] = {}

    # ==================== 路径 ====================

    def person_file(self, qq: str) -> Path:
        return self.root / "people" / f"{_safe(str(qq), 'unknown', 20)}.md"

    def journal_file(self, day: date) -> Path:
        return self.root / "journal" / f"{day:%Y-%m-%d}.md"

    def topic_file(self, topic: str) -> Path:
        return self.root / "topics" / f"{_safe(topic)}.md"

    def self_file(self) -> Path:
        return self.root / "self" / "我.md"

    def private_file(self) -> Path:
        return self.root / "private" / "主人.md"

    # ==================== 人 ====================

    def load_person(self, qq: str) -> dict:
        path = self.person_file(qq)
        if not path.is_file():
            return {}
        fm, body = split_frontmatter(path.read_text(encoding="utf-8"))
        head, sections = split_sections(body)
        return {
            "qq": str(fm.get("qq") or qq),
            "name": fm.get("name", ""),
            "first_met": fm.get("first_met", ""),
            "updated": fm.get("updated", ""),
            "facts": bullets(sections.get(FACT_SECTION, "")),
            "timeline": bullets(sections.get(TIMELINE_SECTION, "")),
            "path": path,
        }

    def _save_person(
        self,
        qq: str,
        name: str,
        facts: list[str],
        timeline: list[str],
        first_met: str = "",
    ) -> None:
        path = self.person_file(qq)
        old = self.load_person(qq) if path.is_file() else {}
        first_met = first_met or old.get("first_met") or date.today().isoformat()
        name = _clean(name, 40) or old.get("name") or ""
        fm = {
            "type": "person",
            "qq": f'"{qq}"',
            "name": name,
            "first_met": first_met,
            "updated": datetime.now().isoformat(timespec="seconds"),
            "facts": str(len(facts)),
            "tags": "[人物]",
        }

        # 人在 Obsidian 里加的其它段落要留着，所以不是整篇重写，而是改自己那两节
        head = f"# {name or qq}"
        extras: "OrderedDict[str, str]" = OrderedDict()
        if path.is_file():
            old_head, old_sections = split_sections(
                split_frontmatter(path.read_text(encoding="utf-8"))[1]
            )
            for line in old_head.splitlines():
                if line.startswith("# "):
                    head = line  # 标题被你改过就跟着你的
                    break
            for title, body_text in old_sections.items():
                if title not in (FACT_SECTION, TIMELINE_SECTION):
                    extras[title] = body_text

        sections: "OrderedDict[str, str]" = OrderedDict()
        sections[FACT_SECTION] = "\n".join(f"- {x}" for x in facts) or "_（还没记什么）_"
        sections[TIMELINE_SECTION] = "\n".join(f"- {x}" for x in timeline)
        sections.update(extras)
        atomic_write(path, build_doc(fm, head, sections))

    def add_fact(self, qq: str, name: str, text: str, keep: int = 80) -> bool:
        """记一条关于某个人的事实。重复的不记，返回是否真的写进去。"""
        text = _clean(text)
        if not qq or not text:
            return False
        person = self.load_person(qq)
        facts = list(person.get("facts") or [])
        fresh = strip_date(text)
        for old in facts:
            # 同一条话换个说法重复记，会把提示词挤爆；粗判包含关系就够用
            if fresh and (fresh in strip_date(old) or strip_date(old) in fresh):
                return False
        facts.append(f"{date.today().isoformat()} · {fresh}")
        if len(facts) > max(1, keep):
            facts = facts[-max(1, keep):]
        self._save_person(qq, name, facts, list(person.get("timeline") or []),
                          person.get("first_met", ""))
        return True

    def add_timeline(self, qq: str, name: str, text: str, keep: int = 40) -> None:
        text = _clean(text)
        if not qq or not text:
            return
        person = self.load_person(qq)
        lines = list(person.get("timeline") or [])
        lines.append(f"{date.today().isoformat()} · {text}")
        if len(lines) > max(1, keep):
            lines = lines[-max(1, keep):]
        self._save_person(qq, name, list(person.get("facts") or []), lines,
                          person.get("first_met", ""))

    def forget_facts(self, keyword: str) -> int:
        """删掉所有人物档案里命中关键词的事实，返回删了几条。"""
        keyword = (keyword or "").strip()
        if not keyword:
            return 0
        removed = 0
        for path in sorted((self.root / "people").glob("*.md")):
            person = self.load_person(path.stem)
            facts = list(person.get("facts") or [])
            kept = [f for f in facts if keyword not in f]
            if len(kept) == len(facts):
                continue
            removed += len(facts) - len(kept)
            # 走 _save_person，顺带把 facts 计数和 qq 的引号一起修正
            self._save_person(
                person.get("qq") or path.stem,
                person.get("name", ""),
                kept,
                list(person.get("timeline") or []),
                person.get("first_met", ""),
            )
        return removed

    # ==================== 日记 ====================

    def append_journal(self, when: datetime, qq: str, name: str, text: str) -> None:
        text = _clean(text, 200)
        if not text:
            return
        path = self.journal_file(when.date())
        fm = {"type": "journal", "date": when.date().isoformat(), "tags": "[日记]"}
        head, sections = f"# {when.date().isoformat()}", OrderedDict()
        if path.is_file():
            fm_old, body = split_frontmatter(path.read_text(encoding="utf-8"))
            head, sections = split_sections(body)
            fm.update({k: v for k, v in fm_old.items() if k not in fm})
            head = head.strip() or f"# {when.date().isoformat()}"
        link = f"[[people/{qq}|{name or qq}]]" if qq else (name or "某人")
        line = f"- {when:%H:%M} · {link}：{text}"
        sections[JOURNAL_SECTION] = (sections.get(JOURNAL_SECTION, "") + "\n" + line).strip("\n")
        atomic_write(path, build_doc(fm, head, sections))

    def journal_lines(self, day: date) -> list[str]:
        """当天 `## 记录` 里的条目，去掉时间戳和 [[链接]]，喂给日记当事实底稿。"""
        path = self.journal_file(day)
        if not path.is_file():
            return []
        try:
            _, body = split_frontmatter(path.read_text(encoding="utf-8"))
        except OSError:
            return []
        _, sections = split_sections(body)
        out: list[str] = []
        for item in bullets(sections.get(JOURNAL_SECTION, "")):
            text = _JOURNAL_LINK.sub("", _JOURNAL_TIME.sub("", item)).strip()
            if text:
                out.append(text)
        return out

    # ==================== 日记（她自己写的） ====================

    def read_diary(self, day: date) -> list[str]:
        """读某天 `## 日记` 的正文条目，按写入顺序。"""
        path = self.journal_file(day)
        if not path.is_file():
            return []
        try:
            _, body = split_frontmatter(path.read_text(encoding="utf-8"))
        except OSError:
            return []
        _, sections = split_sections(body)
        return diary_blocks(sections.get(DIARY_SECTION, ""))

    def append_diary(self, when: datetime, text: str, keep: int = 12) -> bool:
        """她自己写的日记：第一人称散文，落在 `## 日记`。

        和 `## 记录` 分开是有意的，两边不是同一种东西：

        - `## 记录` 是整理器写的第三方摘要，一行一条，**参与检索**；
        - `## 日记` 是她自己的口气，散文、无 `- ` 前缀，所以 `_make_doc`
          收不到它，不会混进"翻到的往事"。它只在注入时按条取用。

        写满 `keep` 条就**停写**，不删旧的：日记是只增不改的东西，
        删掉她昨天写的比少写两行严重得多。
        """
        text = _prose(text)
        if not text:
            return False
        path = self.journal_file(when.date())
        fm = {"type": "journal", "date": when.date().isoformat(), "tags": "[日记]"}
        head, sections = f"# {when.date().isoformat()}", OrderedDict()
        if path.is_file():
            fm_old, body = split_frontmatter(path.read_text(encoding="utf-8"))
            head, sections = split_sections(body)
            fm.update({k: v for k, v in fm_old.items() if k not in fm})
            head = head.strip() or f"# {when.date().isoformat()}"
            if len(diary_blocks(sections.get(DIARY_SECTION, ""))) >= max(1, keep):
                return False
        block = f"**{when:%H:%M}**\n{text}"
        sections[DIARY_SECTION] = (
            (sections.get(DIARY_SECTION, "") + "\n\n" + block).strip("\n")
        )
        # 日记放最前面：打开当天笔记先看到她写的，再看事无巨细的记录
        ordered: "OrderedDict[str, str]" = OrderedDict()
        ordered[DIARY_SECTION] = sections.pop(DIARY_SECTION)
        ordered.update(sections)
        atomic_write(path, build_doc(fm, head, ordered))
        return True


    # ==================== 主题 ====================

    def add_topic(self, topic: str, text: str, qq: str = "", name: str = "") -> bool:
        topic, text = _clean(topic, 20), _clean(text)
        if not topic or not text:
            return False
        path = self.topic_file(topic)
        fm = {"type": "topic", "tags": "[主题]"}
        head, sections = f"# {topic}", OrderedDict()
        if path.is_file():
            fm_old, body = split_frontmatter(path.read_text(encoding="utf-8"))
            head, sections = split_sections(body)
            fm.update({k: v for k, v in fm_old.items() if k not in fm})
            head = head.strip() or f"# {topic}"
        body_text = sections.get("要点", "")
        for old in bullets(body_text):
            if text in strip_date(old) or strip_date(old) in text:
                return False
        who = f"[[people/{qq}|{name or qq}]]" if qq else ""
        line = f"- {date.today().isoformat()} · " + (f"{who}：{text}" if who else text)
        sections["要点"] = (body_text + "\n" + line).strip("\n")
        fm["updated"] = datetime.now().isoformat(timespec="seconds")
        atomic_write(path, build_doc(fm, head, sections))
        return True

    # ==================== 她自己 / 主人私下 ====================

    def add_self(self, text: str, keep: int = 40) -> bool:
        text = _clean(text)
        if not text:
            return False
        path = self.self_file()
        fm = {"type": "self", "title": "我"}
        head, sections = "# 我", OrderedDict()
        if path.is_file():
            fm_old, body = split_frontmatter(path.read_text(encoding="utf-8"))
            head, sections = split_sections(body)
            fm.update({k: v for k, v in fm_old.items() if k not in fm})
            head = head.strip() or "# 我"
        items = bullets(sections.get(SELF_SECTION, ""))
        if any(text in strip_date(x) or strip_date(x) in text for x in items):
            return False
        items.append(f"{date.today().isoformat()} · {text}")
        sections[SELF_SECTION] = "\n".join(f"- {x}" for x in items[-max(1, keep):])
        fm["updated"] = datetime.now().isoformat(timespec="seconds")
        atomic_write(path, build_doc(fm, head, sections))
        return True

    # ==================== 检索用：全库遍历 ====================

    def iter_docs(self):
        """遍历人和日记/主题/自我四个目录，带 mtime 缓存。"""
        for kind in ("people", "journal", "topics", "self"):
            for path in sorted((self.root / kind).glob("*.md")):
                try:
                    st = path.stat()
                except OSError:
                    continue
                key = str(path)
                cached = self._cache.get(key)
                if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
                    yield cached[2]
                    continue
                doc = self._make_doc(path, kind, st.st_mtime)
                if doc:
                    self._cache[key] = (st.st_mtime, st.st_size, doc)
                    yield doc

    def _make_doc(self, path: Path, kind: str, mtime: float) -> dict | None:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return None
        fm, body = split_frontmatter(raw)
        head, sections = split_sections(body)
        title = ""
        for line in head.splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
                break
        title = title or path.stem

        if kind == "people":
            entries = bullets(sections.get(FACT_SECTION, ""))
        else:
            entries = []
            for body_text in sections.values():
                entries.extend(bullets(body_text))
        if not entries:
            return None

        text = f"{path.stem} {title} " + " ".join(entries)
        return {
            "kind": kind,
            "path": path.relative_to(self.root).as_posix(),
            "title": title,
            "stem": path.stem,
            "entries": entries,
            "text": text,
            "ts": mtime,
        }

    def stats(self) -> dict:
        counts = {}
        size = 0
        newest = 0.0
        for kind in self.KINDS:
            files = list((self.root / kind).glob("*.md"))
            counts[kind] = len(files)
            for f in files:
                try:
                    st = f.stat()
                except OSError:
                    continue
                size += st.st_size
                newest = max(newest, st.st_mtime)
        return {"counts": counts, "size": size, "newest": newest, "root": str(self.root)}
