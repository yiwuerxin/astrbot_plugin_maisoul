"""聊天学习子系统 —— 源码级移植 MaiBot 表达学习/黑话/关键词反应

三个直接影响聊天的机制（对标文件与格式均取自 MaiBot 源码原文）：
1. 关键词反应（keyword_reaction）：最新用户消息命中 keyword_rules/regex_rules
   → final message 注入【关键词反应】块；正则用 [命名捕获组] 占位替换
2. 表达习惯（expression）：学习库 {situation, style, count, checked}；
   legacy 使用 = 加权抽样（库 ≥10 条才启用，高频 count>1 抽 5 + 全库抽 5，
   去重）→ 注入【表达习惯参考，请视情况自然的使用】块
   （MaiBot 的 LLM 二次选择路径未接，走其"直接注入"路径）
3. 黑话（jargon）：学习库 {content, meaning, count}；最近上下文消息文本命中
   词条 → 注入【黑话参考】块（上限 10 条）

学习器（发言后异步运行，provider 为 AstrBot 当前模型）：
- learn_style.prompt / learn_jargon.prompt / jargon_inference_with_context.prompt /
  expression_evaluation.prompt 均为 MaiBot prompts/zh-CN 原文
- expression_self_reflect：写入前让 AI 按四条基准检查（suitable/reason）
- learning_list [{platform, item_id, type, use, learn}] 控制按聊天使用/学习；
  *_groups [{targets:[{platform, item_id}]}] 让组内聊天共享学习库

存储：data_learning.json，按共享组键分库（默认 global）；文件存 AstrBot
持久化目录 data/plugin_data/（卸载不删数据时幸存，坑 50），由 main.py
显式传路径，_DATA_FILE 仅作离线默认。
"""

import json
import random
import re
from pathlib import Path

_DATA_FILE = Path(__file__).resolve().parent.parent / "data_learning.json"

MAX_JARGON_REFERENCE_MATCHES = 10  # MAX_JARGON_REFERENCE_MATCHES 原值
MAX_SELECTED_EXPRESSIONS = 5       # 表达直注入条数上限（抽样池同 MaiBot 5+5）
EXPRESSION_MIN_POOL = 10           # legacy：库不足 10 条不启用
ASSISTANT_OPTIMIZATION_KEEP_COUNT = 3  # 优化上下文：自己发言保留条数

_JARGON_HEADER = "以下黑话来自当前上下文中其他用户消息的机械匹配，仅作理解聊天语境的参考："

# ---------------- MaiBot prompts/zh-CN 原文 ---------------- #
LEARN_STYLE_PROMPT = """{chat_str}
请从上面这段群聊中提取用户的语言风格和说话方式。
1. 只考虑文字，不要考虑表情包和图片
2. 不要总结 SELF 的发言，因为这是你自己的发言，不要重复学习你自己的发言
3. 不要涉及具体的人名，也不要涉及具体名词
4. 思考有没有特殊的梗，一并总结成语言风格
5. 例子仅供参考，请严格根据群聊内容总结

请总结成如下格式的规律：当 "AAAAA" 时，可以 "BBBBB"。
- AAAAA 表示某个场景，不超过 20 个字
- BBBBB 表示对应的语言风格、特定句式或表达方式，不超过 20 个字
- 表达方式在 3-5 个左右，不要超过 10 个

输出要求：
请仅输出 JSON 数组，不要输出重复内容。每个元素为一个对象，字段名如下：

[
  {{"situation": "对某件事表示十分惊叹", "style": "使用 我嘞个xxxx", "source_id": "3"}},
  {{"situation": "表示讽刺的赞同", "style": "使用 对对对", "source_id": "4"}}
]

字段说明：
- situation：表示"在什么情境下"的简短概括（不超过 20 个字）
- style：表示对应的语言风格或常用表达（不超过 20 个字）
- source_id：该表达方式对应的来源编号，即上方聊天记录中的 source_id 数字，请只输出数字本身

输出 JSON："""

