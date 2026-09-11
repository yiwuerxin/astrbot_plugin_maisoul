"""Planner 决策层 —— 源码级移植 MaiBot maisaka agent 循环

对标文件：
- prompts/zh-CN/maisaka_chat.prompt（系统提示词原文，占位符 bot_name/
  behavior_style/group_chat_attention_block/query_memory_rule）
- maisaka/runtime.py（MAX_INTERNAL_ROUNDS=10；WAIT/RUNNING 状态机；
  _try_enter_wait_state 连续上限；_resume_from_wait_for_proactive_trigger）
- maisaka/idle_backoff.py（连续空闲指数退避 base*2^n，封顶 cap；非空闲重置；
  pending ≥ bypass 绕过）
- maisaka/builtin_tool/{reply,wait,send_emoji,tool_search}.py（工具声明原文；
  v6.9.7 起 tool_search + deferred 池 = MaiBot「第三方工具默认 deferred」机制，
  由 main._planner_cycle 每轮轮转可见集并注入 <system-reminder> 提醒；
  fetch_history 是 focus 模式专属，部署版不暴露，v6.13.5 起移除）
- chat/replyer（reply 工具执行 → 三件套生成 + 后处理 + 打字发送；v6.9.7 起
  replyer 为纯生成器不带工具，管家/生态工具全部在 planner 侧经 tool_search 发现）

循环语义（对齐 reasoning_engine 的可运行核心）：
- 评分门通过后进入 Planner：system=maisaka_chat 原文，聊天消息（含自发消息）
  逐条进 user 轮（<message msg_id time user [quote] [group_card]
  [is_self_message]> 前缀，对齐 maisaka/context/planner_messages.build_planner_prefix）
- 每轮可调用工具；reply → 走 replyer 并结束本轮；wait → 进入等待（群聊期间
  新消息不唤醒；@/提及必回为主动触发可唤醒）；send_emoji → 表情包后继续；
  无工具 → 本轮空闲结束（fetch_history 为 MaiBot focus 模式专属工具，部署版
  focus_mode=false 不暴露，maisoul 同样不暴露，v6.13.5）
- 连续 wait 上限（默认 3）后视为对话休息；空闲结束累积退避
- 思考中的 Planner 可被新消息打断重思（planner_interrupt_max_consecutive_count，
  默认 0=不打断，消息留待本轮循环的后续轮次）

未移植（对应 maisoul 无等价基础设施，未伪造）：focus 专注模式、注意力漂移、
行为表现情景分析子代理、query_memory（记忆由 livingmemory 承担，取舍见 AGENTS §7）。
"""

import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime as _dt
from typing import Protocol

MAX_INTERNAL_ROUNDS = 10  # runtime.MAX_INTERNAL_ROUNDS

# ---------------- maisaka_chat.prompt 原文 ----------------
PLANNER_SYSTEM_TEMPLATE = """你的任务是分析聊天和聊天中的互动情况，然后做出下一步动作。
你需要关注 {bot_name} 与用户的对话来为 {bot_name} 选择正确的动作和行为

{bot_name}的行为风格：{behavior_style}


以上 {bot_name}的行为风格可以帮助你更好地决策

请你对当前场景和输出规则来进行分析。请注意，你不是 {bot_name} 本人，不要替 {bot_name} 发言，你需要给 {bot_name} 的行为做出决策，请你结合 {bot_name} 的行为风格、当前情况和可用工具做出决策。
在当前场景中，不同的人正在互动（{bot_name}也是一位参与的用户），用户也可能与进行聊天互动。
聊天记录中的「@名字」是群内点名（艾特）的文本形式：@后面是{bot_name}的名字，就表示说话人在点名{bot_name}本人；@后面是其他名字，则是在点名那位群友，不是在叫{bot_name}。
现在的上下文和聊天记录只是与参与聊天的用户当前的互动，你们之间可能有更多过去的关系和信息没有展现在上下文中。
“分析”应该体现你对当前局面的判断、你的建议、你的下一步计划，以及你为什么这样想。默认直接输出你当前的最新分析，不要重复之前的分析内容。最新分析应尽量具体，贴近上下文。
你需要先搜集能够帮助{bot_name}进行下一步行动的信息，然后再给出思考。

{group_chat_attention_block}

# Using your tools
- 当你判断{bot_name}现在应该正式对用户发出一条可见回复时调用reply。调用后生成一条真正展示给用户的回复。你可以针对某个用户回复，也可以对所有用户回复。发言必须通过reply工具，不然用户无法看见如果要回复，把上下文关键信息加入。
{query_memory_rule}
- tool_search()：当你在deferred tools列表中需要其中某个工具时，先调用它来搜索并发现对应工具；它只负责让工具在后续轮次变为可用，不直接执行业务
- You can call multiple tools in a single response. 聚合不同的信息源，进行多种操作来辅助你。If you intend to call multiple tools and there are no dependencies between them, make all independent tool calls in parallel.  However, if some tool calls depend on previous calls to inform dependent values, do NOT call these tools in parallel and instead call them sequentially.
- 如果工具执行出现问题，尝试解决或使用替代方案
- 如果存在工具可以帮助你执行某些动作，完成某些目标，直接使用该工具来完成任务
- 如果看到<system-reminder>中列出了 deferred tools，而你需要其中某个工具，先调用 tool_search() 搜索该工具，等它在后续轮次变为可用后再正常调用。
- wait(): 需要等待一段时间后再次判断时使用。
- 不使用工具: 当没有更多操作需要做时，或者等待长时间也没有最新内容时，结束思考，不要调用任何工具，其他情况都需要调用工具。

注意，你无法不调用reply工具直接回复，必须通过reply工具来发送回复。

现在，请你输出你对{bot_name}发言的分析，视情况输出文本内容的分析、进行工具调用："""

