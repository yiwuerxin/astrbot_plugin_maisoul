"""回复后处理 —— 源码级移植 MaiBot process_llm_response_segments 管线

流程（与 MaiBot chat/utils/utils.py 一致）：
1. 总开关 response_post_process.enable（maisoul 键 enable_response_post_process）
2. 颜文字保护（可选，默认关）
3. 去除 ()/[]/（）包裹的中文心声；全空 → "呃呃"
4. 基本全中文且超过 max_length*2 → 随机默认回复（"XX不知道哦" 等）
5. 分句（引号/冒号/空格规则 + 按长度概率合并 0.2/0.6/0.7）
6. 每句生成错字（chinese_typo 配置）；有纠正时 50% 追加纠正消息
   （引用上一条概率 = enable_correction_quote × correction_quote_probability），
   50% 直接用正确分句
7. 段数 > max_sentence_num → 超限保留全文（默认关）或随机默认回复
8. 合并到 max_split_num 条（纠正段边界优先保留）
9. 恢复颜文字

打字时间 calculate_typing_time：中文 0.3s/字、英文 0.15s/字、单汉字 3 倍+0.3、
emoji 固定 1s，整体 ×typing_speed（≤0 → 0）。
"""

import random
import re
from dataclasses import dataclass

from . import typo as typo_mod

_PARA_PATTERN = re.compile(r"[(\[（](?=.*[一-鿿]).*?[)\]）]")
_KAOMOJI_PATTERN = re.compile(
    r"("
    r"[(\[（【]"
    r"[^()\[\]（）【】]*?"
    r"[^一-龥a-zA-Z0-9\s]"
    r"[^()\[\]（）【】]*?"
    r"[)\]）】"
    r"]"
    r")"
    r"|"
    r"([▼▽・ᴥω･﹏^><≧≦￣｀´∀ヮДд︿﹀へ｡ﾟ╥╯╰︶︹•⁄]{2,15})"
)


@dataclass(frozen=True)
class ProcessedResponseSegment:
    """回复后处理产生的单条消息及其发送提示。"""

    text: str
    quote_previous: bool = False


def _is_english_letter(char: str) -> bool:
    return "a" <= char.lower() <= "z"


def get_western_ratio(paragraph: str) -> float:
    alnum = [c for c in paragraph if c.isalnum()]
    if not alnum:
        return 0.0
    return sum(bool(_is_english_letter(c)) for c in alnum) / len(alnum)


def protect_kaomoji(sentence: str):
    matches = _KAOMOJI_PATTERN.findall(sentence)
    mapping = {}
    for match in matches:
        kaomoji = match[0] or match[1]
        if kaomoji.startswith("[表情包") and kaomoji.endswith("]"):
            continue
        placeholder = f"__KAOMOJI_{len(mapping)}__"
        sentence = sentence.replace(kaomoji, placeholder, 1)
        mapping[placeholder] = kaomoji
    return sentence, mapping


def recover_kaomoji(sentences: list[str], mapping: dict) -> list[str]:
    recovered = []
    for sentence in sentences:
        for placeholder, kaomoji in mapping.items():
            sentence = sentence.replace(placeholder, kaomoji)
        recovered.append(sentence)
    return recovered


def _get_random_default_reply(bot_name: str) -> str:
    default_replies = [
        f"{bot_name}不知道哦",
        f"{bot_name}不知道",
        "不知道哦",
        "不知道",
        "不晓得",
        "懒得说",
        "()",
    ]
    return random.choice(default_replies)