LEARN_JARGON_PROMPT = """{chat_str}
请从上面这段聊天内容中提取"可能是黑话"的候选项（黑话/俚语/网络缩写/口头禅）。

提取规则：
- 必须为对话中真实出现过的短词或短语
- 必须是你无法理解含义、或者需要当前聊天圈内语境才能理解的词语
- 不要选择含义清晰的普通词语
- 排除：人名、@、表情包/图片中的内容、纯标点、常规功能词（如的、了、呢、啊等）
- 每个词条长度建议 2-8 个字符（不强制），尽量短小
- 请尽量提取所有可能的黑话，最多 30 个
- 可以从不同来源中提取，但不要从表情包/图片内容中提取

黑话必须为以下几种类型：
- 由字母构成的，汉语拼音首字母的简写词，例如：nb、yyds、xswl
- 英文词语的缩写，用英文字母概括一个词汇或含义，例如：CPU、GPU、API
- 中文词语的缩写，用几个汉字概括一个词汇或含义，例如：社死、内卷
- 群聊内部反复使用、但脱离上下文不容易理解的短词或短语

输出要求：
请仅输出 JSON 数组，不要输出重复内容。每个元素为一个对象，字段名如下：

[
  {{"content": "词条", "source_id": "12"}},
  {{"content": "词条2", "source_id": "5"}}
]

字段说明：
- content：黑话候选词条的原文
- source_id：该黑话对应的来源编号，即上方聊天记录中的 source_id 数字，请只输出数字本身

输出 JSON："""

JARGON_INFERENCE_PROMPT = """**词条内容**
{content}
**词条出现的上下文。其中的{bot_name}的发言内容是你自己的发言**
{raw_content_list}

请根据上下文，推断"{content}"这个词条的含义。
- 如果这是一个黑话、俚语或网络用语，请推断其含义
- 如果含义明确（常规词汇），也请说明
- {bot_name} 的发言内容可能包含错误，请不要参考其发言内容
- 如果上下文信息不足，无法推断含义，请设置 no_info 为 true

以 JSON 格式输出：
{{
  "meaning": "详细含义说明（包含使用场景、来源、具体解释等）",
  "no_info": false
}}
注意：如果信息不足无法推断，请设置 "no_info": true，此时 meaning 可以为空字符串"""

_EXPRESSION_CRITERIA = [
    "表达方式或言语风格是否与使用条件或使用情景匹配",
    "允许部分语法错误或口语化或缺省出现",
    "表达方式不能太过特指，需要具有泛用性",
    "一般不涉及具体的人名或名称",
]

EXPRESSION_EVALUATION_PROMPT = """请评估以下表达方式或语言风格以及使用条件或使用情景是否合适：
使用条件或使用情景：{situation}
表达方式或言语风格：{style}

请从以下方面进行评估：
{criteria_list}

请以JSON格式输出评估结果：
{{
    "suitable": true/false,
    "reason": "评估理由（如果不合适，请说明原因）"

}}
请严格按照JSON格式输出，不要包含其他内容。"""

# ---------------- expression_select.prompt 原文 ----------------
EXPRESSION_SELECT_PROMPT = """{chat_observe_info}

你的名字是{bot_name}{target_message}
{reply_reason_block}

以下是可选的表达情境：
{all_situations}

请你分析聊天内容的语境、情绪、话题类型，从上述情境中选择最适合当前聊天情境的内容，最多{max_num}个情境。
考虑因素包括：
1.聊天的情绪氛围
2.话题类型
3.情境与当前语境的匹配度
{target_message_extra_block}

请以JSON格式输出，只需要输出选中的情境编号：
例如：
{{
    "selected_situations": [2, 3, 5, 7, 19]
}}

请严格按照JSON格式输出，不要包含其他内容："""

MAX_SELECTED_EXPRESSIONS_LLM = 5  # expression_select 的 max_num（MaiBot 调用值）
_EXPRESSION_SELECTION_SEMAPHORE = {"max_count": 0, "semaphore": None}