# 防复读固定反思文本（对齐 reasoning_engine._should_replace_reasoning 的替换内容）
PLANNER_REFLECT_ON_REPEAT = (
    "我应该根据我上面思考的内容进行反思，重新思考我下一步的行动，"
    "我需要分析当前场景，对话，然后直接输出我的想法："
)

# 叙述性回复意图检测（生产实报 2026-09-11：@bot 三次，模型两轮分析明确写
# "让我用麦麦的身份回复某群友"却零工具调用，planner 把空工具列表当有意沉默
# 收轮——@ 必回复只剩一次生效）。高精度匹配 + 每循环至多一次补问兜底：
# 误判代价 = 多一轮 LLM 调用，漏判代价 = 被点名后沉默，两害取轻。
_REPLY_INTENT_RE = re.compile(
    r"(?:让我|我要|我应该|需要|准备|现在就|接下来|打算)"
    r"[^。！？\n]{0,24}(?:回复|回应|回答)"
    r"|(?:用|以)[^。！？\n]{0,16}身份(?:来)?(?:回复|回应)"
    r"|决定[^。！？\n]{0,12}(?:回复|回应|回答)"
)
_REPLY_INTENT_NEG_RE = re.compile(
    r"(?:不|没|无需|没必要)[^。！？\n]{0,6}(?:回复|回应|回答)"
)


def narrated_reply_intent(text: str) -> bool:
    """空动作轮文本是否含叙述性回复意图（先剥否定式再匹配，防误判）。"""
    t = _REPLY_INTENT_NEG_RE.sub("□", str(text or ""))
    return bool(_REPLY_INTENT_RE.search(t))


# 补问轮注入文案（<system-reminder> 包裹，与 deferred tools 提醒同形态）
REPAIR_NO_TOOL_CALL = (
    "<system-reminder>你上一轮的分析已经决定要回复，但没有发出任何工具调用。"
    "请立即调用 reply 工具完成这次回复；如果你重新权衡后确实决定不回复，"
    "直接结束即可，不要再叙述回复计划。</system-reminder>"
)

# 表情检索词子提示（emoji_selection.prompt 的 maisoul 适配：MaiBot 用视觉子代理
# 从 25 宫格选编号，stealer 生态是关键词检索 → 让子 LLM 产检索词）
EMOJI_QUERY_PROMPT = """{chat_context}

你需要根据上面的聊天内容和当前语气，为{bot_name}选择一个此刻发送的表情包。
请提炼一个用于表情包检索的关键词或短语：优先写图上可能出现的字、角色名或画面描述，不要只写英文分类名，例如"开心到转圈""无语""猫猫震惊"。
当前分析参考：{reason}

请只输出这个检索词本身，不要输出任何其他内容。"""

