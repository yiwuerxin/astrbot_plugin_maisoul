"""群会话状态 —— 对齐 MaiBot runtime 的会话缓冲与存在感统计"""

import time
from collections import deque
from dataclasses import dataclass, field

from .constants import (
    EXTERNAL_BURST_INTERVAL_SECONDS,
    EXTERNAL_MIN_AVERAGE_INTERVAL_SECONDS,
    EXTERNAL_SAMPLE_WINDOW_SECONDS,
)


@dataclass
class GroupState:
    """单个群的运行时状态（缓冲/积压/存在感/防重复）。"""

    buffer: deque = field(default_factory=lambda: deque(maxlen=200))
    recent_self: deque = field(default_factory=lambda: deque(maxlen=50))     # 自发时间戳
    last_replies: deque = field(default_factory=lambda: deque(maxlen=20))   # 近期发言文本
    replied_targets: deque = field(default_factory=lambda: deque(maxlen=30))  # (msg_id, ts)
    reply_by_target: dict = field(default_factory=dict)  # msg_id → 对该目标说过的原文（防重复提醒用）
    last_fire_ts: float = 0.0
    pending_since_fire: int = 0
    ext_intervals: deque = field(default_factory=lambda: deque(maxlen=360))  # 外部消息时间戳（30min 采样窗最多 360 条）
    last_ext_ts: float = 0.0
    firing: bool = False
    defer_task: object = None  # 空窗补偿到点重查任务（对齐 runtime._defer_message_turn_check）
    # Planner 决策层运行时（mode=planner）；惰性导入避免循环依赖
    planner: object = None
    planner_last_cycle_ts: float = 0.0

    def planner_state(self):
        from .planner import PlannerState
        if self.planner is None:
            self.planner = PlannerState()
        return self.planner
    last_persona: str = "默认"

    def record_external(self, record: dict) -> None:
        now = time.time()
        self.buffer.append(record)
        self.pending_since_fire += 1
        self.ext_intervals.append(now)
        self.last_ext_ts = now

    def mark_fire(self, msg_id: str) -> None:
        """协作模式触发时记账：时间戳/积压清零/防重复目标（发言文本由回声钩子回写）。"""
        now = time.time()
        self.last_fire_ts = now
        self.recent_self.append(now)
        self.pending_since_fire = 0
        self.replied_targets.append((msg_id, now))
        self.cancel_defer()

    def cancel_defer(self) -> None:
        """取消挂起的空窗补偿重查任务（触发成功/发言后失效）。"""
        if self.defer_task is not None:
            self.defer_task.cancel()
            self.defer_task = None

    def record_self_reply(self, msg_id: str, segments: list[str], bot_name: str) -> None:
        now = time.time()
        self.last_fire_ts = now
        self.recent_self.append(now)
        self.pending_since_fire = 0
        self.replied_targets.append((msg_id, now))
        self.cancel_defer()
        if msg_id:
            self.reply_by_target[msg_id] = "\n".join(segments)
        self.buffer.append({"name": bot_name, "sid": "self", "msg_id": "",
                            "text": segments[0][:80] if segments else "",
                            "at_bot": False, "reply_bot": False, "ts": now})
        self.last_replies.extend(segments)

    def recent_window(self, seconds: float = 300.0) -> int:
        now = time.time()
        return sum(1 for m in self.buffer if now - m["ts"] < seconds)

    def recent_self_count(self, seconds: float = 300.0) -> int:
        now = time.time()
        return sum(1 for t in self.recent_self if now - t < seconds)

    def avg_external_interval(self) -> float | None:
        """对齐 runtime 外部消息间隔统计四规则：
        30 分钟样本窗、间隔 <5s 连发不采样、平均间隔下限 30s、
        见过消息但无可用样本回退 30s（从未见过外部消息才 None）。"""
        now = time.time()
        ts = [t for t in self.ext_intervals if now - t <= EXTERNAL_SAMPLE_WINDOW_SECONDS]
        gaps = [b - a for a, b in zip(ts, ts[1:])
                if b - a >= EXTERNAL_BURST_INTERVAL_SECONDS]
        if not gaps:
            return EXTERNAL_MIN_AVERAGE_INTERVAL_SECONDS if self.last_ext_ts else None
        return max(EXTERNAL_MIN_AVERAGE_INTERVAL_SECONDS, sum(gaps) / len(gaps))

    def recently_replied(self, msg_id: str, within: float = 120.0) -> bool:
        if not msg_id:
            return False
        now = time.time()
        return any(mid == msg_id and now - ts < within for mid, ts in self.replied_targets)

    def status(self) -> dict:
        now = time.time()
        return {
            "buffer": len(self.buffer),
            "pending": self.pending_since_fire,
            "recent_self": self.recent_self_count(),
            "last_fire_ago": int(now - self.last_fire_ts) if self.last_fire_ts else None,
            "persona": self.last_persona,
        }


class StateManager:
    """所有群的会话状态注册表。"""

    def __init__(self) -> None:
        self._groups: dict[str, GroupState] = {}

    def get(self, gid: str) -> GroupState:
        if gid not in self._groups:
            self._groups[gid] = GroupState()
        return self._groups[gid]

    def status_all(self) -> dict:
        return {gid: st.status() for gid, st in self._groups.items()}

    def __len__(self) -> int:
        return len(self._groups)
