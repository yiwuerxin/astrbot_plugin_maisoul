"""麦麦观察 —— maisoul 的实时监控事件（对齐 MaiBot maisaka/monitor）。

对齐说明（禁止编造：事件名、载荷字段、保留策略均按 MaiBot 源码）：
- 事件集合 = MaiBot events.py 实际存在的 9 个 emit（前端 dump 里的
  timing_gate.result / planner.response / tool.execution / replier.response
  在 MaiBot 后端不存在，属前端遗留兼容类型，不移植）。
- 载荷字段与 emit_* 签名逐字段对齐；NON_PERSISTED_EVENTS 同为
  {"stage.status", "stage.removed", "stage.snapshot"}（只广播不落账本）。
- 保留策略同 event_store.py：10000 条 / 72 小时 / 每 200 条或 60 秒检查清理。
- 存储写法照抄：SQL 表 maisaka_monitor_events（SQLModel，表名/列/四个索引
  逐字段对齐 database_model.py）；record/replay/cleanup 语句逐行对齐
  event_store.py。MaiBot 挂自身 MySQL，maisoul 无法在 AstrBot 主库建表，
  用独立 SQLite data_monitor.db 承载同一张表（仅会话工厂差异）；文件存
  AstrBot 持久化目录 data/plugin_data/（卸载不删数据时幸存，坑 50），
  由 main.py 显式传路径，_DATA_FILE 仅作离线默认。
- 推送适配：MaiBot 经 websocket_manager.broadcast_to_topic 推 WebUI；
  插件页在沙盒 iframe 内，改经 SSE 端点 + 订阅队列（webui/routes.py）。
- sanitize 对齐：剔除 data_url 键，避免大体积内联二进制入账本
  （媒体哈希→路径解析依赖 MaiBot 图片库，maisoul 无对应，不适用）。

MaiBot 有而 maisoul 无真实信号的 emit 不接线：llm.retry（重试进度在
AstrBot Provider 内部，插件层拿不到逐次尝试），仅接 llm.error。
"""

import asyncio
import json
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import Column, DateTime, Float, Index, Integer, Text, create_engine, text
from sqlmodel import Field, Session, SQLModel, select
from sqlalchemy.orm import sessionmaker

_DATA_FILE = Path(__file__).resolve().parent.parent / "data_monitor.db"

MONITOR_EVENT_SCHEMA_VERSION = 1
MAX_MONITOR_EVENT_RECORDS = 10000
MAX_MONITOR_EVENT_AGE_HOURS = 72
DEFAULT_REPLAY_LIMIT = 1000
MAX_REPLAY_LIMIT = MAX_MONITOR_EVENT_RECORDS
CLEANUP_CHECK_INTERVAL_RECORDS = 200
CLEANUP_CHECK_INTERVAL_SECONDS = 60

NON_PERSISTED_EVENTS = {"stage.status", "stage.removed", "stage.snapshot"}

# MaiBot reasoning_engine / builtin_tool/reply 的阶段名（观察页阶段面板直接展示）
STAGE_LOOP_START = "启动循环"
STAGE_MESSAGE_INTAKE = "消息整理"
STAGE_PLANNER = "Planner"
STAGE_PLANNER_INTERRUPTED = "Planner 已打断"
STAGE_TOOL_PREFIX = "工具执行 · "
STAGE_REPLYER = "Replyer"
STAGE_WAITING = "等待消息"
STAGE_ERROR = "错误"


# 插件重载会重新 import 本模块：先摘掉全局 metadata 里的旧表定义，避免
# "Table 'maisaka_monitor_events' is already defined"（AstrBot 插件热重载标准防护）
if "maisaka_monitor_events" in SQLModel.metadata.tables:
    SQLModel.metadata.remove(SQLModel.metadata.tables["maisaka_monitor_events"])


class MaisakaMonitorEventRecord(SQLModel, table=True):
    """麦麦观察事件账本（表结构逐字段对齐 MaiBot MaisakaMonitorEventRecord）。"""

    __tablename__ = "maisaka_monitor_events"  # type: ignore
    __table_args__ = (
        Index("ix_maisaka_monitor_events_session_event", "session_id", "event_id"),
        Index("ix_maisaka_monitor_events_type_event", "event_type", "event_id"),
        Index("ix_maisaka_monitor_events_timestamp", "timestamp"),
        Index("ix_maisaka_monitor_events_created_at", "created_at"),
    )

    event_id: Optional[int] = Field(default=None, primary_key=True)
    event_type: str = Field(max_length=100)
    session_id: str = Field(default="", max_length=255)
    timestamp: float = Field(sa_column=Column(Float, nullable=False))
    schema_version: int = Field(default=1, sa_column=Column(Integer, nullable=False, server_default="1"))
    payload_json: str = Field(sa_column=Column(Text, nullable=False))
    created_at: datetime = Field(default_factory=datetime.now, sa_column=Column(DateTime))