# 表情候选挑选子提示（对齐 MaiBot send_emoji 子代理的「从候选选编号」语义：
# 原版是 VLM 看 25 宫格拼图选号；stealer 生态里候选带文字元数据，纯文本挑号）
EMOJI_PICK_PROMPT = """{chat_context}

你需要为{bot_name}从下面的表情包候选中选出此刻发送的一张，要求最贴合当前聊天的语气、话题与画面内容。
当前分析参考：{reason}

候选表情包：

{candidates}

请只输出选中的编号数字（如 3），不要输出任何其他内容。"""


def parse_meme_candidates(search_result: str, limit: int = 10) -> list[dict]:
    """从 stealer search_meme 的候选列表文本解析 [{num, text}]。

    候选块以「[N] 分类：」起始，其后缩进行（角色/图上文字/标签/描述等）
    并入所属候选；只保留前 limit 个（挑选调用保持小体积）。
    """
    out: list[dict] = []
    for ln in str(search_result or "").splitlines():
        m = re.match(r"^\[(\d+)\]\s*(.*)$", ln.strip())
        if m:
            if len(out) >= max(1, limit):
                break
            out.append({"num": int(m.group(1)), "text": m.group(2).strip()})
        elif out and ln.startswith("    ") and ln.strip():
            out[-1]["text"] += "；" + ln.strip()
    return out


def pick_meme_index(reply: str, nums: list[int]) -> int | None:
    """从挑选模型回复中取第一个落在候选编号集内的整数（越界跳过）。"""
    for tok in re.findall(r"\d+", str(reply or "")):
        v = int(tok)
        if v in nums:
            return v
    return None


WAIT_TOOL_RESULT = (
    "当前对话循环进入等待状态，将固定等待 {seconds} 秒；期间收到的新消息不会提前打断本次等待。"
    "连续 wait 次数：{current}/{maximum}。"
)
WAIT_LIMIT_RESULT = "连续 wait 已达到上限 {maximum} 次，本次不再进入等待；视为多次等待后仍无后续，当前对话进入休息。"

# wait 完成回执（对齐 reasoning_engine._build_wait_completed_message 原文；
# requested 为 None 时省略"原计划"段）
WAIT_COMPLETED_HAS_NEW = (
    "等待已结束，实际等待 {elapsed:.1f} 秒{requested_text}，"
    "期间收到了新的用户输入。请结合这些新消息继续下一轮思考。"
)
WAIT_COMPLETED_TIMEOUT = (
    "等待已超时，实际等待 {elapsed:.1f} 秒{requested_text}，"
    "期间没有收到新的用户输入。请基于现有上下文继续下一轮思考。"
)


def build_wait_completed_message(
    elapsed: float, requested: float | None, has_new_messages: bool
) -> str:
    requested_text = f"，原计划等待 {requested:.1f} 秒" if requested is not None else ""
    template = WAIT_COMPLETED_HAS_NEW if has_new_messages else WAIT_COMPLETED_TIMEOUT
    return template.format(elapsed=elapsed, requested_text=requested_text)


FOLDED_TOOL_HISTORY_PREFIX = "[已折叠的历史工具调用]"
TURN_CONTEXT_KEEP_COUNT = (
    6  # 保留最近 3 组 user/assistant（对齐 ASSISTANT_OPTIMIZATION_KEEP_COUNT=3）
)


def fold_old_turns(
    contexts: list[dict], turn_start: int, keep: int = TURN_CONTEXT_KEEP_COUNT
) -> None:
    """本轮循环产生的旧轮次折叠（对齐 _build_trimmed_assistant_tool_user_message：
    保留最近 3 组 user/assistant，更早的一次性折叠为「[已折叠的历史工具调用]」摘要。
    MaiBot 保留工具调用详情且超 1024 字符转 Complex 消息；maisoul 无该基建，
    精简为逐条摘要文本——防长循环 contexts 无限膨胀。单趟折叠（while 逐对重折
    会在折叠块自身上 1 换 1 死循环）。
    边界对齐配对（v6.20.3）：折叠区终点落在 tool 回执上时，其配对的
    assistant(tool_calls) 已进折叠区，保留区开头会出现孤儿 tool 轮——OpenAI
    类 Provider 的协议校验会拒收整轮请求。终点回退到配对 assistant 之前，
    让整对一起保留（keep 是软上限，配对完整性优先）。"""
    excess = len(contexts) - turn_start - keep
    if excess <= 0:
        return
    fold_end = turn_start + excess
    while fold_end > turn_start and contexts[fold_end].get("role") == "tool":
        fold_end -= 1
    lines = [
        f"- {m.get('role')}: " + " ".join(str(m.get("content") or "").split())[:80]
        for m in contexts[turn_start:fold_end]
    ]
    contexts[turn_start:fold_end] = [
        {
            "role": "user",
            "content": FOLDED_TOOL_HISTORY_PREFIX + "\n" + "\n".join(lines),
        }
    ]