# ---------------------------------------------------------------------- #
# 分句（复刻 split_into_sentences_w_remove_punctuation）
# ---------------------------------------------------------------------- #
def split_into_sentences_w_remove_punctuation(text: str) -> list[str]:
    text = re.sub(r"\n\s*\n+", "\n", text)
    text = re.sub(r"\n\s*([，,。;\s])", r"\n\1", text)
    text = re.sub(r"([，,。;\s])\s*\n", r"\1\n", text)

    len_text = len(text)
    if len_text < 3:
        return list(text) if random.random() < 0.01 else [text]

    quote_chars = {'"', "'", "“", "”", "‘", "’", "「", "」", "『", "』"}
    inside_quote = [False] * len_text
    in_quote = False
    current_quote_char = ""
    for idx, ch in enumerate(text):
        if ch in quote_chars:
            if not in_quote:
                in_quote = True
                current_quote_char = ch
            else:
                if ch == current_quote_char or ch in {'"', "'"} and current_quote_char in {'"', "'"}:
                    in_quote = False
                    current_quote_char = ""
        else:
            inside_quote[idx] = in_quote

    separators = {"，", ",", " ", "。", ";", "\n"}
    segments = []
    current_segment = ""
    i = 0
    while i < len(text):
        char = text[i]
        if char in separators:
            if inside_quote[i]:
                can_split = False
            elif char == "\n":
                can_split = True
            else:
                can_split = True
                if i > 0 and text[i - 1] in {":", "："}:
                    can_split = False
                if i < len(text) - 1 and text[i + 1] in {":", "："}:
                    can_split = False
                if can_split and char == " " and 0 < i < len(text) - 1:
                    prev_char, next_char = text[i - 1], text[i + 1]
                    if prev_char in {"-", "—"} or next_char in {"-", "—"}:
                        can_split = False
                    else:
                        prev_alnum = prev_char.isdigit() or _is_english_letter(prev_char)
                        next_alnum = next_char.isdigit() or _is_english_letter(next_char)
                        if prev_alnum and next_alnum:
                            can_split = False
            if can_split:
                if current_segment:
                    segments.append((current_segment, char))
                elif char in {" ", "\n"}:
                    segments.append(("", char))
                current_segment = ""
            else:
                current_segment += char
        else:
            current_segment += char
        i += 1
    if current_segment:
        segments.append((current_segment, ""))

    segments = [(c, s) for c, s in segments if c or s]
    if not segments:
        return [text] if text else []

    if len_text < 12:
        split_strength = 0.2
    elif len_text < 32:
        split_strength = 0.6
    else:
        split_strength = 0.7
    merge_probability = 1.0 - split_strength

    merged = []
    idx = 0
    while idx < len(segments):
        content, sep = segments[idx]
        if (idx + 1 < len(segments) and content and sep != "\n"
                and random.random() < merge_probability):
            next_content, next_sep = segments[idx + 1]
            if next_content:
                merged.append((content + sep + next_content, next_sep))
            else:
                merged.append((content, next_sep))
            idx += 2
        else:
            merged.append((content, sep))
            idx += 1

    final = [c for c, _ in merged if c]
    final = [s for s in final if s.strip()]
    final = [n for s in final if (n := re.sub(r"[^\S\r\n]*[\r\n]+[^\S\r\n]*", " ", s).strip())]
    return final


def _merge_segments_to_max_count(segments, max_count):
    """压缩段数，纠正消息（quote_previous）的边界优先保留。"""
    if len(segments) <= max_count:
        return segments
    if max_count <= 0:
        return []
    count = len(segments)
    required_starts = [i for i, s in enumerate(segments) if i > 0 and s.quote_previous]
    group_starts = {0, *required_starts[: max_count - 1]}
    evenly_spaced = []
    start = 0
    for group_index in range(max_count):
        remaining = count - start
        remaining_groups = max_count - group_index
        size = (remaining + remaining_groups - 1) // remaining_groups
        evenly_spaced.append(start)
        start += size
    for candidate in evenly_spaced:
        if len(group_starts) >= max_count:
            break
        group_starts.add(candidate)
    sorted_starts = sorted(group_starts)
    merged = []
    for gi, gs in enumerate(sorted_starts):
        ge = sorted_starts[gi + 1] if gi + 1 < len(sorted_starts) else count
        group = segments[gs:ge]
        merged.append(ProcessedResponseSegment(
            text="".join(s.text for s in group),
            quote_previous=group[0].quote_previous))
    return merged


