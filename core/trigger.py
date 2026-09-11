"""消息触发门控 —— 源码级移植 MaiBot turn_scheduler/turn_gates + talk_value 规则

两种触发模式（chat.reply_timing.reply_trigger_mode，MaiBot 默认 frequency）：
- frequency 频率触发：pending ≥ ceil(1/f) 直接触发；不足时按空窗补偿
  （idle/平均间隔 折算等效消息数，封顶 threshold-1，且必须 ≥1 条真实新消息）
- reply_necessity 必要性触发：scoring.evaluate 评分 ≥ 80

强制触发绕过普通阈值：@ 且 inevitable_at_reply（默认开）、
昵称提及且 mentioned_bot_reply（默认关）。
talk_value ≤ 0 为静默接收（不触发）。

talk_value 规则（enable_talk_value_rules + talk_value_rules）：
  [{platform, item_id, rule_type(group/private), time("HH:MM-HH:MM"或"*"), value}]
  目标优先级：双空=1 ＜ 单匹配=3 ＜ 通配=4 ＜ 精确双匹配=5；
  时间优先级：空=1 ＜ 区间命中=2 ＜ "*"=3；取 (目标,时间) 最大的规则值。
"""

import time
from math import ceil

from astrbot.api import logger

from . import freqfeedback, scoring
from .constants import necessity_threshold
from .states import GroupState

TRIGGER_SCORE = 80  # REPLY_NECESSITY_TRIGGER_SCORE


# ---------------------------------------------------------------------- #
# talk_value 解析（复刻 ChatConfigUtils.get_talk_value）
# ---------------------------------------------------------------------- #
def _parse_range(range_str: str):
    try:
        start_str, end_str = [s.strip() for s in range_str.split("-")]
        sh, sm = [int(x) for x in start_str.split(":")]
        eh, em = [int(x) for x in end_str.split(":")]
        return sh * 60 + sm, eh * 60 + em
    except Exception:
        # 降级：时间段格式非法按无时间限制（规则仍按优先级生效）
        logger.debug("maisoul: talk_value 规则时间段解析失败", exc_info=True)
        return None


def _rule_time_priority(rule_time: str, now_min: int):
    rule_time = str(rule_time or "").strip()
    if not rule_time:
        return 1
    if rule_time == "*":
        return 3
    parsed = _parse_range(rule_time)
    if not parsed:
        return None
    start_min, end_min = parsed
    if start_min <= end_min:
        return 2 if start_min <= now_min <= end_min else None
    return 2 if now_min >= start_min or now_min <= end_min else None


def _rule_target_priority(
    rule: dict, platform: str, chat_id: str, is_group: bool
) -> int | None:
    p = str(rule.get("platform") or "").strip()
    item = str(rule.get("item_id") or "").strip()
    rule_type = str(rule.get("rule_type") or "").strip()
    if rule_type != ("group" if is_group else "private"):
        return None
    if not p and not item:
        return 1
    has_wildcard = p == "*" or item == "*"
    platform_matches = not p or p == "*" or p == platform
    item_matches = not item or item == "*" or item == chat_id
    if not platform_matches or not item_matches:
        return None
    if p and item and not has_wildcard:
        return 5
    if has_wildcard:
        return 4
    return 3


def _rule_value(rule: dict) -> float:
    try:
        return float(rule.get("value"))
    except (TypeError, ValueError):
        return 0.0


def effective_talk_value(
    cfg, platform: str, chat_id: str, now: float | None = None, is_group: bool = True
) -> float:
    """基础 talk_value（私聊用 private_talk_value）+ 动态规则覆盖（无规则命中回落基础值）。"""
    base = float(
        cfg.get("talk_value", 1.0)
        if is_group
        else cfg.get("private_talk_value", 1.0) or 0.0
    )
    if not cfg.get("enable_talk_value_rules", False):
        return base
    rules = [r for r in (cfg.get("talk_value_rules") or []) if isinstance(r, dict)]
    if not rules:
        return base
    local = time.localtime(now)
    now_min = local.tm_hour * 60 + local.tm_min
    best: tuple[tuple[int, int], float] | None = None
    for rule in rules:
        target_priority = _rule_target_priority(rule, platform, chat_id, is_group)
        if target_priority is None:
            continue
        time_priority = _rule_time_priority(str(rule.get("time") or ""), now_min)
        if time_priority is None:
            continue
        priority = (target_priority, time_priority)
        if best is None or priority > best[0]:
            best = (priority, _rule_value(rule))
    return best[1] if best is not None else base


def message_trigger_threshold(mode: str, frequency: float) -> int:
    """复刻 runtime._get_message_trigger_threshold：frequency=ceil(1/f)，necessity=ceil(1/f²)。

    necessity 分支引 constants.necessity_threshold（单一真相）。"""
    f = min(1.0, max(0.0, frequency))
    if f <= 0:
        return 0
    if mode == "reply_necessity":
        return necessity_threshold(f)
    return max(1, ceil(1.0 / f))