def build_history_contexts(
    history_msgs: list[dict], analyses, context_limit: int, is_group: bool = True
) -> tuple[list[dict], list[dict]]:
    """合并聊天历史与历史 planner 分析，按时间戳交错构建 contexts 初始段。

    对齐 MaiBot 会话历史机制：build_model_output_context_messages 把 planner
    每轮输出写入 _chat_history，select_llm_context_messages 在「聊天消息+分析+
    工具结果」合并流上从新往旧按 2× 稳定窗选取——模型每轮都能看到自己先前
    轮次的分析（assistant 轮），输出结构由此自我强化。这是部署版 planner 分析
    呈「当前状态/分析/下一步」格式的来源（提示词并无此要求）；maisoul 此前
    每轮从聊天记录重建、分析不回灌，格式零样本漂移（v6.13.4 补齐）。

    角色分工对齐部署版请求 dump：全部聊天消息（含自发消息，带
    is_self_message="true"）进 user 轮（<message> 前缀），只有 planner 分析
    进 assistant 轮（v6.13.5 修正——旧版自发消息进 assistant 轮是误对齐）。
    窗口在合并流上截取（对齐 MaiBot 按合并条数计数）；返回
    (contexts, included_chat_msgs)，后者为进入窗口的聊天消息。
    跨日时间行规则与旧实现一致（相邻项跨天时插入「时间：YYYY-MM-DD HH:MM:SS」）。
    """
    merged: list[dict] = [
        {"kind": "chat", "ts": float(m.get("ts") or 0), "msg": m}
        for m in history_msgs
        if str(m.get("text") or "").strip()
    ]
    merged += [
        {
            "kind": "analysis",
            "ts": float(a.get("ts") or 0),
            "text": str(a.get("text") or "").strip(),
        }
        for a in analyses
        if str(a.get("text") or "").strip()
    ]
    # 稳定排序：同 ts 时聊天消息在前（先插入，分析是响应、天然晚于触发消息）
    merged.sort(key=lambda x: x["ts"])
    if context_limit > 0:
        merged = merged[-context_limit:]
    contexts: list[dict] = []
    included_chat: list[dict] = []
    _last_day = None
    for item in merged:
        day = _dt.fromtimestamp(item["ts"]).date()
        if _last_day is not None and day != _last_day:
            contexts.append(
                {
                    "role": "user",
                    "content": (
                        f"时间：{_dt.fromtimestamp(item['ts']).strftime('%Y-%m-%d %H:%M:%S')}"
                    ),
                }
            )
        _last_day = day
        if item["kind"] == "analysis":
            contexts.append({"role": "assistant", "content": item["text"]})
            continue
        contexts.append(
            {"role": "user", "content": render_planner_message(item["msg"], is_group)}
        )
        included_chat.append(item["msg"])
    return contexts, included_chat


def split_pending(
    records: list[dict], last_cycle_ts: float
) -> tuple[list[dict], list[dict]]:
    """按排水水位切分 (pending, history)：pending = ts 晚于水位的外部消息
    （planner 循环内经 _drain_pending 注入）。

    pending 判定的单一实现——planner 循环与 fetch_chat_history 的排除集
    共用，防两处各写一份表达式后口径漂移（v6.20.1 盲区修复）。"""
    pending: list[dict] = []
    history: list[dict] = []
    for m in records or []:
        if float(m.get("ts") or 0) > last_cycle_ts and str(m.get("sid")) != "self":
            pending.append(m)
        else:
            history.append(m)
    return pending, history


