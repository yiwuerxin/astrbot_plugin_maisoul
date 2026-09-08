"""提及判定与 at 档构成 —— 单一真相（v6.18.3）

原则：At 段的 qq 精确匹配是"被艾特"的唯一权威信号（pipeline 层 _has_at_bot），
@ 其他人即使昵称包含 bot_name（如「小小麦」含「小麦」）也不算艾特不算提及；
本模块负责文本提及：适配器会把 @其他用户 渲染为 " @昵称(QQ号) " 拼进
message_str（aiocqhttp），先剥渲染 token 与 exclude_names（At 段他人昵称），
再对 bot_name/aliases 做前边界匹配。

边界取舍（中文无分词）：只做前边界——「小小麦」「麦小麦」不命中（前缀扩展，
冒名的主要形态），「小麦你觉得呢」「小麦在吗」命中（右侧粘连动词/代词是
正常叫名形态）；后缀扩展名（「小麦子」）的纯文本提及仍会命中，属已知残留，
@ 场景由 exclude_names（At 段昵称级排除）精确兜底。
"""

import re

# 词字符：中文/字母/数字/下划线/间隔号——关键字前紧贴这些字符即不算提及
_WORD = r"[\w\u4e00-\u9fff·]"
# aiocqhttp 适配器渲染的 @其他用户 token："@昵称(QQ号)"（昵称不含空格括号时）
_AT_RENDERED_RE = re.compile(r"@[^\s()]{0,64}\(\d+\)")


def is_mentioned(text: str, bot_name: str, aliases, exclude_names=()) -> bool:
    """文本提及判定：剥渲染 @token 与他人昵称后，前边界匹配 bot_name/aliases。"""
    t = _AT_RENDERED_RE.sub(" ", str(text or ""))
    for name in exclude_names or ():
        name = str(name or "").strip()
        if name:
            t = t.replace(name, " ")
    for kw in [str(bot_name or ""), *(str(a) for a in (aliases or []))]:
        kw = kw.strip().lstrip("@").strip()
        if not kw:
            continue
        if re.search(rf"(?<!{_WORD}){re.escape(kw)}", t):
            return True
    return False


def effective_at_bot(
    has_at: bool, explicit: bool, has_at_all: bool, reply_to_bot: bool
) -> bool:
    """at 档构成：At 段命中恒入档；框架唤醒标志（explicit）仅唤醒前缀场景入档。

    AstrBot waking_check 对 @bot、@全体成员（AtAll）、引用回复 bot、唤醒前缀
    四种形态统一置 is_at_or_wake_command=True，整体并入 at 档会把 @全体/引用
    升级成强制必回、绕过 mentioned_bot_reply 的默认关——故 AtAll 与 Reply 命中
    时对 explicit 降级，回落到提及/普通档（AtAll+前缀并存的罕见 case 按降级处理）。
    """
    return bool(has_at or (explicit and not has_at_all and not reply_to_bot))
