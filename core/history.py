"""会话历史获取工具（fetch_chat_history）的纯逻辑。

planner 的稳定窗只带最近 2×base 条（坑 31），窗口外的记录模型看不到；
本工具读 maisoul 自己的会话缓冲（GroupState.buffer，deque maxlen=200），
把窗口外的原文按 planner 请求格式喂回去。MaiBot 的 fetch_history 是
focus 模式专属（部署版未开，坑 30），本工具为 maisoul 自有扩展——数据源
是 buffer，不碰 OneBot、不建存储；buffer 随进程重启清空，上限 200 条。
"""

from __future__ import annotations

from datetime import datetime as _dt

from .planner import build_history_contexts, render_planner_message, split_pending

MAX_HISTORY_LIMIT = 50
DEFAULT_HISTORY_LIMIT = 20


def planner_seen_ids(
    records: list[dict],
    analyses,
    context_limit: int,
    last_cycle_ts: float,
    is_group: bool = True,
) -> set[int]:
    """planner 本轮可见的聊天消息身份集 = 稳定窗 included + 待排水 pending。

    与 planner 请求同一套选取逻辑（split_pending + build_history_contexts
    复用）——v6.20.1 前工具按 buffer 条数硬排 2×base，而 planner 稳定窗在
    「聊天+分析」合并流上截取、分析占坑，两窗不一致的中间段任何途径都
    取不到（盲区）；现按真实可见集排除。"""
    pending, history_buf = split_pending(records, last_cycle_ts)
    _, included = build_history_contexts(history_buf, analyses, context_limit, is_group)
    return {id(m) for m in pending} | {id(m) for m in included}


def fetch_history_slice(
    records: list[dict],
    seen_ids: set[int],
    keyword: str = "",
    limit: int = DEFAULT_HISTORY_LIMIT,
) -> list[dict]:
    """已见集之外记录的选取：排除 planner 本轮可见消息（seen_ids 取
    planner_seen_ids，防重复灌），keyword 命中过滤（text 包含、不区分
    大小写），从新到旧取 limit 条（下限 1、封顶 50），返回按时间正序
    （阅读顺序）。"""
    all_records = records or []
    older = [m for m in all_records if id(m) not in (seen_ids or set())]
    kw = str(keyword or "").strip().lower()
    if kw:
        older = [m for m in older if kw in str(m.get("text") or "").lower()]
    # limit 钳制对齐 MaiBot tool_search 风格：缺省=默认 20，0/负数压到 1，
    # 封顶 50（不能用 or 兜底——0 会被误吞成默认值）
    raw = limit if limit is not None else DEFAULT_HISTORY_LIMIT
    cap = min(max(1, int(raw)), MAX_HISTORY_LIMIT)
    return older[-cap:]


def render_history_result(
    picked: list[dict], total_outside: int, is_group: bool = True
) -> str:
    """选取记录 → 工具回执文本：说明头 + <message> 前缀行（与 planner
    请求同格式，自发消息带 is_self_message），跨日插「时间：YYYY-MM-DD」
    分隔行（对齐坑 31 的跨日插行语义——历史记录常跨天，只有时分会误读）。"""
    if not picked:
        return (
            f"没有可返回的更早聊天记录（窗口外共 {max(0, int(total_outside))} 条"
            "，可能关键词未命中或会话缓冲已到头）。"
        )
    lines = [
        f"以下是比当前上下文窗口更早的聊天记录（{len(picked)} 条，"
        f"窗口外共 {int(total_outside)} 条，由旧到新）："
    ]
    last_day = ""
    for m in picked:
        ts = float(m.get("ts") or 0)
        day = _dt.fromtimestamp(ts).strftime("%Y-%m-%d") if ts > 0 else ""
        if day and day != last_day:
            lines.append(f"时间：{day}")
            last_day = day
        lines.append(render_planner_message(m, is_group))
    return "\n".join(lines)