REPLY_TOOL_SPEC = {
    "type": "object",
    "properties": {
        "msg_id": {"type": "string", "description": "要回复的消息msg_id。"},
        "set_quote": {
            "type": "boolean",
            "description": "以引用回复的方式发送这条回复，当发言人数过多，聊天比较乱时使用。",
            "default": True,
        },
        "reply_reference": {
            "type": "string",
            "description": "有助于回复的信息，包括当前聊天状态、人物关系、事实信息、回忆信息。",
        },
        "reply_style": {
            "type": "string",
            "description": "可选。控制本次回复的篇幅和表达方式；正常回复不会附加额外要求。",
            "enum": ["简短表达", "正常回复", "长回复"],
        },
    },
    "required": ["msg_id"],
}
WAIT_TOOL_SPEC = {
    "type": "object",
    "properties": {"seconds": {"type": "integer", "description": "等待秒数。"}},
    "required": ["seconds"],
}
# fetch_history 不暴露：MaiBot focus 模式专属工具（_is_builtin_tool_enabled_by_config
# 要求 experimental.focus_mode，部署版 false → 工具集里没有它），v6.13.5 移除
# tool_search 声明 = MaiBot builtin_tool/tool_search.py 原文
TOOL_SEARCH_SPEC = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "要搜索的工具名、前缀或关键词。"},
        "limit": {
            "type": "integer",
            "description": "最多返回多少个工具。",
            "minimum": 1,
        },
    },
    "required": ["query"],
}

TOOL_SEARCH_NO_HIT = (
    "未找到匹配的 deferred tools，请尝试更完整的工具名、前缀或其他关键词。"
)


def tool_search_no_hit_text(pool: list[dict], discovered) -> str:
    """未命中回执 = MaiBot 原文提示 + maisoul 扩展纠正段（MaiBot 没有的防呆）。

    烂 query（自然语言描述/占位符串，如 "context history message"、
    "xx是什么意思"）是模型漂移的常见形态——MaiBot 只回一句提示，模型
    可能连着重试同类 query 空转烧轮次；扩展段列出当前仍可发现的
    deferred 工具名并给出明确重试格式，下一轮几乎必然修正。
    池空或全部已发现时退回原文提示。清单封顶 20 个防刷屏。"""
    names: list[str] = []
    for item in pool or []:
        name = str(item.get("name") or "").strip()
        if name and name not in (discovered or set()) and name not in names:
            names.append(name)
    if not names:
        return TOOL_SEARCH_NO_HIT
    return (
        TOOL_SEARCH_NO_HIT
        + "\n当前可搜索的 deferred tools："
        + "、".join(names[:20])
        + "。请直接用以上工具名（或其前缀）作为 query 重试，不要用自然语言描述。"
    )


def search_deferred_tools(pool: list[dict], query: str, limit: int = 5) -> list[dict]:
    """按 MaiBot runtime.search_deferred_tool_specs 的分数表匹配 deferred 工具：
    精确=1000 / 名称前缀=300 / 名称包含=200 / 描述包含=100 / 分词名称=25 / 分词描述=10，
    同分按名称排序，取前 limit 个。"""
    import re as _re

    normalized = str(query or "").strip().lower()
    if not normalized:
        return []
    terms = [t for t in _re.split(r"[_\- ]+", normalized) if t]
    scored: list[tuple[int, dict]] = []
    for item in pool:
        name = str(item.get("name") or "").lower()
        desc = str(item.get("description") or "").lower()
        score = 0
        if normalized == name:
            score += 1000
        if name.startswith(normalized):
            score += 300
        if normalized in name:
            score += 200
        if normalized in desc:
            score += 100
        for term in terms:
            if term in name:
                score += 25
            if term in desc:
                score += 10
        if score > 0:
            scored.append((score, item))
    scored.sort(key=lambda x: (-x[0], str(x[1].get("name") or "")))
    return [item for _, item in scored[: max(1, int(limit))]]


def build_deferred_reminder(deferred_pool: list[dict], discovered: set) -> str:
    """未发现的 deferred 工具 → <system-reminder> 提醒（模板 = runtime.build_deferred_tools_reminder 原文）。

    已发现的工具不再列出（它们已作为真实工具定义可见）。
    """
    lines = []
    for item in deferred_pool:
        name = str(item.get("name") or "").strip()
        if not name or name in discovered:
            continue
        desc = str(item.get("description") or "").strip()
        lines.append(f"{name}: {desc}" if desc else name)
    if not lines:
        return ""
    numbered = [f"{index}. {line}" for index, line in enumerate(lines, start=1)]
    return "\n".join(
        [
            "<system-reminder>",
            "以下工具当前未直接暴露给你，但可以通过 tool_search 工具发现并在后续轮次中使用：",
            *numbered,
            "",
            "如需其中某个工具，请先调用 tool_search。tool_search 只负责发现工具，不直接执行。",
            "</system-reminder>",
        ]
    )


