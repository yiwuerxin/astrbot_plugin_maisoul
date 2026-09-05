"""MaiBot 三件套 prompt 组装 —— 复刻 maisaka_generator_base 的各 build 方法"""

import random
from datetime import datetime

from .constants import OUTPUT_INSTRUCTION
from .states import GroupState


def build_identity(cfg) -> str:
    """复刻 _build_personality_prompt：名字+别名+人格设定。"""
    bot_name = str(cfg.get("bot_name") or "麦麦").strip()
    aliases = [str(a).strip() for a in (cfg.get("aliases") or []) if str(a).strip()]
    alias_part = f"，也有人叫你{','.join(aliases)}" if aliases else ""
    personality = str(cfg.get("personality") or "").strip() or "是人类。"
    return f"你的名字是{bot_name}{alias_part}。\n{personality}"


def select_reply_style(cfg) -> str:
    """基础说话风格 + 临时备用风格彩票（复刻 _select_temporary_reply_style）。"""
    style = str(cfg.get("reply_style") or "").strip()
    candidates = [str(s).strip() for s in (cfg.get("multiple_reply_style") or []) if str(s).strip()]
    prob = float(cfg.get("multiple_probability", 0) or 0)
    if candidates and prob > 0 and random.random() * 100 < prob:
        style += f"\n本次临时风格（仅本次回复生效）：{random.choice(candidates)}"
    return style


def build_attention_block(cfg, chat_id: str | None = None, platform: str | None = None,
                          is_group: bool = True,
                          include_chat_prompt: bool = True) -> str:
    """复刻 _build_group_chat_attention_block：通用注意事项（群聊/私聊各自提示词）。

    部署版的「当前聊天额外注意事项」（chat_prompts 精确匹配）不在系统提示词里，
    而是每次请求末尾的独立 user 消息（_build_current_chat_attention_tail_message，
    v6.13.5 修正）——planner 走 include_chat_prompt=False + chat_attention_tail；
    replyer 侧沿用合并形态（include_chat_prompt 默认 True，行为不变）。
    """
    lines = []
    key = "group_chat_prompt" if is_group else "private_chat_prompts"
    prompt = str(cfg.get(key) or "").strip()
    if prompt:
        lines.append(f"通用注意事项：\n{prompt}")
    if include_chat_prompt:
        extra = _match_chat_prompt(cfg, chat_id, platform, is_group)
        if extra:
            lines.append(f"当前聊天额外注意事项：\n{extra}")
    if not lines:
        return ""
    return "在该聊天中的注意事项：\n" + "\n\n".join(lines) + "\n"


def chat_attention_tail(cfg, chat_id: str | None, platform: str | None = None,
                        is_group: bool = True) -> str:
    """chat_prompts 命中 → 请求末尾的独立 user 消息（对齐部署版
    _build_current_chat_attention_tail_message 原文格式）。"""
    extra = _match_chat_prompt(cfg, chat_id, platform, is_group)
    if not extra:
        return ""
    return f"当前聊天额外注意事项：\n{extra}"


def _match_chat_prompt(cfg, chat_id: str | None, platform: str | None = None,
                       is_group: bool = True) -> str:
    """chat_prompts: [{platform, item_id, rule_type, prompt}] —— 复刻 ChatConfigUtils.
    _iter_matching_chat_prompts：精确匹配 platform+item_id，多条命中以换行拼接。"""
    if not chat_id:
        return ""
    matched = []
    for item in (cfg.get("chat_prompts") or []):
        if not isinstance(item, dict):
            continue
        p = str(item.get("platform") or "").strip()
        item_id = str(item.get("item_id") or "").strip()
        rule_type = str(item.get("rule_type") or "group").strip()
        content = str(item.get("prompt") or "").strip()
        if not p or not item_id or not content:
            continue
        if rule_type != ("group" if is_group else "private"):
            continue
        if p == (platform or "") and item_id == chat_id:
            matched.append(content)
    return "\n".join(matched)


def build_preset_dialogues_block(cfg) -> str:
    """预设对话（maisoul 扩展）：示例对话注入系统提示词作为说话风格参考。

    条目 {user, reply} 均非空才收录；只示范语气与习惯，明确告知不照搬内容。
    """
    items = []
    for d in (cfg.get("preset_dialogues") or []):
        if not isinstance(d, dict):
            continue
        u = str(d.get("user") or "").strip()
        r = str(d.get("reply") or "").strip()
        if u and r:
            items.append((u, r))
    if not items:
        return ""
    lines = ["【预设对话】以下示例展示你应有的说话风格，只参考语气与用词习惯，不要照搬内容："]
    lines += [f"用户：{u}\n你：{r}" for u, r in items]
    return "\n".join(lines) + "\n"


def build_system_prompt(cfg, chat_id: str | None = None, platform: str | None = None,
                        is_group: bool = True) -> str:
    """逐行复刻 prompts/zh-CN/maisaka_replyer.prompt 的结构。"""
    from .sanitize import ANTI_INJECTION_LINES

    base = (
        f"{build_identity(cfg)}\n"
        "现在请你读读之前的聊天记录，把握当前的话题，然后给出日常且口语化的回复，\n"
        f"{select_reply_style(cfg)}\n"
        "你可以参考【回复信息参考】中的信息，但是视情况而定，不用完全遵守。\n"
        f"{build_attention_block(cfg, chat_id, platform, is_group)}"
        f"{build_preset_dialogues_block(cfg)}"
        f"{OUTPUT_INSTRUCTION}"
    )
    if bool(cfg.get("anti_injection", True)):  # P-F：防注入声明（默认开）
        base += ANTI_INJECTION_LINES
    return base


