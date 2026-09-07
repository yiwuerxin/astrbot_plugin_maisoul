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

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    Index,
    Integer,
    Text,
    create_engine,
    text,
)
from sqlmodel import Field, Session, SQLModel, select
from sqlalchemy.orm import sessionmaker

from astrbot.api import logger

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
    schema_version: int = Field(
        default=1, sa_column=Column(Integer, nullable=False, server_default="1")
    )
    payload_json: str = Field(sa_column=Column(Text, nullable=False))
    created_at: datetime = Field(
        default_factory=datetime.now, sa_column=Column(DateTime)
    )


class MonitorStore:
    """麦麦观察事件账本（对齐 MaiBot event_store.py 的 SQL 写法，SQLite 独立库）。

    MaiBot 挂在自身 MySQL 的 maisaka_monitor_events 表上；maisoul 无法在
    AstrBot 主库建表，用插件目录下独立 SQLite 文件承载同一张表/同一套语句。
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(
            f"sqlite:///{self.path}", connect_args={"check_same_thread": False}
        )
        SQLModel.metadata.create_all(self.engine)
        self._session_factory = sessionmaker(
            bind=self.engine, class_=Session, expire_on_commit=False
        )
        self._cleanup_lock = threading.Lock()
        self._records_since_cleanup = 0
        self._last_cleanup_at = 0.0
        self._closed = False
        self._import_legacy_json()

    def close(self) -> None:
        """释放连接池（插件卸载 terminate 时调用；幂等——热重载会重建）。"""
        if self._closed:
            return
        self._closed = True
        try:
            self.engine.dispose()
        except Exception:
            logger.debug("maisoul: 监控库连接池释放失败", exc_info=True)

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
                for r in raw.get("records") or []:
                    session.add(
                        MaisakaMonitorEventRecord(
                            event_type=str(r.get("event_type") or ""),
                            session_id=str(r.get("session_id") or ""),
                            timestamp=_coerce_float(
                                r.get("timestamp"), default=time.time()
                            ),
                            schema_version=int(r.get("schema_version") or 1),
                            payload_json=str(r.get("payload_json") or "{}"),
                            created_at=datetime.fromtimestamp(
                                _coerce_float(r.get("created_at"), default=time.time())
                            ),
                        )
                    )
                session.commit()
            legacy.rename(legacy.with_suffix(".json.imported"))
        except Exception:
            logger.debug(
                "maisoul: 旧 JSON 观察账本迁移失败（保留原文件，不影响运行）",
                exc_info=True,
            )

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
                cleaned_payload, ensure_ascii=False, separators=(",", ":")
            )
            session.add(record)

            if self._should_cleanup_monitor_events():
                self._cleanup_monitor_events(session)
            session.commit()
            return cleaned_payload

    def replay(
        self, *, since_event_id: int = 0, limit: int = DEFAULT_REPLAY_LIMIT
    ) -> list[dict[str, Any]]:
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
            text("""
                DELETE FROM maisaka_monitor_events
                WHERE event_id NOT IN (
                    SELECT event_id
                    FROM maisaka_monitor_events
                    ORDER BY event_id DESC
                    LIMIT :max_records
                )
                """),
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
            if (
                self._records_since_cleanup < CLEANUP_CHECK_INTERVAL_RECORDS
                and now - self._last_cleanup_at < CLEANUP_CHECK_INTERVAL_SECONDS
            ):
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
    """订阅队列总线（对齐 websocket_manager.broadcast_to_topic 的广播语义）。

    M12：订阅队列有界（500）——慢 SSE 消费者不再让队列无限吃内存，
    满时丢最旧保最新（观察页语义：最新状态比完整历史重要）。
    """

    _QUEUE_MAX = 500

    def __init__(self):
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._QUEUE_MAX)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def publish(self, event: str, data: dict[str, Any]) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait({"event": event, "data": data})
            except asyncio.QueueFull:
                try:
                    q.get_nowait()  # 丢最旧，保最新
                    q.put_nowait({"event": event, "data": data})
                except Exception:
                    pass  # 降级：丢旧重放也失败（订阅刚被移除）则放弃本条
            except Exception:
                self._subscribers.discard(q)


class Monitor:
    """事件入口：持久化（非 NON_PERSISTED）+ 广播，emit_* 签名对齐 MaiBot events.py。"""

    def __init__(self, store: MonitorStore, bus: MonitorBus | None = None):
        self.store = store
        self.bus = bus or MonitorBus()
        self._queue: asyncio.Queue | None = None  # M10 writer 队列（None=直写）
        self._writer: asyncio.Task | None = None
        self._dropped = 0  # M12：队列满丢弃计数

    def close(self) -> None:
        """卸载时释放账本连接池（透传 MonitorStore.close，幂等）。"""
        self.store.close()

    def _broadcast(self, event: str, data: dict[str, Any]) -> None:
        # M10：writer 启动时 emit 只入队，SQL 落库经 to_thread 移出事件循环
        # （逐消息同步 commit + planner.finalized 大 payload 是高流量群的
        # 延迟放大器）；writer 未启动（测试/离线）保持直写，行为不变
        if self._writer is not None and self._queue is not None:
            try:
                self._queue.put_nowait((event, data))
            except asyncio.QueueFull:
                self._dropped += 1  # 有界队列（M12）：慢消费丢弃最新并计数
            return
        self._dispatch(event, data)

    def _dispatch(self, event: str, data: dict[str, Any]) -> None:
        try:
            broadcast_data = data
            if event not in NON_PERSISTED_EVENTS:
                broadcast_data = self.store.record(event, data)
            self.bus.publish(event, broadcast_data)
        except Exception:
            # 观察账本写入失败不阻断聊天管线，但必须留痕（高频路径用 debug）
            logger.debug(f"maisoul: 麦麦观察事件写入失败: {event}", exc_info=True)

    def start_writer(self) -> None:
        """启动后台落库 writer 协程（M10）。幂等；需在事件循环内调用。"""
        if self._writer is not None and not self._writer.done():
            return
        self._queue = asyncio.Queue(maxsize=2000)
        self._writer = asyncio.create_task(self._writer_loop(), name="monitor_writer")

    _SENTINEL = object()  # stop_writer 的优雅退出信号

    async def _writer_loop(self) -> None:
        q = self._queue
        while q is not None:
            item = await q.get()
            if item is self._SENTINEL:
                return
            # 批量排水：顺手取走已积压项（不再等待新事件），保持顺序
            batch = [item]
            while len(batch) < 64 and not q.empty():
                nxt = q.get_nowait()
                if nxt is self._SENTINEL:
                    return  # 排水中收到停止信号：本批已取项照常落库后退出
                batch.append(nxt)
            for ev, d in batch:
                try:
                    broadcast_data = d
                    if ev not in NON_PERSISTED_EVENTS:
                        broadcast_data = await asyncio.to_thread(
                            self.store.record, ev, d
                        )
                    self.bus.publish(ev, broadcast_data)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.debug(f"maisoul: 麦麦观察事件写入失败: {ev}", exc_info=True)

    async def stop_writer(self, timeout: float = 5.0) -> None:
        """优雅冲刷并停止（terminate 用）。

        哨兵让 writer 处理完队列再退出（Sourcery：直接 cancel 会把正在
        to_thread 落库的当前批一起丢掉）；超时才 cancel，残余同步落库。"""
        writer, q = self._writer, self._queue
        self._writer, self._queue = None, None
        if writer is None:
            return
        try:
            q.put_nowait(self._SENTINEL)
            await asyncio.wait_for(writer, timeout=timeout)
        except (asyncio.TimeoutError, asyncio.QueueFull):
            writer.cancel()
            await asyncio.wait([writer], timeout=1.0)
        if q is not None:
            while not q.empty():
                item = q.get_nowait()
                if item is not self._SENTINEL:
                    self._dispatch(*item)

    def notify(self, event: str, data: dict[str, Any]) -> None:
        """同步上下文的发射口（本实现全程同步：落账本 + 入队广播）。"""
        self._broadcast(event, data)

    # ---- emit_*（字段与 MaiBot events.py 一致；MaiBot 为 async，插件侧统一同步发射） ----

    def emit_session_start(
        self,
        session_id: str,
        session_name: str,
        *,
        is_group_chat: bool,
        group_id,
        user_id,
        platform: str,
    ) -> None:
        self._broadcast(
            "session.start",
            {
                "session_id": session_id,
                "session_name": session_name,
                "is_group_chat": is_group_chat,
                "group_id": group_id,
                "user_id": user_id,
                "platform": platform,
                "timestamp": time.time(),
            },
        )

    def emit_stage_status(
        self,
        *,
        session_id: str,
        session_name: str,
        stage: str,
        detail: str = "",
        round_text: str = "",
        agent_state: str = "",
        stage_started_at: float = 0.0,
        updated_at: float = 0.0,
        timestamp: float = 0.0,
    ) -> None:
        now = time.time()
        self._broadcast(
            "stage.status",
            {
                "session_id": session_id,
                "session_name": session_name,
                "stage": stage,
                "detail": detail,
                "round_text": round_text,
                "agent_state": agent_state,
                "stage_started_at": stage_started_at or now,
                "updated_at": updated_at or now,
                "timestamp": timestamp or now,
            },
        )

    def emit_llm_error(
        self,
        *,
        session_id: str,
        task_name: str,
        request_type: str,
        model_name: str,
        message: str,
    ) -> None:
        self._broadcast(
            "llm.error",
            {
                "session_id": session_id,
                "task_name": task_name,
                "request_type": request_type,
                "model_name": model_name,
                "message": message,
                "timestamp": time.time(),
            },
        )

    def emit_message_ingested(
        self,
        session_id: str,
        speaker_name: str,
        content: str,
        message_id: str,
        timestamp: float,
        *,
        platform: str = "",
        user_id: str = "",
        group_id: str = "",
        reply_to=None,
        media=None,
    ) -> None:
        self._broadcast(
            "message.ingested",
            {
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
            },
        )

    def emit_message_sent(
        self,
        session_id: str,
        speaker_name: str,
        content: str,
        message_id: str,
        timestamp: float,
        source_kind: str = "",
        *,
        platform: str = "",
        user_id: str = "",
        group_id: str = "",
        reply_to=None,
        media=None,
    ) -> None:
        self._broadcast(
            "message.sent",
            {
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
            },
        )

    def emit_planner_finalized(
        self,
        *,
        session_id: str,
        cycle_id: int,
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
        eco_injection: str = "",
        planner_system_prompt: str = "",
        reasoning_by_idx=None,
        replyer_reasoning: str = "",
    ) -> None:
        """广播一轮 planner 结束后的最终聚合事件（MaiBot 原事件名与嵌套结构）。

        token 用量来自 AstrBot LLMResponse.usage（TokenUsage：input_other+
        input_cached=输入、output=输出），在 _planner_cycle 逐轮累计。
        native_tool_calls / prompt_html_uri 是 MaiBot Provider 专属，缺省即省。
        system_prompt / messages[].tool_calls 是 maisoul 扩展（推理过程页复刻
        部署版 ReasoningLogViewerPage 需要，MaiBot 从 dump 文件取）。
        """
        self._broadcast(
            "planner.finalized",
            {
                "session_id": session_id,
                "cycle_id": cycle_id,
                "timestamp": time.time(),
                "request": _serialize_request_block(
                    planner_request_messages,
                    planner_selected_history_count,
                    planner_tool_count,
                    planner_system_prompt or None,
                    reasoning_map=reasoning_by_idx,
                ),
                "planner": _serialize_planner_block(
                    planner_content,
                    planner_tool_calls,
                    planner_prompt_tokens,
                    planner_completion_tokens,
                    planner_total_tokens,
                    planner_duration_ms,
                    replyer_reasoning,
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
            },
        )


def _serialize_request_block(
    messages, selected_history_count, tool_count, system_prompt=None, reasoning_map=None
):
    if messages is None and selected_history_count is None and tool_count is None:
        return None
    out = {
        "messages": [],
        "selected_history_count": int(selected_history_count or 0),
        "tool_count": int(tool_count or 0),
    }
    rmap = {int(k): v for k, v in dict(reasoning_map or {}).items()}
    for i, m in enumerate(list(messages or [])):
        if not isinstance(m, dict):
            continue
        item = {"role": str(m.get("role", "unknown")), "content": m.get("content")}
        # 推理过程页：assistant 轮附思考（ReasoningItem）——仅进监控副本，
        # 回灌 contexts 永不带 reasoning（坑 54），两边互不影响
        reasoning = str(rmap.get(i, "") or "").strip()
        if reasoning and item["role"] == "assistant":
            item["reasoning"] = reasoning
        if m.get("tool_calls"):
            item["tool_calls"] = m["tool_calls"]
        if str(m.get("role")) == "tool" and m.get("tool_call_id"):
            item["tool_call_id"] = str(m["tool_call_id"])
        out["messages"].append(item)
    if system_prompt:
        out["system_prompt"] = str(system_prompt)
    return out


def _serialize_planner_block(
    content,
    tool_calls,
    prompt_tokens,
    completion_tokens,
    total_tokens,
    duration_ms,
    replyer_reasoning="",
):
    if (
        content is None
        and tool_calls is None
        and duration_ms is None
        and prompt_tokens is None
        and completion_tokens is None
        and total_tokens is None
    ):
        return None
    out = {
        "content": content,
        "tool_calls": [
            {
                "id": str(tc.get("id", "")),
                "name": str(tc.get("name", "unknown")),
                "arguments": tc.get("arguments", {}),
            }
            for tc in list(tool_calls or [])
            if isinstance(tc, dict)
        ],
        "prompt_tokens": int(prompt_tokens or 0),
        "completion_tokens": int(completion_tokens or 0),
        "total_tokens": int(total_tokens or 0),
        "duration_ms": float(duration_ms or 0.0),
    }
    if str(replyer_reasoning or "").strip():
        out["reasoning"] = str(
            replyer_reasoning
        ).strip()  # 推理过程页：reply 工具的回复器思考
    return out


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