def tool_search_result_text(hits: list[dict], discovered_after: set) -> str:
    """tool_search 返回文本（格式 = tool_search.py 原文）。"""
    lines = []
    for item in hits:
        name = str(item.get("name") or "")
        fresh = name not in discovered_after
        lines.append(f"- {name}（{'本次新发现' if fresh else '此前已发现'}）")
    return "已找到 " + str(
        len(hits)
    ) + " 个 deferred tools，" "它们会在后续轮次中加入可用工具列表：\n" + "\n".join(lines)


def build_planner_system(cfg, attention_block: str) -> str:
    behavior_style = str(cfg.get("behavior_style") or "").strip()
    if not behavior_style:  # 兼容拆分前配置：旧版 Planner 直接使用人格配置
        behavior_style = str(cfg.get("personality") or "").strip()
    return PLANNER_SYSTEM_TEMPLATE.format(
        bot_name=str(cfg.get("bot_name") or "麦麦"),
        behavior_style=behavior_style,
        group_chat_attention_block=attention_block,
        query_memory_rule="",  # query_memory 由 livingmemory 承担（取舍），不暴露工具
    )


def _attr_escape(value: str) -> str:
    """XML 属性值转义（对齐 xml.sax.saxutils.escape(quote=True)：& < > \" ）。"""
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _format_quote_ids(quote: str) -> str:
    """引用目标 ID 列表 → 属性值（对齐 planner_messages._format_quote_ids：去重 + 逗号拼接）。"""
    ids: list[str] = []
    seen: set[str] = set()
    for raw in str(quote or "").split(","):
        qid = raw.strip()
        if not qid or qid in seen:
            continue
        seen.add(qid)
        ids.append(qid)
    return ",".join(ids)


def render_planner_message(m: dict, is_group: bool = True) -> str:
    """单条消息 → 部署版 planner 前缀格式（对齐 maisaka/context/planner_messages.
    build_planner_prefix 原文）：<message msg_id="…" [quote="…"] time="…"
    user="…" [group_card="…"] [is_self_message="true"]>\\n内容（无闭合标签）。

    近似项：quote=消息记录里的引用目标（_record 从 Reply 组件提取）；
    group_card=群聊时用发送者名（AstrBot 事件侧群名片与昵称同源）；自发消息
    标 is_self_message，msg_id 恒空（坑 21：发送回执拿不到 message_id）。
    旧版 HH:MM:SS[msg_id:x][说话人] 是 format_speaker_content 的可见文本
    格式，不是 planner 请求格式（v6.13.2 误对齐，v6.13.5 修正）。
    """
    ts = float(m.get("ts") or time.time())
    name = str(m.get("name") or "").strip()
    text = str(m.get("text") or "").strip()
    attrs = [f'msg_id="{_attr_escape(str(m.get("msg_id") or "").strip())}"']
    quote = _format_quote_ids(str(m.get("quote") or ""))
    if quote:
        attrs.append(f'quote="{_attr_escape(quote)}"')
    attrs.append(f'time="{_dt.fromtimestamp(ts).strftime("%H:%M:%S")}"')
    attrs.append(f'user="{_attr_escape(name)}"')
    if is_group and str(m.get("sid")) != "self":
        attrs.append(f'group_card="{_attr_escape(name)}"')
    if str(m.get("sid")) == "self":
        attrs.append('is_self_message="true"')
    return f"<message {' '.join(attrs)}>\n" + text


# 每轮 Planner 请求末尾的一次性 user 提醒（chat_loop_service.
# PLANNER_FINAL_USER_REMINDER_TEMPLATE 原文，v6.13.2 补齐）
PLANNER_FINAL_USER_REMINDER = (
    "你需要输出对{bot_name}发言的分析，视情况输出文本内容的分析，思考是否进行工具调用"
)


