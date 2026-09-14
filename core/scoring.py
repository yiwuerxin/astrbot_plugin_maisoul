"""回复必要性评分器 —— 移植自 MaiBot src/maisaka/reply_necessity.py

final = (相关档位 + 内容分 + 压力分 − 存在感惩罚) × 频率倍率
档位互斥：@=100 / 昵称提及=80 / 普通=0；阈值默认 80。
"""

import time
from dataclasses import dataclass
from math import ceil, log1p
import re

from . import mention
from .constants import (
    DIRECT_REQUEST_TERMS,
    IDLE_PRESSURE_BONUS,
    IGNORED_TEXT_PREFIXES,
    MEDIA_PLACEHOLDER_PREFIXES,
    OPINION_TERMS,
    OTHER_ASSISTANT_PATTERN,
    PRESSURE_FULL_RATIO,
    PRESSURE_MAX,
    PRESSURE_STANDARD,
    QUESTION_TERMS,
    SELF_PENALTY_MAX,
    SELF_RATIO_FREE,
    SELF_RATIO_FULL,
    SHORT_REACTIONS,
    WEAK_REQUEST_TERMS,
    necessity_threshold,
)
from .states import GroupState


@dataclass
class NecessityResult:
    score: int
    detail: str
    style: str  # 简短表达/正常回复/长回复 —— MaiBot reply 工具的真实枚举；Planner 未复刻期间由档位+内容启发式代选


def strip_noise(text: str) -> str:
    """对齐 strip_reply_necessity_noise：剥离引用/CQ/@/媒体占位。"""
    t = " ".join((text or "").split()).strip()
    if t.startswith(IGNORED_TEXT_PREFIXES):
        return ""
    t = re.sub(r"^\[CQ:reply[^\]]*\]\s*", "", t)
    t = re.sub(r"^\[回复了.+?的消息: .+?\]\s*", "", t)
    t = re.sub(r"@<[^>]+>|@\S+", "", t).strip()
    if t.startswith(MEDIA_PLACEHOLDER_PREFIXES):
        return ""
    return t.strip()


def is_question(text: str) -> bool:
    """对齐 has_reply_necessity_question：按句式判断真实问句。"""
    if not text:
        return False
    if re.fullmatch(r"[？?！!~～…\s]+[\w\u4e00-\u9fff]{1,4}[？?！!~～…\s]+", text):
        return False
    if any(term in text for term in QUESTION_TERMS):
        return True
    if re.search(r"(?<![这那没])什么", text):
        return True
    if re.search(r"[吗呢](?:[？?。！!~～…]*$)", text) and 4 <= len(text) <= 80:
        return True
    return bool(re.search(r"[？?](?:$|[。！!~～…])", text) and 4 <= len(text) <= 120)


def request_reason(text: str, is_direct: bool) -> str:
    if not is_direct and OTHER_ASSISTANT_PATTERN.search(text):
        return ""
    hits = [t for t in DIRECT_REQUEST_TERMS if t in text]
    if "能不能" in hits and not (is_direct or text.startswith("能不能")):
        hits.remove("能不能")
    if not is_direct:
        hits = [t for t in hits if t not in ("可以吗", "要不要")]
    if hits:
        return "/".join(hits)
    if is_direct:
        weak = [t for t in WEAK_REQUEST_TERMS if t in text]
        if weak:
            return "/".join(weak)
    return ""


def opinion_reason(text: str, is_direct: bool, bot_names: list[str]) -> str:
    if "不怎么看" in text:
        return ""
    if not is_direct and not mention.is_mentioned(text, "", bot_names):
        return ""
    hits = [t for t in OPINION_TERMS if t in text]
    if hits:
        return "/".join(hits)
    # 「怎么看」句式随 bot_name/aliases 动态构造（v6.20.3：旧正则硬编码
    # "麦麦"，改名后"XX怎么看"不加分；默认名下与 MaiBot 原文字面等价，
    # 别名入式与提及档收口同口径）
    names = [re.escape(str(n).strip()) for n in (bot_names or []) if str(n).strip()]
    if names:
        alt = "|".join(names)
        if re.search(rf"(?:你|{alt}).{{0,6}}怎么看|怎么看.{{0,6}}(?:你|{alt})", text):
            return "怎么看"
    return ""


def pressure_score(pending: int, msg_threshold: int, idle_reached: bool) -> int:
    """对齐 _calculate_pressure_score：阈值内平方、超阈值对数、闲置加成。"""
    th = max(1, msg_threshold)
    ratio = max(0.0, pending / th)
    if ratio <= 1.0:
        score = int(round(PRESSURE_STANDARD * ratio * ratio))
        if idle_reached:
            score += IDLE_PRESSURE_BONUS
        return min(PRESSURE_STANDARD, score)
    overflow = ratio - 1.0
    factor = min(1.0, log1p(overflow) / log1p(PRESSURE_FULL_RATIO - 1.0))
    return min(
        PRESSURE_MAX,
        PRESSURE_STANDARD + int(round((PRESSURE_MAX - PRESSURE_STANDARD) * factor)),
    )