# ---------------------------------------------------------------------- #
# 主入口
# ---------------------------------------------------------------------- #
def process_response_segments(text: str, cfg) -> list[ProcessedResponseSegment]:
    """LLM 原始回复 → 待发送段列表。cfg 为 maisoul 配置视图（含主配置键）。"""
    bot_name = str(cfg.get("bot_name") or "麦麦").strip() or "麦麦"
    if not str(text or "").strip():
        return [ProcessedResponseSegment("呃呃")]

    if not cfg.get("enable_response_post_process", True):
        return [ProcessedResponseSegment(text)]

    if cfg.get("splitter_enable_kaomoji_protection", False):
        protected_text, kaomoji_mapping = protect_kaomoji(text)
    else:
        protected_text, kaomoji_mapping = text, {}

    cleaned_text = _PARA_PATTERN.sub("", protected_text)
    if cleaned_text == "":
        return [ProcessedResponseSegment("呃呃")]

    max_length = int(cfg.get("splitter_max_length", 512)) * 2
    max_sentence_num = int(cfg.get("splitter_max_sentence_num", 8))
    max_split_num = int(cfg.get("splitter_max_split_num", 3))
    if get_western_ratio(cleaned_text) < 0.1 and len(cleaned_text) > max_length:
        return [ProcessedResponseSegment(_get_random_default_reply(bot_name))]

    if cfg.get("splitter_enable", True):
        split_sentences = split_into_sentences_w_remove_punctuation(cleaned_text)
    else:
        split_sentences = [cleaned_text]

    segments: list[ProcessedResponseSegment] = []
    typo_on = cfg.get("typo_enable", True)
    generator = typo_mod.get_typo_generator(cfg) if typo_on else None
    for sentence in split_sentences:
        if generator is not None:
            typoed, corrections = generator.create_typo_sentence(sentence)
            if corrections:
                if random.random() < 0.5:
                    quote_previous = (
                        cfg.get("typo_enable_correction_quote", True)
                        and random.random() < float(cfg.get("typo_correction_quote_probability", 1.0))
                    )
                    segments.append(ProcessedResponseSegment(typoed))
                    segments.append(ProcessedResponseSegment(corrections, quote_previous=quote_previous))
                else:
                    segments.append(ProcessedResponseSegment(sentence))
            else:
                segments.append(ProcessedResponseSegment(typoed))
        else:
            segments.append(ProcessedResponseSegment(sentence))

    if len(segments) > max_sentence_num:
        if cfg.get("splitter_enable_overflow_return_all", False):
            segments = [ProcessedResponseSegment(cleaned_text)]
        else:
            return [ProcessedResponseSegment(_get_random_default_reply(bot_name))]

    segments = _merge_segments_to_max_count(segments, max_split_num)

    if cfg.get("splitter_enable_kaomoji_protection", False):
        recovered = recover_kaomoji([s.text for s in segments], kaomoji_mapping)
        segments = [ProcessedResponseSegment(text=t, quote_previous=s.quote_previous)
                    for s, t in zip(segments, recovered)]
    return segments


def calculate_typing_time(input_string: str, typing_speed: float = 1.0,
                          chinese_time: float = 0.3, english_time: float = 0.15,
                          is_emoji: bool = False) -> float:
    """复刻 MaiBot calculate_typing_time：×typing_speed，speed≤0 → 0。"""
    chinese_chars = sum("\u4e00" <= c <= "\u9fff" for c in input_string)
    if chinese_chars == 1 and len(input_string.strip()) == 1:
        return chinese_time * 3 + 0.3

    total = 0.0
    for char in input_string:
        total += chinese_time if "\u4e00" <= char <= "\u9fff" else english_time
    if is_emoji:
        total = 1.0
    if typing_speed <= 0:
        return 0
    return total * typing_speed