class MonitorStore:
    """麦麦观察事件账本（对齐 MaiBot event_store.py 的 SQL 写法，SQLite 独立库）。

    MaiBot 挂在自身 MySQL 的 maisaka_monitor_events 表上；maisoul 无法在
    AstrBot 主库建表，用插件目录下独立 SQLite 文件承载同一张表/同一套语句。
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(
            f"sqlite:///{self.path}", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self._session_factory = sessionmaker(bind=self.engine, class_=Session, expire_on_commit=False)
        self._cleanup_lock = threading.Lock()
        self._records_since_cleanup = 0
        self._last_cleanup_at = 0.0
        self._import_legacy_json()

    def _import_legacy_json(self) -> None:
        """一次性迁移：v6.8 早期版本的 JSON 账本导入 SQL（有表数据则跳过）。"""
        legacy = self.path.with_suffix(".json")
        if not legacy.exists():
            return
        try:
            with self._session_factory() as session:
                if session.exec(select(MaisakaMonitorEventRecord).limit(1)).first():
                    return
                raw = json.loads(legacy.read_text(encoding="utf-8"))
                for r in (raw.get("records") or []):
                    session.add(MaisakaMonitorEventRecord(
                        event_type=str(r.get("event_type") or ""),
                        session_id=str(r.get("session_id") or ""),
                        timestamp=_coerce_float(r.get("timestamp"), default=time.time()),
                        schema_version=int(r.get("schema_version") or 1),
                        payload_json=str(r.get("payload_json") or "{}"),
                        created_at=datetime.fromtimestamp(
                            _coerce_float(r.get("created_at"), default=time.time()))))
                session.commit()
            legacy.rename(legacy.with_suffix(".json.imported"))
        except Exception:
            pass

    def record(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        """写入一条麦麦观察时间线事件，并返回带 event_id 的清洗后 payload。"""
        now = time.time()
        with self._session_factory() as session:
            cleaned_payload = sanitize_monitor_payload(payload)
            timestamp = _coerce_float(cleaned_payload.get("timestamp"), default=now)
            session_id = str(cleaned_payload.get("session_id") or "")
            record = MaisakaMonitorEventRecord(
                event_type=event_type,
                session_id=session_id,
                timestamp=timestamp,
                schema_version=MONITOR_EVENT_SCHEMA_VERSION,
                payload_json="{}",
                created_at=datetime.now(),
            )
            session.add(record)
            session.flush()

            if record.event_id is None:
                raise RuntimeError("麦麦观察事件写入后未获得 event_id")

            cleaned_payload["event_id"] = record.event_id
            cleaned_payload["schema_version"] = MONITOR_EVENT_SCHEMA_VERSION
            record.payload_json = json.dumps(
                cleaned_payload, ensure_ascii=False, separators=(",", ":"))
            session.add(record)

            if self._should_cleanup_monitor_events():
                self._cleanup_monitor_events(session)
            session.commit()
            return cleaned_payload

    def replay(self, *, since_event_id: int = 0,
               limit: int = DEFAULT_REPLAY_LIMIT) -> list[dict[str, Any]]:
        """按 event_id 返回可重放的麦麦观察事件。"""
        normalized_limit = max(1, min(limit, MAX_REPLAY_LIMIT))
        with self._session_factory() as session:
            if since_event_id > 0:
                statement = (
                    select(MaisakaMonitorEventRecord)
                    .where(MaisakaMonitorEventRecord.event_id > since_event_id)
                    .order_by(MaisakaMonitorEventRecord.event_id)
                    .limit(normalized_limit)
                )
                records = list(session.exec(statement).all())
            else:
                statement = (
                    select(MaisakaMonitorEventRecord)
                    .order_by(MaisakaMonitorEventRecord.event_id.desc())
                    .limit(normalized_limit)
                )
                records = list(reversed(session.exec(statement).all()))

        return [self._record_to_replay_event(r) for r in records]

    def cleanup(self) -> int:
        """清理超出保留策略的麦麦观察事件记录（对齐 cleanup_monitor_events）。"""
        with self._session_factory() as session:
            removed = self._cleanup_monitor_events(session)
            session.commit()
        return removed

    def _cleanup_monitor_events(self, session: Session) -> int:
        cutoff = datetime.now() - timedelta(hours=MAX_MONITOR_EVENT_AGE_HOURS)
        age_result = session.execute(
            text("DELETE FROM maisaka_monitor_events WHERE created_at < :cutoff"),
            {"cutoff": cutoff},
        )
        count_result = session.execute(
            text(
                """
                DELETE FROM maisaka_monitor_events
                WHERE event_id NOT IN (
                    SELECT event_id
                    FROM maisaka_monitor_events
                    ORDER BY event_id DESC
                    LIMIT :max_records
                )
                """
            ),
            {"max_records": MAX_MONITOR_EVENT_RECORDS},
        )
        removed_count = int(age_result.rowcount or 0) + int(count_result.rowcount or 0)
        return removed_count

    @staticmethod
    def _record_to_replay_event(record: MaisakaMonitorEventRecord) -> dict[str, Any]:
        payload = json.loads(record.payload_json)
        if not isinstance(payload, dict):
            payload = {}
        payload.setdefault("event_id", record.event_id)
        payload.setdefault("schema_version", record.schema_version)
        return {
            "event": record.event_type,
            "data": payload,
        }

    def _should_cleanup_monitor_events(self) -> bool:
        with self._cleanup_lock:
            self._records_since_cleanup += 1
            now = time.time()
            if (self._records_since_cleanup < CLEANUP_CHECK_INTERVAL_RECORDS
                    and now - self._last_cleanup_at < CLEANUP_CHECK_INTERVAL_SECONDS):
                return False
            self._records_since_cleanup = 0
            self._last_cleanup_at = now
            return True


def sanitize_monitor_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """清洗监控事件 payload，避免持久化大体积内联二进制（剔除 data_url）。"""
    cleaned = _sanitize_value(payload)
    if not isinstance(cleaned, dict):
        cleaned = {}
    return cleaned


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _sanitize_value(v) for k, v in value.items() if k != "data_url"}
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(v) for v in value]
    return value


def _coerce_float(value: Any, *, default: float) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return default
    return default


class MonitorBus:
    """订阅队列总线（对齐 websocket_manager.broadcast_to_topic 的广播语义）。"""

    def __init__(self):
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def publish(self, event: str, data: dict[str, Any]) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait({"event": event, "data": data})
            except Exception:
                self._subscribers.discard(q)


class Monitor:
    """事件入口：持久化（非 NON_PERSISTED）+ 广播，emit_* 签名对齐 MaiBot events.py。"""

    def __init__(self, store: MonitorStore, bus: MonitorBus | None = None):
        self.store = store
        self.bus = bus or MonitorBus()

    def _broadcast(self, event: str, data: dict[str, Any]) -> None:
        try:
            broadcast_data = data
            if event not in NON_PERSISTED_EVENTS:
                broadcast_data = self.store.record(event, data)
            self.bus.publish(event, broadcast_data)
        except Exception:
            pass

    def notify(self, event: str, data: dict[str, Any]) -> None:
        """同步上下文的发射口（本实现全程同步：落账本 + 入队广播）。"""
        self._broadcast(event, data)

    # ---- emit_*（字段与 MaiBot events.py 一致；MaiBot 为 async，插件侧统一同步发射） ----

    def emit_session_start(self, session_id: str, session_name: str, *,
                           is_group_chat: bool, group_id, user_id, platform: str) -> None:
        self._broadcast("session.start", {
            "session_id": session_id,
            "session_name": session_name,
            "is_group_chat": is_group_chat,
            "group_id": group_id,
            "user_id": user_id,
            "platform": platform,
            "timestamp": time.time(),
        })

    def emit_stage_status(self, *, session_id: str, session_name: str, stage: str,
                          detail: str = "", round_text: str = "", agent_state: str = "",
                          stage_started_at: float = 0.0, updated_at: float = 0.0,
                          timestamp: float = 0.0) -> None:
        now = time.time()
        self._broadcast("stage.status", {
            "session_id": session_id,
            "session_name": session_name,
            "stage": stage,
            "detail": detail,
            "round_text": round_text,
            "agent_state": agent_state,
            "stage_started_at": stage_started_at or now,
            "updated_at": updated_at or now,
            "timestamp": timestamp or now,
        })

    def emit_stage_removed(self, *, session_id: str, session_name: str = "") -> None:
        self._broadcast("stage.removed", {
            "session_id": session_id,
            "session_name": session_name,
            "timestamp": time.time(),
        })

    def emit_llm_error(self, *, session_id: str, task_name: str, request_type: str,
                       model_name: str, message: str) -> None:
        self._broadcast("llm.error", {
            "session_id": session_id,
            "task_name": task_name,
            "request_type": request_type,
            "model_name": model_name,
            "message": message,
            "timestamp": time.time(),
        })

    def emit_message_ingested(self, session_id: str, speaker_name: str, content: str,
                              message_id: str, timestamp: float, *, platform: str = "",
                              user_id: str = "", group_id: str = "",
                              reply_to=None, media=None) -> None:
        self._broadcast("message.ingested", {
            "session_id": session_id,
            "speaker_name": speaker_name,
            "content": content,
            "message_id": message_id,
            "platform": platform,
            "user_id": user_id,
            "group_id": group_id,
            "reply_to": reply_to,
            "media": media or [],
            "timestamp": timestamp or time.time(),
        })

    def emit_message_sent(self, session_id: str, speaker_name: str, content: str,
                          message_id: str, timestamp: float, source_kind: str = "", *,
                          platform: str = "", user_id: str = "", group_id: str = "",
                          reply_to=None, media=None) -> None:
        self._broadcast("message.sent", {
            "session_id": session_id,
            "speaker_name": speaker_name,
            "content": content,
            "message_id": message_id,
            "source_kind": source_kind,
            "platform": platform,
            "user_id": user_id,
            "group_id": group_id,
            "reply_to": reply_to,
            "media": media or [],
            "timestamp": timestamp or time.time(),
        })

    def emit_planner_finalized(self, *, session_id: str, cycle_id: int,
                               planner_request_messages=None,
                               planner_selected_history_count=None,
                               planner_tool_count=None,
                               planner_content=None,
                               planner_tool_calls=None,
                               planner_prompt_tokens=None,
                               planner_completion_tokens=None,
                               planner_total_tokens=None,
                               planner_duration_ms=None,
                               tools=None,
                               time_records=None,
                               agent_state: str = "",
                               planner_interrupted: bool = False,
                               end_reason: str = "",
                               end_detail: str = "",
                               eco_injection: str = "") -> None:
        """广播一轮 planner 结束后的最终聚合事件（MaiBot 原事件名与嵌套结构）。

        token 用量来自 AstrBot LLMResponse.usage（TokenUsage：input_other+
        input_cached=输入、output=输出），在 _planner_cycle 逐轮累计。
        native_tool_calls / prompt_html_uri 是 MaiBot Provider 专属，缺省即省。
        """
        self._broadcast("planner.finalized", {
            "session_id": session_id,
            "cycle_id": cycle_id,
            "timestamp": time.time(),
            "request": _serialize_request_block(
                planner_request_messages,
                planner_selected_history_count,
                planner_tool_count,
            ),
            "planner": _serialize_planner_block(
                planner_content,
                planner_tool_calls,
                planner_prompt_tokens,
                planner_completion_tokens,
                planner_total_tokens,
                planner_duration_ms,
            ),
            "tools": _serialize_tool_results(list(tools or [])),
            "interrupted": planner_interrupted,
            "final_state": {
                "time_records": dict(time_records or {}),
                "agent_state": agent_state,
                "end_reason": end_reason,
                "end_detail": end_detail,
                # maisoul 扩展：本轮 replyer 收集的生态注入全文（心弦好感/记忆/世界书）
                "eco_injection": eco_injection or "",
            },
        })


def _serialize_request_block(messages, selected_history_count, tool_count):
    if messages is None and selected_history_count is None and tool_count is None:
        return None
    return {
        "messages": [{"role": str(m.get("role", "unknown")),
                      "content": m.get("content")}
                     for m in list(messages or []) if isinstance(m, dict)],
        "selected_history_count": int(selected_history_count or 0),
        "tool_count": int(tool_count or 0),
    }


def _serialize_planner_block(content, tool_calls, prompt_tokens,
                             completion_tokens, total_tokens, duration_ms):
    if (content is None and tool_calls is None and duration_ms is None
            and prompt_tokens is None and completion_tokens is None
            and total_tokens is None):
        return None
    return {
        "content": content,
        "tool_calls": [
            {"id": str(tc.get("id", "")),
             "name": str(tc.get("name", "unknown")),
             "arguments": tc.get("arguments", {})}
            for tc in list(tool_calls or []) if isinstance(tc, dict)
        ],
        "prompt_tokens": int(prompt_tokens or 0),
        "completion_tokens": int(completion_tokens or 0),
        "total_tokens": int(total_tokens or 0),
        "duration_ms": float(duration_ms or 0.0),
    }


def _serialize_tool_results(tools):
    """标准化最终 planner 卡中的工具结果列表（对齐 _serialize_tool_results）。"""
    out = []
    for tool in tools:
        item = {
            "tool_call_id": str(tool.get("tool_call_id", "")),
            "tool_name": str(tool.get("tool_name", "")),
            "tool_args": tool.get("tool_args", {}),
            "tool_call_source": str(tool.get("tool_call_source", "")),
            "tool_call_source_label": str(tool.get("tool_call_source_label", "")),
            "success": bool(tool.get("success", False)),
            "duration_ms": float(tool.get("duration_ms", 0.0) or 0.0),
            "summary": str(tool.get("summary", "")),
        }
        detail = tool.get("detail")
        if detail is not None:
            item["detail"] = detail
        out.append(item)
    return out