REPLY_INSTRUCTION = "请自然地回复。不要输出多余说明、括号、@ 或额外标记，只输出实际要发言的内容。"

# reply 工具 reply_style 参数的篇幅指令（maisaka_generator_base.
# _build_requested_reply_style_message 三档原文；"正常回复"为空=不注入）
REPLY_STYLE_INSTRUCTIONS = {
    "简短表达": "请简短的回复，允许句子残缺，奇怪表达，倒装，省略，符合口语习惯，符合省力随意回复习惯",
    "正常回复": "",
    "长回复": "可以针对问题做出较为详细的评论和说明",
}


def image_context_parts(st: "GroupState", cfg, max_num: int | None = None) -> list:
    """识图进上下文（v6.9.9，MaiBot [visual] 的精简等价）：

    取最近 max_num 张聊天图片引用 → ImageURLPart 列表（旧→新），经 text_chat 的
    extra_user_content_parts 附给模型（openai 实现序列化为标准 image_url 段）。
    总开关 enable_image_context 默认关（需视觉模型）；MaiBot 的识图等待/大图
    压缩/图片缓存属其自有基建，不在此范围。
    """
    if not cfg.get("enable_image_context", False):
        return []
    limit = max_num if max_num is not None else max(0, int(cfg.get("image_context_max_num", 3)))
    if limit == 0:
        return []
    refs: list[str] = []
    for m in reversed(list(st.buffer)):  # 新→旧收集，消息内也逆序，保证翻转后严格旧→新
        for ref in reversed([str(r or "").strip() for r in (m.get("images") or [])]):
            if ref and ref not in refs:  # 去重按最新出现计
                refs.append(ref)
        if len(refs) >= limit:
            break
    if not refs:
        return []
    try:
        from astrbot.core.agent.message import ImageURLPart
    except ImportError:
        return []
    # image_url 字段要 dict/ImageURL 实例（裸字符串会被 pydantic 拒绝，docstring 示例有误导）
    return [ImageURLPart(image_url={"url": r}) for r in reversed(refs[:limit])]


def _optimize_transcript(buf: list[dict], bot_name: str, keep: int = 3) -> list[dict]:
    """优化上下文（对应 chat.enable_context_optimization）：自己的旧发言只保留最近几条。"""
    self_idx = [i for i, m in enumerate(buf) if str(m.get("name")) == bot_name]
    drop = set(self_idx[:-keep]) if len(self_idx) > keep else set()
    return [m for i, m in enumerate(buf) if i not in drop]


def build_final_user_message(
    st: GroupState, cfg, reason: str, style: str = "",
    *, expression_habits: str = "", jargon_reference: str = "",
    keyword_reaction: str = "", reference_override: str = "",
    is_group: bool = True,
) -> str:
    """复刻 _build_final_user_message 的段结构。

    差异：MaiBot 把聊天记录/表达习惯/黑话参考作为独立上下文消息传入；AstrBot
    text_chat 单轮调用，故内联为等价块。reference_override 对齐
    _build_reply_reference_lines：显式 reference（planner reply 工具的
    reply_reference）优先；style 为 reply 工具的 reply_style 参数。
    keyword_reaction 位置对齐 MaiBot（在结尾指令前）。
    """
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    bot_name = str(cfg.get("bot_name") or "麦麦")
    context_key = "max_context_size" if is_group else "max_private_context_size"
    buf = list(st.buffer)[-int(cfg.get(context_key, 40 if is_group else 60)):]
    if cfg.get("enable_context_optimization", True):
        from .learning import ASSISTANT_OPTIMIZATION_KEEP_COUNT
        buf = _optimize_transcript(buf, bot_name, ASSISTANT_OPTIMIZATION_KEEP_COUNT)
    transcript = "\n".join(
        f"{m['name']}{'(@了我)' if m['at_bot'] else ''}: {m['text']}" for m in buf
    )

    reference = reference_override.strip() or (f"当前思考：\n{reason}" if reason else "")
    if style:
        # 篇幅指令 = MaiBot 三档原文（"正常回复"映射空串=完全不注入）；
        # 独立注入（MaiBot 是独立 user 消息，此处内联，且不再依赖
        # reference_override 为空——planner 模式恒有 reference，旧写法漏注入）
        instruction = REPLY_STYLE_INSTRUCTIONS.get(style)
        if instruction:
            reference += f"\n{instruction}"
        elif style not in REPLY_STYLE_INSTRUCTIONS:
            reference += f"\n本次回复篇幅要求：{style}。"

    # 同目标防重复提醒 —— MaiBot reply 工具 _DUPLICATE_TARGET_REPLY_REMINDER_TEMPLATE 原文
    dup_reminder = ""
    if buf and st.recently_replied(buf[-1].get("msg_id", "")):
        prev = str(st.reply_by_target.get(buf[-1].get("msg_id", "")) or "").strip()
        if not prev:
            prev = "（刚才的发言）"
        dup_reminder = (
            f"你刚刚已经回复过这条消息，你刚刚的发言是：“{prev}”\n"
            "你现在想再次回复这条消息，进行补充，注意请不要和之前你的发言重复。"
        )

    sections = [f"当前时间：{current_time}"]
    if transcript:
        sections.append(f"【最近群聊记录】\n{transcript}")
    if expression_habits:
        sections.append(expression_habits.strip())
    if jargon_reference:
        sections.append(jargon_reference.strip())
    if reference:
        sections.append(f"【回复信息参考】\n{reference}")
    if dup_reminder:
        sections.append(dup_reminder)
    if keyword_reaction:
        sections.append(keyword_reaction.strip())
    sections.append(REPLY_INSTRUCTION)
    return "\n\n".join(sections)