def get_selection_semaphore(cfg):
    """表达学习最大并发（max_expression_learner）信号量。"""
    import asyncio
    max_count = max(1, int(cfg.get("max_expression_learner", 3)))
    cached = _EXPRESSION_SELECTION_SEMAPHORE
    if cached["semaphore"] is None or cached["max_count"] != max_count:
        cached["max_count"] = max_count
        cached["semaphore"] = asyncio.Semaphore(max_count)
    return cached["semaphore"]


def _sample_legacy_pool(pool: list[dict]) -> list[dict]:
    """legacy 候选池：高频(>1)抽 5 + 全库抽 5，去重（对齐 _sample_legacy_expression_candidates）。"""
    high = [e for e in pool if int(e.get("count", 1) or 1) > 1]
    high_picks = _weighted_sample(high, min(len(high), 5)) if len(high) >= 10 else []
    candidates, seen = [], set()
    for item in [*high_picks, *_weighted_sample(pool, min(len(pool), 5))]:
        token = (item.get("situation"), item.get("style"))
        if token in seen:
            continue
        seen.add(token)
        candidates.append(item)
    return candidates


async def select_expression_habits_block(provider, store: "LearningStore", key: str,
                                         checked_only: bool, chat_observe_info: str,
                                         bot_name: str, reply_reason: str = "",
                                         model: str | None = None) -> str:
    """legacy 完整路径：加权抽候选池 → LLM 按语境选择 → 注入块。

    LLM 选择失败或无可选情境时回落"直接注入"（MaiBot 的另一条真实路径）。
    """
    pool = [e for e in store.expressions(key) if not checked_only or e.get("checked")]
    if len(pool) < EXPRESSION_MIN_POOL:
        return ""
    candidates = _sample_legacy_pool(pool)
    if not candidates:
        return ""

    selected = None
    if provider is not None:
        try:
            situations = "\n".join(
                f"{i}. {e['situation']}" for i, e in enumerate(candidates, start=1))
            prompt_text = EXPRESSION_SELECT_PROMPT.format(
                chat_observe_info=chat_observe_info,
                bot_name=bot_name,
                target_message="",
                reply_reason_block=f"\n{reply_reason}\n" if reply_reason else "",
                all_situations=situations,
                max_num=MAX_SELECTED_EXPRESSIONS_LLM,
                target_message_extra_block="",
            )
            resp = await provider.text_chat(prompt=prompt_text, session_id="maisoul_expr_select",
                                            model=model)
            raw = str(getattr(resp, "completion_text", "") or "")
            seg = raw[raw.find("{"):raw.rfind("}") + 1]
            parsed = json.loads(seg)
            ids = [int(x) for x in (parsed.get("selected_situations") or []) if str(x).isdigit()]
            selected = [candidates[i - 1] for i in ids if 1 <= i <= len(candidates)]
        except Exception:
            selected = None

    if not selected:
        selected = candidates[:MAX_SELECTED_EXPRESSIONS]
    lines = [f"- 当\"{e['situation']}\"时，可以用\"{e['style']}\"来表达。" for e in selected]
    return "【表达习惯参考，请视情况自然的使用】\n" + "\n".join(lines)


# ---------------------------------------------------------------------- #
# 存储层
# ---------------------------------------------------------------------- #
class LearningStore:
    """JSON 持久化：{共享组键: {expressions: [...], jargons: [...]}}"""

    def __init__(self, path: Path = _DATA_FILE):
        self.path = path
        try:
            self.data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            self.data = {}

    def _bucket(self, key: str) -> dict:
        return self.data.setdefault(key, {"expressions": [], "jargons": []})

    def save(self):
        try:
            self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
        except OSError:
            pass

    # ---------------- 表达 ---------------- #
    def expressions(self, key: str) -> list[dict]:
        return self._bucket(key).get("expressions") or []

    def add_expression(self, key: str, situation: str, style: str, checked: bool) -> bool:
        situation, style = situation.strip(), style.strip()
        if not situation or not style:
            return False
        for item in self.expressions(key):
            if item.get("situation") == situation and item.get("style") == style:
                item["count"] = int(item.get("count", 1)) + 1
                item["checked"] = bool(item.get("checked")) or checked
                self.save()
                return False
        self._bucket(key)["expressions"].append(
            {"situation": situation, "style": style, "count": 1, "checked": checked})
        self.save()
        return True

    # ---------------- 黑话 ---------------- #
    def jargons(self, key: str) -> list[dict]:
        return self._bucket(key).get("jargons") or []

    def add_jargon(self, key: str, content: str, meaning: str) -> bool:
        content, meaning = content.strip(), (meaning or "").strip()
        if not content or not meaning:
            return False
        for item in self.jargons(key):
            if item.get("content") == content:
                item["count"] = int(item.get("count", 1)) + 1
                if meaning and meaning != item.get("meaning"):
                    item["meaning"] = meaning
                self.save()
                return False
        self._bucket(key)["jargons"].append({"content": content, "meaning": meaning, "count": 1})
        self.save()
        return True