# ---------------------------------------------------------------------- #
# 门控判定
# ---------------------------------------------------------------------- #
def idle_compensation(
    st: GroupState, pending: int, threshold: int, now: float | None = None
) -> tuple[bool, str]:
    """复刻 FrequencyThresholdTurnGate._calculate_idle_compensation。

    空窗折算封顶 threshold-1，杜绝纯沉默触发；平均间隔带下限保护。
    """
    if pending < 1:
        return False, "pending=0，不允许纯沉默触发"
    avg_interval = st.avg_external_interval()
    if not avg_interval or avg_interval <= 0:
        return False, "平均消息间隔不可用，无法进行空窗补偿"
    now = time.time() if now is None else now
    idle = max(0.0, now - (st.last_ext_ts or now))
    idle_equiv = min(idle / avg_interval, float(max(0, threshold - 1)))
    equivalent = pending + idle_equiv
    detail = (
        f"平均间隔={avg_interval:.2f}s 空窗={idle:.2f}s "
        f"空窗折算={idle_equiv:.2f} 等效消息数={equivalent:.2f}/{threshold}"
    )
    return equivalent >= threshold, detail


def frequency_recheck_delay(
    st: GroupState, pending: int, threshold: int, now: float | None = None
) -> float | None:
    """复刻 FrequencyThresholdTurnGate.evaluate 的 delay 分支：
    预计再过多久等效消息数将达到阈值（到点无新消息也重查 → 主动补话）。"""
    if threshold <= 0 or pending < 1:
        return None
    avg_interval = st.avg_external_interval()
    if not avg_interval or avg_interval <= 0:
        return None
    now = time.time() if now is None else now
    idle = max(0.0, now - (st.last_ext_ts or now))
    return max(0.0, (threshold - pending) * avg_interval - idle)


def hit_ban_filter(text: str, words, regexes) -> bool:
    """过滤词（对齐 MessageUtils.check_ban_words/check_ban_regex：子串/正则命中即丢弃）。"""
    text = str(text or "")
    if not text:
        return False
    import re as _re

    for word in words or []:
        word = str(word or "")
        if word and word in text:
            return True
    for pattern in regexes or []:
        pattern = str(pattern or "")
        if not pattern:
            continue
        try:
            if _re.search(pattern, text):
                return True
        except _re.error:
            continue
    return False


def should_trigger(
    st: GroupState,
    cfg,
    *,
    at_bot: bool,
    mentioned: bool,
    text: str,
    aliases: list[str],
    bot_name: str,
    platform: str,
    chat_id: str,
    now: float | None = None,
    is_group: bool = True,
) -> tuple[bool, str, scoring.NecessityResult | None]:
    """总门控。返回 (是否触发, 日志明细, 必要性评分结果或 None)。"""
    now = time.time() if now is None else now
    talk_value = effective_talk_value(cfg, platform, chat_id, now, is_group)
    mode = str(cfg.get("reply_trigger_mode") or "frequency")
    threshold = message_trigger_threshold(mode, talk_value)
    pending = st.pending_since_fire
    freq_detail = f"[频率: {talk_value:.3f}][模式: {mode}][{pending}/{threshold} 消息]"

    if talk_value <= 0:
        return False, f"{freq_detail} 判定=静默接收", None

    forced = (at_bot and cfg.get("inevitable_at_reply", True)) or (
        mentioned and cfg.get("mentioned_bot_reply", False)
    )
    if forced:
        reason = "@" if at_bot else "提及"
        detail = f"{freq_detail} 判定=强制触发({reason}必回复)"
        if mode == "reply_necessity":
            fb_factor, fb_note = freqfeedback.frequency_feedback_factor(st, cfg)
            result = scoring.evaluate(
                st,
                at_bot=at_bot,
                text=text,
                aliases=aliases,
                feedback_factor=fb_factor,
                feedback_note=fb_note,
                bot_name=bot_name,
                frequency=talk_value,
            )
            return True, detail, result
        return True, detail, None

    if mode == "reply_necessity":
        fb_factor, fb_note = freqfeedback.frequency_feedback_factor(st, cfg)
        result = scoring.evaluate(
            st,
            at_bot=at_bot,
            text=text,
            aliases=aliases,
            feedback_factor=fb_factor,
            feedback_note=fb_note,
            bot_name=bot_name,
            frequency=talk_value,
        )
        fired = result.score >= TRIGGER_SCORE
        decision = "进入生成" if fired else "等待更多消息"
        return (
            fired,
            f"{freq_detail}[{result.detail}][评分阈值={TRIGGER_SCORE}][{decision}]",
            result,
        )

    if threshold > 0 and pending >= threshold:
        return True, f"{freq_detail} 判定=达到阈值进入生成", None
    compensated, idle_detail = idle_compensation(st, pending, threshold, now)
    if compensated:
        return True, f"{freq_detail}[{idle_detail}] 判定=空窗补偿进入生成", None
    return False, f"{freq_detail}[{idle_detail}] 判定=等待更多消息", None
