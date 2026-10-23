# -*- coding: utf-8 -*-
"""关键词检索：先不依赖 embedding。

为什么不用向量：AstrBot 现在只配了 chat 模型，没有 Embedding Provider，
上向量就得先装一个本地嵌入模型或者开通云端 embedding API。先用中文二元组
+ IDF + 时间衰减把召回跑起来，够用，而且零依赖；等真配了 embedding，
把 `search()` 里加一路向量打分做融合就行，接口不用改。

分词的粗暴程度是有意的：中文没有空格，句子里「显卡」「换显卡」这类词，
二元组能把大部分有效信息捞出来，代价是偶尔召回过宽——由 IDF 和 top_k 兜住。
"""

from __future__ import annotations

import math
import re
import time

_CJK = re.compile(r"[\u4e00-\u9fff]+")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]{1,}")
_DATE_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}\s*[·:]\s*")

# 只挡掉真正高频、且几乎不携带信息的二元组；挡太多反而会漏
_STOP = {
    "我们", "你们", "他们", "她们", "什么", "怎么", "这个", "那个", "一个", "就是",
    "不是", "还是", "没有", "知道", "现在", "时候", "因为", "所以", "如果", "但是",
    "然后", "已经", "自己", "可以", "不是", "一样", "这样", "那样", "有点", "一直",
}

# 单个汉字的命中权重要打折：它只用来兜住"猫""猫呢"这种一两个字的短问句，
# 不参与长句打分，否则"的""我"这类字会把所有文档都点亮
_STOP_CHAR = set("的了是我你他她它在有和就不也都这那就很还吗呢吧把被给让")


def tokenize(text: str) -> set[str]:
    text = (text or "").lower()
    out: set[str] = set()
    for run in _CJK.findall(text):
        for ch in run:
            if ch not in _STOP_CHAR:
                out.add(ch)
        for i in range(len(run) - 1):
            piece = run[i:i + 2]
            if piece not in _STOP:
                out.add(piece)
    for word in _WORD.findall(text):
        if len(word) >= 2:
            out.add(word)
    return out


def _weight(token: str) -> float:
    """二元组算满分，单字打折。"""
    return 1.0 if len(token) > 1 else 0.45


def _strip_date(item: str) -> str:
    return _DATE_PREFIX.sub("", item or "").strip()


def strip_bullets(text: str) -> list[str]:
    """把一个段落里的 `- xxx` 取出来（读别人手写的笔记时用）。"""
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith(("- ", "* ")):
            item = line[2:].strip()
            if item and not item.startswith("_("):
                out.append(item)
    return out


def _recency(doc: dict, now: float) -> float:
    """日记衰减快，人物档案几乎不衰减——事实是事实，日记是当时的心情。"""
    days = max(0.0, (now - float(doc.get("ts") or now)) / 86400)
    if doc.get("kind") == "journal":
        return 0.35 + 0.65 * math.exp(-days / 30.0)
    if doc.get("kind") == "topics":
        return 0.6 + 0.4 * math.exp(-days / 90.0)
    return 1.0


def _best_entry(entries: list[str], query_tokens: set[str]) -> str:
    best, best_hit = "", -1
    for item in entries:
        hit = len(tokenize(item) & query_tokens)
        if hit > best_hit:
            best, best_hit = item, hit
    text = _strip_date(best or (entries[0] if entries else ""))
    return text[:160]


def search(
    docs: list[dict],
    query: str,
    top_k: int = 3,
    exclude_paths: set[str] | None = None,
    min_score: float = 0.6,
) -> list[dict]:
    """返回 [{path,title,kind,snippet,score}]，按分数降序。

    exclude_paths 用来排掉「已经整篇注入过的人物档案」，避免同一批事实出现两次。
    """
    query_tokens = tokenize(query)
    if not query_tokens or not docs:
        return []

    exclude_paths = exclude_paths or set()
    corpus = [d for d in docs if d.get("path") not in exclude_paths]
    if not corpus:
        return []

    doc_freq: dict[str, int] = {}
    doc_tokens: list[set[str]] = []
    for doc in corpus:
        tokens = tokenize(doc.get("text", ""))
        doc_tokens.append(tokens)
        for token in tokens:
            doc_freq[token] = doc_freq.get(token, 0) + 1

    total = len(corpus)
    now = time.time()
    # 全是单字的短问句（"猫呢"）本来信号就弱，阈值跟着松一点
    if all(len(t) == 1 for t in query_tokens):
        min_score *= 0.5
    scored: list[dict] = []
    for doc, tokens in zip(corpus, doc_tokens):
        matched = query_tokens & tokens
        if not matched:
            continue
        score = sum(
            math.log(1.0 + total / max(1, doc_freq.get(t, 0))) * _weight(t)
            for t in matched
        )
        # 命中率高一点再加权，避免只有一两个通用词命中的文档挤掉真正相关的
        score *= 1.0 + 0.15 * (len(matched) - 1)
        score *= _recency(doc, now)
        if score < min_score:
            continue
        scored.append({
            "path": doc["path"],
            "title": doc.get("title", ""),
            "kind": doc.get("kind", ""),
            "snippet": _best_entry(doc.get("entries") or [], query_tokens),
            "score": round(score, 2),
        })

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[: max(1, top_k)]