# ---------------------------------------------------------------------- #
# 配置匹配（LearningItem / 共享组）
# ---------------------------------------------------------------------- #
def learning_flags(cfg, list_field: str, platform: str, chat_id: str,
                    is_group: bool = True) -> tuple[bool, bool]:
    """返回 (use, learn)。精确命中规则优先，回落全局默认规则，再回落 (True, True)。"""
    matched = None
    default = None
    want_type = "group" if is_group else "private"
    for item in (cfg.get(list_field) or []):
        if not isinstance(item, dict) or str(item.get("type") or "group") != want_type:
            continue
        p = str(item.get("platform") or "").strip()
        i = str(item.get("item_id") or "").strip()
        if not p and not i:
            default = item
            continue
        if p == platform and i == chat_id:
            matched = item
            break
    rule = matched or default or {}
    return bool(rule.get("use", True)), bool(rule.get("learn", True))


def share_key(cfg, groups_field: str, platform: str, chat_id: str) -> str:
    """命中共享组的聊天返回组键，否则 global。"""
    for gi, group in enumerate(cfg.get(groups_field) or []):
        for target in ((group or {}).get("targets") or []):
            if not isinstance(target, dict):
                continue
            p = str(target.get("platform") or "").strip()
            i = str(target.get("item_id") or "").strip()
            if p == platform and i == chat_id:
                return f"group_{gi}"
    return "global"


# ---------------------------------------------------------------------- #
# 注入块构建（格式 = MaiBot 原文）
# ---------------------------------------------------------------------- #
def _weighted_sample(candidates: list[dict], n: int) -> list[dict]:
    if not candidates or n <= 0:
        return []
    weights = [max(1, int(c.get("count", 1) or 1)) for c in candidates]
    if sum(weights) <= 0:
        return random.sample(candidates, min(n, len(candidates)))
    picked, pool = [], list(candidates)
    while pool and len(picked) < n:
        chosen = random.choices(pool, weights=weights[: len(pool)], k=1)[0]
        picked.append(chosen)
        idx = pool.index(chosen)
        pool.pop(idx)
        weights.pop(idx)
    return picked


def expression_habits_block(store: LearningStore, key: str, checked_only: bool) -> str:
    """legacy 直接注入：库 ≥10 条才启用；高频(>1)抽 5 + 全库抽 5 去重，上限 5 条。"""
    pool = [e for e in store.expressions(key) if not checked_only or e.get("checked")]
    if len(pool) < EXPRESSION_MIN_POOL:
        return ""
    high = [e for e in pool if int(e.get("count", 1) or 1) > 1]
    candidates, seen = [], set()
    high_picks = _weighted_sample(high, min(len(high), 5)) if len(high) >= 10 else []
    for item in [*high_picks, *_weighted_sample(pool, min(len(pool), 5))]:
        token = (item.get("situation"), item.get("style"))
        if token in seen:
            continue
        seen.add(token)
        candidates.append(item)
    candidates = candidates[:MAX_SELECTED_EXPRESSIONS]
    if not candidates:
        return ""
    lines = [f"- 当\"{e['situation']}\"时，可以用\"{e['style']}\"来表达。" for e in candidates]
    return "【表达习惯参考，请视情况自然的使用】\n" + "\n".join(lines)


