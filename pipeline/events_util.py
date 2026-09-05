"""事件与观察工具（M7 自 main.py 机械拆出，行为零变化）。

消息组件解析（msg_id/引用/@/识图引用）、会话记录、观察事件上报。
P 为插件实例（组合根），模块函数首参；静态工具不依赖 P。
"""

from __future__ import annotations

import time

from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import At, Reply

from ..core.states import session_key


def _msg_id(event: AstrMessageEvent) -> str:
    try:
        return str(event.message_obj.message_id or "")
    except Exception:
        return ""

def _record(P, event: AstrMessageEvent, text: str, gid: str | None = None):
    """gid 由调用方传入（群=群号，私聊=用户ID），保证与门控使用同一会话状态。"""
    if gid is None:
        gid = session_key(event)
    sender_name = event.get_sender_name() or str(event.get_sender_id())
    group_id = str(event.get_group_id() or "")
    P.states.get(gid).record_external({
        "name": sender_name,
        "sid": str(event.get_sender_id()),
        "msg_id": _msg_id(event),
        "text": text if text else "[图片/表情]",
        "at_bot": _has_at_bot(P, event),
        "reply_bot": _is_reply_to_bot(P, event),
        "quote": _quote_ids(event),  # 引用目标（<message quote="…"> 属性用）
        "ts": time.time(),
        "images": _extract_image_refs(event),  # 识图上下文用（v6.9.9，只存引用）
    })
    # 麦麦观察：消息注入事件（字段对齐 emit_message_ingested）
    P.monitor.emit_message_ingested(
        gid, sender_name, text if text else "[图片/表情]",
        _msg_id(event), time.time(),
        platform=str(event.get_platform_name() or ""),
        user_id=str(event.get_sender_id() or ""),
        group_id=group_id,
    )

def _monitor_stage(P, gid: str, stage: str, detail: str = "",
                   round_text: str = "", agent_state: str = "") -> None:
    """阶段状态上报（stage 名对齐 MaiBot reasoning_engine；不落账本仅广播）。"""
    P.monitor.emit_stage_status(
        session_id=gid, session_name=_session_name(P, gid),
        stage=stage, detail=detail, round_text=round_text,
        agent_state=agent_state)

def _session_name(P, gid: str) -> str:
    if gid in P._group_sessions:
        return f"群 {gid}"
    return f"私聊 {gid}"

def _emit_sent(P, gid: str, content: str, msg_id: str, source_kind: str,
               event: AstrMessageEvent | None = None) -> None:
    """麦麦观察：自己发送的消息事件（字段对齐 emit_message_sent）。"""
    P.monitor.emit_message_sent(
        gid, str(P.config.get("bot_name") or "麦麦"), content,
        msg_id, time.time(), source_kind,
        platform=str(event.get_platform_name() or "") if event else "",
        user_id=str(event.get_sender_id() or "") if event else "",
        group_id=str(event.get_group_id() or "") if event else "",
    )

def _extract_image_refs(event: AstrMessageEvent) -> list[str]:
    """消息内 Image 组件的可解析引用（url/file/path，去重；只存引用不落盘）。"""
    refs: list[str] = []
    try:
        from astrbot.api.message_components import Image as _Img
        for seg in event.get_messages():
            if isinstance(seg, _Img):
                ref = str(getattr(seg, "url", "") or getattr(seg, "file", "")
                          or getattr(seg, "path", "") or "").strip()
                if ref and ref not in refs:
                    refs.append(ref)
    except Exception:
        pass
    return refs

def _quote_ids(event: AstrMessageEvent) -> str:
    """消息 Reply 组件的引用目标 ID（去重逗号拼接，对齐
    extract_quote_ids_from_message_sequence；渲染进 <message quote="…">）。"""
    ids: list[str] = []
    try:
        for seg in event.get_messages():
            if isinstance(seg, Reply):
                qid = str(getattr(seg, "id", "") or "").strip()
                if qid and qid not in ids:
                    ids.append(qid)
    except Exception:
        pass
    return ",".join(ids)

def _has_at_bot(P, event: AstrMessageEvent) -> bool:
    try:
        for seg in event.get_messages():
            if isinstance(seg, At) and str(seg.qq) == str(event.get_self_id()):
                return True
    except Exception:
        pass
    return False

def _is_reply_to_bot(P, event: AstrMessageEvent) -> bool:
    try:
        for seg in event.get_messages():
            if isinstance(seg, Reply) and str(seg.sender_id) == str(event.get_self_id()):
                return True
    except Exception:
        pass
    return False

def _resp_text(resp) -> str:
    """提取 LLMResponse 可见文本。

    completion_text 缺失时走 result_chain 逐组件取 .text——绝不 str()
    整个组件：纯工具调用轮链上常是空 Plain，str() 得到 pydantic repr
    （type=<ComponentType.Plain...> text=''），会冒充思考文本并顶掉
    reasoning_content 兜底（坑 49 同族，v6.13.3）。
    """
    if not resp:
        return ""
    txt = getattr(resp, "completion_text", None)
    if txt:
        return str(txt).strip()
    chain = getattr(resp, "result_chain", None)
    if chain:
        parts = [str(getattr(c, "text", "") or "")
                 for c in getattr(chain, "chain", [])]
        return "".join(parts).strip()
    return ""