def presence_penalty(st: GroupState) -> int:
    """对齐 _calculate_recent_presence_penalty：5 分钟窗口自发占比。"""
    window = st.recent_window(300)
    self_cnt = st.recent_self_count(300)
    if window <= 0 or self_cnt <= 0:
        return 0
    ratio = min(1.0, self_cnt / window)
    if ratio <= SELF_RATIO_FREE:
        return 0
    progress = min(1.0, (ratio - SELF_RATIO_FREE) / (SELF_RATIO_FULL - SELF_RATIO_FREE))
    return int(round(SELF_PENALTY_MAX * progress))


def score_content(
    cleaned: str,
    is_direct: bool,
    bot_names: list[str],
    short_reaction: bool | None = None,
) -> tuple[int, list[str]]:
    """内容分。short_reaction 显式传入时覆盖默认判定（批次口径由调用方算，
    对齐 turn_gates 的批次级短反应：任一条 >8 字即非短反应批次）。"""
    score, reasons = 0, []
    if is_question(cleaned):
        score += 15
        reasons.append("问题")
    req = request_reason(cleaned, is_direct)
    if req:
        score += 20
        reasons.append(f"请求:{req}")
    op = opinion_reason(cleaned, is_direct, bot_names)
    if op:
        score += 20
        reasons.append(f"征询:{op}")
    if len(cleaned) >= 40:
        score += 5
    if len(cleaned) >= 120:
        score += 10
    if cleaned in SHORT_REACTIONS if short_reaction is None else short_reaction:
        score -= 25
        reasons.append("短反应")
    return score, reasons


def freq_factor(frequency: float) -> float:
    return 0.5 + 0.5 * max(0.0, min(1.0, frequency))


def msg_trigger_threshold(frequency: float) -> int:
    """对齐 runtime._get_message_trigger_threshold：必要性模式下 ceil(1/f²)。

    实现在 constants.necessity_threshold（单一真相，2026-09-11 下沉）。"""
    return necessity_threshold(frequency)


def evaluate(
    st: GroupState,
    *,
    at_bot: bool,
    text: str,
    aliases: list[str],
    bot_name: str,
    frequency: float,
    batch_texts: list[str] | None = None,
    feedback_note: str = "",
) -> NecessityResult:
    """必要性触发模式的评分入口（阈值固定 80 = REPLY_NECESSITY_TRIGGER_SCORE）。

    aliases 即 MaiBot alias_names。batch_texts：上次发言以来的整批外部消息
    （对齐 turn_gates 对 pending_messages 整批评）——提及档批内任一命中即
    80 档；内容分按拼接全文算长度；短反应按批次判定（任一条 >8 字即非
    短反应批次，全批 ∈ SHORT_REACTIONS 才 −25）。
    frequency 传已含频率窗口反馈的 effective 值（v6.28.0 对齐 MaiBot
    _talk_frequency_adjust 结构：阈值与倍率同吃一个乘数，倍率天然带 0.5
    下限——不再在倍率外侧另乘反馈，消除最坏 ×0.1 的双重压制）。"""
    _name = str(bot_name or "麦麦")
    if at_bot:
        rel, rel_reason = 100, "@"
    elif mention.is_mentioned(text, _name, aliases) or any(
        mention.is_mentioned(str(t or ""), _name, aliases) for t in (batch_texts or [])
    ):
        rel, rel_reason = 80, "提及"
    else:
        rel, rel_reason = 0, "普通"
    is_direct = rel > 0

    cleaned_list = [strip_noise(t) for t in [text, *(batch_texts or [])]]
    cleaned_list = [c for c in cleaned_list if c]
    combined = "".join(cleaned_list)
    # 批次级短反应（对齐 turn_gates：任一条 >8 字即非短反应；全批 ∈ 词表才罚）
    _short = (
        bool(cleaned_list)
        and all(len(c) <= 8 for c in cleaned_list)
        and all(c in SHORT_REACTIONS for c in cleaned_list)
    )
    bot_names = [_name, *[str(n) for n in (aliases or [])]]
    content, reasons = score_content(
        combined, is_direct, bot_names, short_reaction=_short
    )
    cleaned = combined  # 档位文案/篇幅判定沿用拼接全文

    avg_interval = st.avg_external_interval()
    idle_reached = bool(
        avg_interval and st.last_ext_ts and time.time() - st.last_ext_ts >= avg_interval
    )
    pending = st.pending_since_fire
    pressure = pressure_score(pending, msg_trigger_threshold(frequency), idle_reached)
    penalty = presence_penalty(st)

    raw = rel + content + pressure - penalty
    # P-E：频率窗口反馈已乘进 frequency（见 docstring），此处只算基础倍率
    factor = freq_factor(frequency)
    final = max(0, int(round(raw * factor)))

    parts = [f"最终={final}", f"原始={raw}", f"档位={rel}({rel_reason})"]
    if content:
        parts.append(f"内容={content}({','.join(reasons)})")
    if pressure:
        parts.append(
            f"压力={pressure}(积压{pending}/闲置{'是' if idle_reached else '否'})"
        )
    if penalty:
        parts.append(f"存在感=-{penalty}")
    parts.append(f"倍率={factor:.2f}")
    if feedback_note:
        parts.append(feedback_note)

    if rel >= 80 and (
        len(cleaned) >= 120 or any(r.startswith("请求") for r in reasons)
    ):
        style = "长回复"
    elif rel == 0:
        style = "简短表达"
    else:
        style = "正常回复"
    return NecessityResult(final, " ".join(parts), style)