def jargon_reference_block(store: LearningStore, key: str, recent_texts: list[str],
                           exclude: set | None = None,
                           matched_out: list | None = None) -> str:
    """最近上下文文本机械命中词条 → 参考块（上限 10 条）。

    exclude：已注入过的词条（planner 轮间去重，对齐 jargon_context_matcher
    对历史黑话参考消息的去重）；matched_out：回填本次命中的词条原文。
    排序：词条 count 降序 + 首现位置提前优先——MaiBot 的高频词表加权
    （高频基数 1000+出现次数×2）依赖其高频词学习器（已取舍未移植），此为近似。
    """
    scored: list[tuple[tuple[int, int], dict]] = []
    for item in store.jargons(key):
        content = str(item.get("content") or "").strip()
        if not content or (exclude and content in exclude):
            continue
        first_index = next((i for i, t in enumerate(recent_texts)
                            if t and content in t), None)
        if first_index is not None:
            count = int(item.get("count", 1) or 1)
            scored.append(((-count, first_index), item))
    if not scored:
        return ""
    scored.sort(key=lambda x: x[0])
    matched = [item for _, item in scored[:MAX_JARGON_REFERENCE_MATCHES]]
    if matched_out is not None:
        matched_out.extend(str(item.get("content") or "") for item in matched)
    lines = [_JARGON_HEADER]
    for index, item in enumerate(matched, start=1):
        lines.append(f"{index}. {item['content']}：{item['meaning']}")
    return "\n".join(lines)


def keyword_reaction_block(cfg, match_text: str) -> str:
    """最新用户消息命中关键词/正则规则 → 反应块（格式 = MaiBot 原文）。"""
    match_text = str(match_text or "").strip()
    if not match_text:
        return ""
    matched_reactions: list[str] = []
    for rule in (cfg.get("keyword_rules") or []):
        if not isinstance(rule, dict):
            continue
        keywords = [str(k).strip() for k in (rule.get("keywords") or []) if str(k).strip()]
        if keywords and any(k in match_text for k in keywords):
            reaction = str(rule.get("reaction") or "").strip()
            if reaction:
                matched_reactions.append(reaction)
    for rule in (cfg.get("regex_rules") or []):
        if not isinstance(rule, dict):
            continue
        reaction = str(rule.get("reaction") or "").strip()
        if not reaction:
            continue
        for pattern in (rule.get("regex") or []):
            pattern = str(pattern).strip()
            if not pattern:
                continue
            try:
                m = re.search(pattern, match_text)
            except re.error:
                continue
            if m is None:
                continue
            replaced = reaction
            for group_name, group_value in m.groupdict().items():
                replaced = replaced.replace(f"[{group_name}]", group_value or "")
            matched_reactions.append(replaced)
            break
    if not matched_reactions:
        return ""
    lines = "\n".join(f"- {r}" for r in matched_reactions)
    return f"【关键词反应】\n最新消息命中了预设反应规则，请在回复时优先参考以下要求：\n{lines}\n"


# ---------------------------------------------------------------------- #
# 异步学习器
# ---------------------------------------------------------------------- #
def _repair_json_array(raw: str) -> list:
    raw = str(raw or "").strip()
    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        parsed = json.loads(raw[start:end + 1])
        return parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        return []


def _build_chat_str(buffer: list[dict], bot_name: str, limit: int = 30) -> str:
    """带 source_id 编号的聊天记录；机器人自己的发言标注为 SELF。"""
    lines = []
    for i, m in enumerate(list(buffer)[-limit:]):
        name = str(m.get("name") or "?")
        tag = " [SELF]" if name == bot_name else ""
        lines.append(f"[source_id:{i}]{tag} {name}: {m.get('text') or ''}")
    return "\n".join(lines)


async def _llm(provider, prompt: str, model: str | None = None) -> str:
    resp = await provider.text_chat(prompt=prompt, session_id="maisoul_learning",
                                    model=model)
    return str(getattr(resp, "completion_text", "") or "").strip()


