"""独立模式生成与发送（M7 自 main.py 机械拆出，行为零变化）。"""

from __future__ import annotations

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..core import bridge, learning, monitor, personas, prompt, sender
from ..core.states import session_key
try:
    from astrbot.api.event import MessageChain
except ImportError:
    from astrbot.core.message.message_event_result import MessageChain
from astrbot.api.message_components import Reply

from .ecobridge import _eco_fire_response, _eco_inject_block
from .events_util import _emit_sent, _monitor_stage, _msg_id, _resp_text
from .modelbind_host import _embedding_provider, _pick_task_model, _task_text_chat


async def _generate_and_send(P, event: AstrMessageEvent, st, reason: str,
                             style: str, trigger_text: str = "", is_group: bool = True):
    provider = P.context.get_using_provider()
    if provider is None:
        logger.warning("maisoul: 未配置可用的模型 Provider，本次跳过发言")
        return

    # 会话键与 _process_chat/on_llm_* 回声钩子同式（群=group_id，私聊=sender_id，
    # 均空才回退 umo——坑 23：私聊漏掉 sender_id 会让观察账本落错会话、
    # 学习库按 item_id=用户ID 的匹配全部失效）
    gid = session_key(event)
    umo = event.unified_msg_origin
    platform = str(event.get_platform_name() or "")
    eff_cfg, pname = await personas.resolve_active(
        P.context, P.config, gid, umo)
    st.last_persona = pname
    system_prompt = prompt.build_system_prompt(eff_cfg, chat_id=gid, platform=platform,
                                               is_group=is_group)
    system_prompt += bridge.build_skills_block(eff_cfg)
    eco_block, eco_extras = await _eco_inject_block(P, event, trigger_text)
    if eco_block:
        system_prompt += f"\n\n{eco_block}"

    # 学习注入块（表达习惯选择 + 关键词反应 —— 格式对齐 MaiBot 原文）；
    # 黑话参考已移至 planner 每轮注入（对齐 jargon_context_matcher 位置）
    use_expr, _ = learning.learning_flags(
        eff_cfg, "expression_learning_list", platform, gid, is_group)
    expr_block = ""
    if use_expr:
        observe = learning.build_chat_info(list(st.buffer))
        expr_bind = _pick_task_model(P, "expression_use", eff_cfg)
        emb = _embedding_provider(P, eff_cfg)
        expr_block = await learning.select_expression_habits_block(
            expr_bind[0] if expr_bind else provider, P.learning_store,
            learning.share_key(eff_cfg, "expression_groups", platform, gid),
            bool(eff_cfg.get("expression_checked_only", True)),
            observe, str(eff_cfg.get("bot_name") or "麦麦"), reason,
            model=expr_bind[1] if expr_bind else None,
            mode=str(eff_cfg.get("expression_selection_mode") or "legacy"),
            embedding=emb,
            embedding_model=str((getattr(emb, "provider_config", None) or {})
.get("id", "") or "") if emb is not None else "",
            query_text=learning.build_expression_query_text(reply_reason=reason),
            pool_size=int(eff_cfg.get("expression_vector_candidate_pool_size", 50) or 50))
    keyword_block = learning.keyword_reaction_block(eff_cfg, trigger_text)

    user_message = prompt.build_final_user_message(
        st, eff_cfg, reason, style,
        expression_habits=expr_block,
        keyword_reaction=keyword_block)

    func_tool = bridge.build_chat_toolset(P.context, eff_cfg)
    if func_tool is not None:
        system_prompt += bridge.MAID_BRIDGE_PROMPT

    _monitor_stage(P, gid, monitor.STAGE_REPLYER, "生成可见回复", agent_state="running")
    try:
        resp = await _task_text_chat(P, 
            "replyer", eff_cfg,
            prompt=user_message,
            session_id=f"maisoul_{gid}",
            system_prompt=system_prompt,
            func_tool=func_tool,
            extra_user_content_parts=eco_extras or None,
        )
    except Exception as e:
        P.monitor.emit_llm_error(
            session_id=gid, task_name="replyer", request_type="text_chat",
            model_name=str(getattr(provider, "id", "") or type(provider).__name__),
            message=str(e))
        raise

    if getattr(resp, "tools_call_name", None):
        results = await bridge.exec_tool_calls(P.context, event, resp)
        if results:
            logger.info(f"maisoul[{gid}] 管家桥执行: {results[:120]}")
            user_message += (
                f"\n\n【管家执行结果】\n{results}\n\n"
                "请结合结果，用你自己的口吻输出给群友的发言内容。"
            )
        resp = await _task_text_chat(P, 
            "replyer", eff_cfg,
            prompt=user_message,
            session_id=f"maisoul_{gid}",
            system_prompt=system_prompt,
            extra_user_content_parts=eco_extras or None,
        )

    answer = _resp_text(resp)
    if not answer:
        logger.info(f"maisoul[{gid}] 模型未返回内容，放弃本次发言")
        return

    # WebUI 聊天页是单气泡：AstrBot 的 run accumulator 对 streaming=False 的
    # plain 事件做替换，逐段 event.send 会互相覆盖——先攒段，结束后合并一次发。
    webchat = str(event.get_platform_name() or "") == "webchat"
    webchat_buf: list[str] = []
    # 引用回复（对齐 MaiBot reply 的 set_quote 默认 true）：首段挂 Reply(触发消息)
    quote_id = (_msg_id(event)
                if not webchat and eff_cfg.get("enable_reply_quote", True) else "")
    quoted = {"done": False}

    async def send(text: str) -> None:
        if webchat:
            webchat_buf.append(text)
            return
        if quote_id and not quoted["done"]:
            quoted["done"] = True
            await P.context.send_message(
                umo, MessageChain([Reply(id=quote_id)]).message(text))
        else:
            await P.context.send_message(umo, MessageChain().message(text))
        _emit_sent(P, gid, text, _msg_id(event), "reply", event)

    sent = await sender.send_humanlike(send, answer, eff_cfg)
    if webchat and webchat_buf:
        await event.send(MessageChain().message("\n\n".join(webchat_buf)))
        for seg_text in webchat_buf:
            _emit_sent(P, gid, seg_text, _msg_id(event), "reply", event)
    st.record_self_reply(_msg_id(event), sent,
                         str(eff_cfg.get("bot_name") or P.config["bot_name"]),
                         quote=quote_id if quoted["done"] else "")
    logger.info(f"maisoul[{gid}] 已发言 {len(sent)} 段（人格={pname}）")
    await _eco_fire_response(P, event, "\n".join(sent))
    _schedule_learning(P, provider, eff_cfg, st, platform, gid)