@dataclass
class PlannerState:
    """单群 Planner 运行时（WAIT/RUNNING 状态机 + 退避 + 打断）。"""

    agent_state: str = "idle"  # idle / running / wait
    cycle_gen: int = 0  # 循环代际号（M3：打断后旧代退出不得回写状态）
    consecutive_wait_count: int = 0
    wait_until: float = 0.0
    backoff_count: int = 0
    backoff_until: float = 0.0
    running_task: object = None
    interrupt_count: int = 0
    last_analysis: str = (
        ""  # 上一轮 planner 思考（防复读比对用，对齐 _should_replace_reasoning）
    )
    analysis_log: deque = field(
        default_factory=lambda: deque(maxlen=200)
    )  # 历史分析 {ts,text}（跨轮回灌，对齐 build_model_output_context_messages 写会话历史；2× 稳定窗外的旧条目随界淘汰）
    discovered_tools: set = field(
        default_factory=set
    )  # tool_search 已发现的 deferred 工具（会话级；MaiBot 跟上下文裁切走，此处简化）
    last_event: object = (
        None  # 最近一次真实触发 event（wait 续轮复用：候选列表挂 event 上，换对象=candidate_expired）
    )
    eco_injection: str = ""  # 本轮 replyer 收集的生态注入全文（观察页展示用，轮始清空）
    last_cycle_ts: float = 0.0  # 上一轮消费到的消息时间戳（pending 排水用）
    umo: str = ""  # wait 恢复续轮所需的发送上下文
    platform: str = ""
    is_group: bool = True

    # ---------------- wait 状态机（对齐 _try_enter_wait_state） ----------------
    def begin_cycle(self) -> int:
        """开启新循环并返回其代际号（打断 cancel 旧循环后由新循环调用）。"""
        self.cycle_gen += 1
        return self.cycle_gen

    def set_idle_if_current(self, gen: int) -> None:
        """循环退出置 idle 的代际守卫（M3）。

        打断流程是 cancel 旧任务 → 置 running → 开新循环；被取消的旧任务
        在下一个 await 点才收到 CancelledError，其退出路径若直接回写
        agent_state="idle" 会清掉新循环状态（后续消息误判 idle 再开一
        循环 → 双循环并发）。只有代际号仍是自己时才允许回写。"""
        if self.cycle_gen == gen:
            self.agent_state = "idle"

    def try_enter_wait(self, cfg, seconds: int) -> tuple[bool, int, int]:
        maximum = max(1, int(cfg.get("max_consecutive_wait_count", 3)))
        if self.consecutive_wait_count >= maximum:
            return False, self.consecutive_wait_count, maximum
        self.consecutive_wait_count += 1
        self.agent_state = "wait"
        self.wait_until = time.time() + max(0, seconds)
        return True, self.consecutive_wait_count, maximum

    def in_wait(self) -> bool:
        return self.agent_state == "wait" and time.time() < self.wait_until

    def resume_from_wait(self) -> bool:
        """主动触发（@/提及必回）从 wait 恢复运行（对齐 _resume_from_wait_for_proactive_trigger）。"""
        if self.agent_state != "wait":
            return False
        self.agent_state = "idle"
        self.wait_until = 0.0
        return True

    # ---------------- 空闲退避（对齐 IdleBackoffController） ----------------
    def record_idle_cycle(self, cfg):
        base = max(0.0, float(cfg.get("no_action_backoff_base_seconds", 15)))
        cap = max(0.0, float(cfg.get("no_action_backoff_cap_seconds", 300)))
        if base <= 0 or cap <= 0:
            return
        start_count = max(1, int(cfg.get("no_action_backoff_start_count", 2)))
        self.backoff_count += 1
        if self.backoff_count < start_count:
            return
        exponent = max(0, self.backoff_count - start_count)
        self.backoff_until = time.time() + min(cap, base * (2**exponent))

    def reset_backoff(self):
        self.backoff_count = 0
        self.backoff_until = 0.0

    def should_delay(self, cfg, pending_count: int) -> bool:
        if self.backoff_until <= 0 or time.time() >= self.backoff_until:
            self.backoff_until = 0.0
            return False
        bypass = max(0, int(cfg.get("no_action_backoff_bypass_pending_count", 6)))
        if bypass > 0 and pending_count >= bypass:
            return False
        return True