def build_chat_info(buffer: list[dict], limit: int = 10) -> str:
    """expression_select 的 chat_observe_info：最近 10 条对话行（对齐 _build_chat_info）。"""
    from datetime import datetime as _dt
    lines = []
    for m in list(buffer)[-limit:]:
        text = " ".join(str(m.get("text") or "").split())
        if not text:
            continue
        time_str = _dt.fromtimestamp(float(m.get("ts") or 0)).strftime("%H:%M:%S")
        lines.append(f"- {time_str} {m.get('name')}: {text[:120]}")
    return "\n".join(lines)


async def learn_from_chat(provider, cfg, buffer: list[dict], platform: str, chat_id: str,
                          store: LearningStore, model: str | None = None) -> str:
    """发言后异步学习：表达 + 黑话。返回日志摘要。

    整体受 max_expression_learner 信号量约束（对齐 MaiBot 学习并发上限）。
    model：任务级模型绑定（learner 任务）时的按次覆盖。
    """
    async with get_selection_semaphore(cfg):
        return await _learn_from_chat_inner(provider, cfg, buffer, platform, chat_id,
                                            store, model=model)


async def _learn_from_chat_inner(provider, cfg, buffer: list[dict], platform: str, chat_id: str,
                                 store: LearningStore, model: str | None = None) -> str:
    bot_name = str(cfg.get("bot_name") or "麦麦")
    key = share_key(cfg, "expression_groups", platform, chat_id)
    jkey = share_key(cfg, "jargon_groups", platform, chat_id)
    _, learn_expr = learning_flags(cfg, "expression_learning_list", platform, chat_id)
    _, learn_jargon = learning_flags(cfg, "jargon_learning_list", platform, chat_id)
    if not learn_expr and not learn_jargon:
        return "学习未启用"
    chat_str = _build_chat_str(buffer, bot_name)
    if not chat_str.strip():
        return "无内容"

    summary = []
    try:
        if learn_expr:
            raw = await _llm(provider, LEARN_STYLE_PROMPT.format(chat_str=chat_str), model=model)
            items = [x for x in _repair_json_array(raw) if isinstance(x, dict)]
            added = 0
            for item in items[:10]:
                situation = str(item.get("situation") or "")
                style = str(item.get("style") or "")
                checked = True
                if cfg.get("expression_self_reflect", True) and situation and style:
                    criteria = "\n".join(f"{i + 1}. {c}" for i, c in enumerate(_EXPRESSION_CRITERIA))
                    review_raw = await _llm(provider, EXPRESSION_EVALUATION_PROMPT.format(
                        situation=situation, style=style, criteria_list=criteria), model=model)
                    checked = '"suitable": true' in review_raw.replace(" ", "") or \
                              '"suitable":true' in review_raw
                if store.add_expression(key, situation, style, checked):
                    added += 1
            summary.append(f"表达 +{added}")
    except Exception:
        summary.append("表达学习失败")

    try:
        if learn_jargon:
            raw = await _llm(provider, LEARN_JARGON_PROMPT.format(chat_str=chat_str), model=model)
            items = [x for x in _repair_json_array(raw) if isinstance(x, dict)]
            known = {j.get("content") for j in store.jargons(jkey)}
            added = 0
            for item in items[:10]:
                content = str(item.get("content") or "").strip()
                if not content or content in known:
                    continue
                infer_raw = await _llm(provider, JARGON_INFERENCE_PROMPT.format(
                    content=content, bot_name=bot_name, raw_content_list=chat_str), model=model)
                meaning = ""
                try:
                    seg = infer_raw[infer_raw.find("{"):infer_raw.rfind("}") + 1]
                    parsed = json.loads(seg)
                    meaning = str(parsed.get("meaning") or "")
                    if parsed.get("no_info"):
                        meaning = ""
                except Exception:
                    meaning = ""
                if store.add_jargon(jkey, content, meaning):
                    added += 1
            summary.append(f"黑话 +{added}")
    except Exception:
        summary.append("黑话学习失败")

    return "、".join(summary) or "无新增"
