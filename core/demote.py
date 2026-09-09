"""P-D 发送队列降级判定（纯函数，core 层——CI 离线环境可测）。"""

from __future__ import annotations


def demote_quote(
    buffer: list, cfg, baseline_ts: float, *, max_msgs: int = 3, max_chars: int = 200
) -> tuple[str, str] | None:
    """生成期新消息超阈值 → 返回 (quote_id, msg_id) 引用最新一条。

    仅统计非自发消息；baseline_ts 为生成开始时刻（时间戳口径，v6.20.3：
    buffer 是 maxlen=200 的滚动 deque，按条数切片在满载滚动时索引漂移会
    漏计生成期新消息）；未超阈值/开关关/最新一条无 msg_id 返回 None。"""
    if not bool(cfg.get("send_queue_demotion", False)):
        return None
    try:
        base = float(baseline_ts)
    except (TypeError, ValueError):
        base = 0.0
    arrived = [
        m
        for m in buffer
        if str(m.get("sid") or "") != "self" and float(m.get("ts") or 0) > base
    ]
    new_chars = sum(len(str(m.get("text") or "")) for m in arrived)
    if not arrived or (len(arrived) <= max_msgs and new_chars <= max_chars):
        return None
    mid = str(arrived[-1].get("msg_id") or "").strip()
    return (mid, mid) if mid else None
