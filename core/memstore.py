"""中期记忆（P-A，GOAL Phase 3）——上下文折叠摘要 + 线索召回。

被上下文窗口裁掉的旧消息经一次廉价 LLM 摘要为 {summary, recall_cues≤5}
存入会话记忆；后续轮次用最近消息与线索的词集 Jaccard（无 embedding 的
召回近似，阈值可配）召回为「内部参考」注入 replyer。单轮 ≤3 条、总长
≤900 字、标注"内部参考，不要逐字引用"。纯逻辑可单测；LLM 摘要在
planner_host 后台任务中调用（memory_enable 默认关）。
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field

MAX_POINTS = 64  # 每会话记忆点上限（FIFO 淘汰）
MAX_RECALL_ITEMS = 3  # 单轮召回上限
MAX_RECALL_CHARS = 900  # 召回总字数上限
DEFAULT_THRESHOLD = 0.18  # Jaccard 词集命中阈值（无 embedding 的召回近似）

_WORD_RE = re.compile(r"[A-Za-z0-9_]+")


def word_set(text: str) -> set[str]:
    """词集：CJK 按相邻二元组（无分词依赖的近似），拉丁/数字按词。"""
    t = str(text or "")
    words: set[str] = set()
    cjk = ""
    for ch in t:
        if "\u4e00" <= ch <= "\u9fff":
            cjk += ch
            continue
        if cjk:
            _add_cjk(words, cjk)
            cjk = ""
    if cjk:
        _add_cjk(words, cjk)
    words.update(w.lower() for w in _WORD_RE.findall(t) if len(w) >= 2)
    return words


def _add_cjk(words: set[str], s: str) -> None:
    for i in range(len(s) - 1):
        words.add(s[i : i + 2])
    if len(s) == 1:
        words.add(s)


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


@dataclass
class MemoryPoint:
    summary: str
    cues: list[str] = field(default_factory=list)
    ts: float = 0.0


@dataclass
class SessionMemory:
    """单会话记忆点集（内存态，FIFO 有界）。"""

    points: deque = field(default_factory=lambda: deque(maxlen=MAX_POINTS))

    def add(self, summary: str, cues: list[str], ts: float) -> None:
        s = str(summary or "").strip()[:300]
        if not s:
            return
        self.points.append(
            MemoryPoint(
                s, [str(c).strip() for c in (cues or [])[:5] if str(c).strip()], ts
            )
        )

    def recall(
        self,
        texts: list[str],
        *,
        k: int = MAX_RECALL_ITEMS,
        threshold: float = DEFAULT_THRESHOLD,
        max_chars: int = MAX_RECALL_CHARS,
    ) -> list[str]:
        """最近消息词集 × 线索词集 Jaccard 最大值 ≥ threshold → 命中，按分取前 k。"""
        target = set()
        for t in texts or []:
            target |= word_set(t)
        if not target:
            return []
        scored: list[tuple[float, float, str]] = []
        for p in self.points:
            cue_set = set()
            for c in p.cues:
                cue_set |= word_set(c)
            score = jaccard(target, cue_set)
            if score >= threshold:
                scored.append((score, p.ts, p.summary))
        scored.sort(key=lambda x: (-x[0], -x[1]))
        out, total = [], 0
        for _, _, summary in scored[:k]:
            if total + len(summary) > max_chars:
                break
            out.append(summary)
            total += len(summary)
        return out

    @staticmethod
    def render(recalled: list[str]) -> str:
        """渲染「内部参考」块（标注不逐字引用）。"""
        if not recalled:
            return ""
        body = "\n".join(f"- {s}" for s in recalled)
        return f"\n\n【内部参考（更早的对话记忆，帮助理解语境；不要逐字引用）】\n{body}"


def parse_summary(raw: str) -> tuple[str, list[str]] | None:
    """解析摘要 LLM 输出 {"summary":…,"cues":[…]}；失败返回 None（静默跳过）。"""
    import json

    text = str(raw or "").strip()
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        summary = str(obj.get("summary") or "").strip()
        cues = [str(c).strip() for c in (obj.get("cues") or []) if str(c).strip()]
        if not summary:
            return None
        return summary[:300], cues[:5]
    except Exception:
        return None


SUMMARIZE_PROMPT = (
    "请把下面的聊天记录压缩成一句总结（保留谁、做了什么、关系/约定类信息），"
    "并给 ≤5 个回忆线索词（人名/外号/话题关键词），用于之后判断是否要想起这段对话。\n"
    "只输出 JSON，不要其他内容：\n"
    '{{"summary": "一句话总结", "cues": ["线索词1", "线索词2"]}}\n\n'
    "聊天记录：\n{chat_log}"
)
