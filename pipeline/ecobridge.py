"""生态注入桥（M7 自 main.py 机械拆出，行为零变化）。

绕过 call_event_hook 直遍注册表（麦麦管线已 stop_event，官方遍历会在
第一个 handler 后中断），触发心弦/记忆/世界书等插件的注入与回写。
"""

from __future__ import annotations

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.core.provider.entities import LLMResponse, ProviderRequest
from astrbot.core.star.star_handler import EventType, star_handlers_registry


def _normalize_extra_parts(parts: list) -> list:
    """生态钩子写入的 extra_user_content_parts 归一化为 ContentPart 列表。

    - ContentPart 子类：原样透传（保留 mark_as_temp 的 _no_save 语义）
    - 裸字符串：包成 TextPart
    - 其它对象（Plain 组件等）：取 .text 文本包成 TextPart，取不到则丢弃
      ——绝不 str() 整个对象，repr 垃圾既进不了模型也不能进观察页。
    """
    from astrbot.core.agent.message import ContentPart, TextPart
    normalized = []
    for x in parts or []:
        if isinstance(x, ContentPart):
            normalized.append(x)
        elif isinstance(x, str):
            if x.strip():
                normalized.append(TextPart(text=x))
        else:
            text = str(getattr(x, "text", "") or "").strip()
            if text:
                normalized.append(TextPart(text=text))
    return normalized

def _extra_part_text(part) -> str:
    """观察页展示用：ContentPart/组件 → 纯文本（图片等无文本段以类名占位）。"""
    text = getattr(part, "text", None)
    return str(text) if text is not None else f"<{type(part).__name__}>"

async def _eco_inject_block(P, event: AstrMessageEvent, text: str):
    """触发生态插件的 on_llm_request 钩子，收集两个注入通道的内容。

    返回 (system_block, extra_parts)：
    - system_block：写 req.system_prompt 的注入（心弦好感/世界书），拼进系统提示词
    - extra_parts：写 req.extra_user_content_parts 的注入（livingmemory 记忆召回
      特意走用户内容附加——它已废弃 system_prompt 方式以保护前缀缓存），
      按 livingmemory 设计语义经 extra_user_content_parts 传入 text_chat（与识图同通道）

    不走官方 call_event_hook：它逐 handler 检查 event.is_stopped()，而麦麦
    管线在 planner/independent 模式下早已 stop_event（静默闸门），会在第一
    个 handler 后中断——此处直遍注册表，注入不改变事件传播状态。
    单个插件注入异常只废它自己的注入（对齐 call_event_hook 逐 handler 捕获）。
    """
    if not P.config.get("eco_injection", True):
        return "", []
    try:
        req = ProviderRequest(prompt=text or "",
                              session_id=event.unified_msg_origin,
                              system_prompt="", contexts=[], func_tool=None)
        fired = 0
        for handler in star_handlers_registry.get_handlers_by_event_type(
                EventType.OnLLMRequestEvent, plugins_name=event.plugins_name):
            if "astrbot_plugin_maisoul" in str(handler.handler_module_path):
                continue  # 自家 native 模式钩子，防三件套重复注入
            try:
                await handler.handler(event, req)
                fired += 1
            except BaseException:
                logger.error(f"maisoul: 生态注入 {handler.handler_name} 异常",
                             exc_info=True)
        block = (req.system_prompt or "").strip()
        extras = _normalize_extra_parts(
            getattr(req, "extra_user_content_parts", None) or [])
        logger.info(f"maisoul: 生态注入桥执行 {fired} 个钩子，system {len(block)} 字符"
                    f" + 用户内容附加 {len(extras)} 段"
                    f"（好感/记忆/世界书；私聊心弦不注入属正常）")
        return block, extras
    except Exception:
        logger.error("maisoul: 生态注入桥失败", exc_info=True)
        return "", []

async def _eco_fire_response(P, event: AstrMessageEvent, answer: str):
    """发言后触发 on_llm_response 钩子（livingmemory 记忆沉淀等生态回写）。"""
    if not P.config.get("eco_injection", True) or not (answer or "").strip():
        return
    try:
        event.set_extra("maisoul_eco_resp", True)  # 自家回声钩子防重入
        resp = LLMResponse(role="assistant", completion_text=answer)
        for handler in star_handlers_registry.get_handlers_by_event_type(
                EventType.OnLLMResponseEvent, plugins_name=event.plugins_name):
            if "astrbot_plugin_maisoul" in str(handler.handler_module_path):
                continue
            try:
                await handler.handler(event, resp)
            except BaseException:
                logger.error(f"maisoul: 生态回写 {handler.handler_name} 异常",
                             exc_info=True)
    except Exception:
        logger.error("maisoul: 生态回写失败", exc_info=True)


async def xinxian_profile_block(P, gid: str, uid: str) -> str:
    """P-H 跨插件联动：好心弦 facade 取好感画像渲染进系统提示词。

    任一插件缺失/旧版无方法/任何异常一律返回空串静默降级（联动是增强，
    不是依赖）；开关 xinxian_link 默认关。"""
    if not bool(P.config.get("xinxian_link", False)) or not uid:
        return ""
    try:
        star = P.context.get_registered_star("astrbot_plugin_xinxian")
        api = getattr(getattr(star, "star_cls", None), "api", None) if star else None
        if api is None:
            return ""
        prof = await api.get_profile(gid, uid)
        if not prof:
            return ""
        lines = ["\n\n【好感档案（来自心弦插件）】",
                 f"你与对方的好感度：{prof.get('favor')}（等级：{prof.get('level')}）",
                 f"态度参考：{prof.get('guidance')}"]
        if prof.get("impression"):
            tag = f"（{'、'.join(prof.get('tags') or [])}）" if prof.get("tags") else ""
            lines.append(f"你对 TA 的印象：{prof['impression']}{tag}")
        if prof.get("relationship"):
            lines.append(f"你们的关系：{prof['relationship']}")
        return "\n".join(lines)
    except Exception:
        logger.debug("maisoul: 心弦联动降级（插件缺失或版本过旧）", exc_info=True)
        return ""