def build_planner_toolset(deps) -> "object":
    """构造 planner 可见工具集（reply/wait/send_emoji/tool_search，声明=
    MaiBot 原文；部署版 focus_mode=false 下 fetch_history/switch_chat 不暴露，
    对齐 dump 实测的 7 工具集中 maisoul 有对应物的 4 个）。已发现的 deferred
    工具由调用方追加进 ToolSet。deps 需提供：on_reply / on_wait /
    on_send_emoji / on_tool_search。
    """
    from . import bridge

    ToolSet, FunctionTool = bridge.planner_tool_classes()

    async def _reply(**kwargs):
        return await deps.on_reply(kwargs)

    async def _wait(**kwargs):
        return deps.on_wait(kwargs)

    async def _send_emoji(**kwargs):
        return await deps.on_send_emoji()

    def _tool_search(**kwargs):
        return deps.on_tool_search(kwargs)

    tool_set = ToolSet()
    tool_set.add_tool(
        FunctionTool(
            name="reply",
            description="根据当前思考生成并发送一条可见回复。",
            parameters=REPLY_TOOL_SPEC,
            handler=_reply,
        )
    )
    tool_set.add_tool(
        FunctionTool(
            name="wait",
            description="暂停当前对话并固定等待一段时间。",
            parameters=WAIT_TOOL_SPEC,
            handler=_wait,
        )
    )
    tool_set.add_tool(
        FunctionTool(
            name="send_emoji",
            description="发送一个表情包来表达情绪，参与聊天。",
            parameters={"type": "object", "properties": {}},
            handler=_send_emoji,
        )
    )
    tool_set.add_tool(
        FunctionTool(
            name="tool_search",
            description="在 deferred tools 列表中按名称或关键词搜索工具，并将命中的工具加入后续轮次的可用工具列表。",
            parameters=TOOL_SEARCH_SPEC,
            handler=_tool_search,
        )
    )
    return tool_set


class PlannerHost(Protocol):
    """planner 工具回调的宿主接口（M9）。

    由 pipeline/planner_host 的适配器提供；planner 只依赖本协议，
    不再感知插件对象（解 main↔planner 双向耦合——原 PlannerDeps.plugin
    直接回调插件私有方法，拆分后即断）。"""

    async def planner_execute_reply(self, deps, reason: str, args: dict) -> str: ...
    def planner_schedule_wait_resume(self, st, cfg, gid: str, seconds: int) -> None: ...
    async def planner_send_emoji(self, deps) -> str: ...


class PlannerDeps:
    """把 planner 工具回调绑定到宿主（PlannerHost）的 replyer/表情桥/消息缓冲。"""

    def __init__(
        self,
        host: PlannerHost,
        st,
        eff_cfg,
        event,
        platform: str,
        gid: str,
        is_group: bool = True,
        send_fn=None,
    ):
        self.host = host
        self.st = st
        self.cfg = eff_cfg
        self.event = event
        self.platform = platform
        self.gid = gid
        self.is_group = is_group
        self.umo = ""
        self.latest_reason = ""
        self.send_fn = send_fn  # WebUI 聊天页等需要走 event.send 流式回填的发送通道
        self.deferred_pool: list[dict] = (
            []
        )  # [{name, description, tool}]，由 _planner_cycle 注入

    async def on_reply(self, args: dict) -> str:
        return await self.host.planner_execute_reply(self, self.latest_reason, args)

    def on_wait(self, args: dict) -> str:
        try:
            seconds = int(args.get("seconds", 30))
        except (TypeError, ValueError):
            seconds = 30
        entered, current, maximum = self.st.planner.try_enter_wait(self.cfg, seconds)
        if not entered:
            return WAIT_LIMIT_RESULT.format(maximum=maximum)
        self.host.planner_schedule_wait_resume(self.st, self.cfg, self.gid, seconds)
        return WAIT_TOOL_RESULT.format(
            seconds=max(0, seconds), current=current, maximum=maximum
        )

    async def on_send_emoji(self) -> str:
        return await self.host.planner_send_emoji(self)

    def on_tool_search(self, args: dict) -> str:
        """tool_search 执行：打分匹配 deferred 池 → 命中记入 discovered_tools（下一轮可用）。"""
        try:
            limit = max(1, int(args.get("limit", 5) or 5))
        except (TypeError, ValueError):
            limit = 5
        hits = search_deferred_tools(
            self.deferred_pool, str(args.get("query") or ""), limit
        )
        if not hits:
            # 未命中走纠正回执（附可发现工具名清单，防烂 query 连续空转）
            return tool_search_no_hit_text(
                self.deferred_pool, self.st.planner_state().discovered_tools
            )
        discovered = self.st.planner_state().discovered_tools
        # 新发现判定要在更新前做（MaiBot 同款标记）
        result = tool_search_result_text(hits, set(discovered))
        if len(discovered) >= 256:  # M12：会话级集合封顶（清后可重发现，无行为损失）
            discovered.clear()
        discovered.update(str(h.get("name")) for h in hits)
        return result