# ------------------------------------------------------------------ #
# 生态注入桥（v6.11.0）：手动触发 on_llm_request/on_llm_response 钩子链，  #
# 心弦好感/记忆/世界书等注入型插件在麦麦管线内同样生效                       #

def _webchat_sender(P, event: AstrMessageEvent):
    """WebUI 聊天页发送通道：段落经 event.send 流进当前请求的气泡。"""
    async def _send(text: str) -> None:
        await event.send(MessageChain().message(text))
    return _send

def _schedule_learning(P, provider, eff_cfg, st, platform: str, gid: str):
    """发言后异步学习表达/黑话（对齐 MaiBot 学习器：失败不影响发言）。"""
    try:
        _, learn_expr = learning.learning_flags(
            eff_cfg, "expression_learning_list", platform, gid)
        _, learn_jargon = learning.learning_flags(
            eff_cfg, "jargon_learning_list", platform, gid)
        if not (learn_expr or learn_jargon):
            return
        snapshot_cfg = dict(eff_cfg)
        snapshot_buf = list(st.buffer)[-30:]
        learn_bind = _pick_task_model(P, "learner", snapshot_cfg)

        async def _run():
            try:
                summary = await learning.learn_from_chat(
                    learn_bind[0] if learn_bind else provider,
                    snapshot_cfg, snapshot_buf, platform, gid,
                    P.learning_store,
                    model=learn_bind[1] if learn_bind else None)
                if summary and summary != "学习未启用":
                    logger.info(f"maisoul[{gid}] 学习: {summary}")
            except Exception:
                logger.debug("maisoul: 学习任务失败", exc_info=True)

        P._spawn(_run(), name=f"learning:{gid}")
    except Exception:
        logger.debug("maisoul: 学习任务调度失败", exc_info=True)

# ------------------------------------------------------------------ #
# Planner 决策模式：maisaka agent 循环                                  #
