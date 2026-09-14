"""独立模式生成与发送（M7 自 main.py 机械拆出，行为零变化）。"""

from __future__ import annotations

import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..core import bridge, learning, monitor, personas, prompt, sender
from ..core.demote import demote_quote
from ..core.states import session_key

try:
    from astrbot.api.event import MessageChain
except ImportError:
    from astrbot.core.message.message_event_result import MessageChain
from astrbot.api.message_components import Reply

from .ecobridge import _eco_fire_response, _eco_inject_block
from .events_util import _emit_sent, _monitor_stage, _msg_id, _resp_text
from .modelbind_host import (
    _embedding_provider,
    _pick_task_model,
    _task_text_chat,
    replyer_image_capable,
)


async def _generate_and_send(
    P,
    event: AstrMessageEvent,
    st,
    reason: str,
    style: str,
    trigger_text: str = "",
    is_group: bool = True,
):
    provider = P.context.get_using_provider()
    if provider is None:
        logger.warning("maisoul: 未配置可用的模型 Provider，本次跳过发言")
        return
    # P-D：生成开始时刻（时间戳基线，v6.20.3——buffer 是 maxlen=200 的滚动
    # deque，条数基线在满载滚动时索引漂移会漏计生成期新消息）
    gen_baseline = time.time()

    # 会话键与 _process_chat/on_llm_* 回声钩子同式（群=group_id，私聊=sender_id，
    # 均空才回退 umo——坑 23：私聊漏掉 sender_id 会让观察账本落错会话、
    # 学习库按 item_id=用户ID 的匹配全部失效）
    gid = session_key(event)
    umo = event.unified_msg_origin
    platform = str(event.get_platform_name() or "")
    eff_cfg, pname = await personas.resolve_active(P.context, P.config, gid, umo)
    st.last_persona = pname
    system_prompt = prompt.build_system_prompt(
        eff_cfg, chat_id=gid, platform=platform, is_group=is_group
    )
    system_prompt += bridge.build_skills_block(eff_cfg)
    if bool(eff_cfg.get("emotion_enable", False)):  # P-B：情绪行注入
        system_prompt += "\n" + st.emotion.prompt_line(time.time())
    eco_block, eco_extras = await _eco_inject_block(P, event, trigger_text)
    if eco_block:
        system_prompt += f"\n\n{eco_block}"

    # 学习注入块（表达习惯选择 + 关键词反应 —— 格式对齐 MaiBot 原文）；
    # 黑话参考已移至 planner 每轮注入（对齐 jargon_context_matcher 位置）
    # M8：表达块选择走公共段（与 planner reply 同一实现）
    expr_block = await _select_expr_block(
        P, st, eff_cfg, platform, gid, provider, reason, is_group=is_group
    )
    keyword_block = learning.keyword_reaction_block(eff_cfg, trigger_text)

    user_message = prompt.build_final_user_message(
        st,
        eff_cfg,
        reason,
        style,
        expression_habits=expr_block,
        keyword_reaction=keyword_block,
    )
    if bool(eff_cfg.get("emotion_enable", False)):  # P-B：要求模型行首给情绪标签
        user_message += (
            # v6.25.0 词表 9→12（N9 补齐委屈/期待/安心的增量定义，标签-驱动
            # 闭环无死角；正则剥标签本就接受任意 1-6 字词，旧模型输出兼容）
            "\n\n【输出要求】请在正文最前面单独一行写 [情绪:愤怒/厌恶/恐惧/悲伤/平静/好奇/开心/兴奋/喜爱/委屈/期待/安心]，"
            "然后换行写正文；这一行会被系统剥离，不会发出。"
        )

    func_tool = bridge.build_chat_toolset(P.context, eff_cfg)
    if func_tool is not None:
        system_prompt += bridge.MAID_BRIDGE_PROMPT

    # 识图门控（v6.21.0）：replyer 不看 enable_image_context 开关，读 AstrBot
    # 模型条目 modalities「图像」勾选（勾了才附图）；两轮生成同传（坑 47 同款）
    image_parts = prompt.image_context_parts(
        st, eff_cfg, enabled=replyer_image_capable(P, eff_cfg)
    )

    _monitor_stage(P, gid, monitor.STAGE_REPLYER, "生成可见回复", agent_state="running")
    try:
        resp = await _task_text_chat(
            P,
            "replyer",
            eff_cfg,
            prompt=user_message,
            session_id=f"maisoul_{gid}",
            system_prompt=system_prompt,
            func_tool=func_tool,
            extra_user_content_parts=(eco_extras + image_parts) or None,
        )
    except Exception as e:
        P.monitor.emit_llm_error(
            session_id=gid,
            task_name="replyer",
            request_type="text_chat",
            model_name=str(getattr(provider, "id", "") or type(provider).__name__),
            message=str(e),
        )
        raise

    if getattr(resp, "tools_call_name", None):
        results = await bridge.exec_tool_calls(P.context, event, resp)
        if results:
            logger.info(f"maisoul[{gid}] 管家桥执行: {results[:120]}")
            user_message += (
                f"\n\n【管家执行结果】\n{results}\n\n"
                "请结合结果，用你自己的口吻输出给群友的发言内容。"
            )
        resp = await _task_text_chat(
            P,
            "replyer",
            eff_cfg,
            prompt=user_message,
            session_id=f"maisoul_{gid}",
            system_prompt=system_prompt,
            extra_user_content_parts=(eco_extras + image_parts) or None,
        )

    answer = _resp_text(resp)
    if not answer:
        logger.info(f"maisoul[{gid}] 模型未返回内容，放弃本次发言")
        return

    # WebUI 聊天页是单气泡（accumulator 替换语义）：攒段合并一次发；
    # 引用回复（MaiBot set_quote 默认 true）：首段挂 Reply(触发消息)。
    # 投递/记账/学习调度走 _deliver_reply 公共段（M8，与 planner reply 同源）
    webchat = str(event.get_platform_name() or "") == "webchat"
    quote_id = (
        _msg_id(event)
        if not webchat and eff_cfg.get("enable_reply_quote", True)
        else ""
    )

    async def _webchat_send(text: str) -> None:
        await event.send(MessageChain().message(text))

    sent = await _deliver_reply(
        P,
        st=st,
        eff_cfg=eff_cfg,
        gid=gid,
        umo=umo,
        event=event,
        provider=provider,
        platform=platform,
        answer=answer,
        msg_id=_msg_id(event),
        quote_id=quote_id,
        webchat_send=_webchat_send if webchat else None,
        gen_baseline=gen_baseline,
        is_group=is_group,
    )
    logger.info(f"maisoul[{gid}] 已发言 {len(sent)} 段（人格={pname}）")


# ------------------------------------------------------------------ #
# 生态注入桥（v6.11.0）：手动触发 on_llm_request/on_llm_response 钩子链，  #
# 心弦好感/记忆/世界书等注入型插件在麦麦管线内同样生效                       #


def _webchat_sender(P, event: AstrMessageEvent):
    """WebUI 聊天页发送通道：段落经 event.send 流进当前请求的气泡。"""

    async def _send(text: str) -> None:
        await event.send(MessageChain().message(text))

    return _send


def _schedule_learning(
    P, provider, eff_cfg, st, platform: str, gid: str, is_group: bool = True
):
    """发言后异步学习表达/黑话（对齐 MaiBot 学习器：失败不影响发言）。

    is_group：学习规则按 group/private 匹配（v6.20.3 贯通——此前漏传恒按
    group 匹配，私聊 learn=False 规则失效）。
    学习触发闸三件套（v6.28.0，对齐 runtime 学习调度）：① 会话级互斥——
    同会话上一批未完成不叠批（重复学习同一窗口虚增 count + 重复账单）；
    ② 30s 最小间隔——学习 LLM 不再与聊天 1:1 放大；③ ≥10 条可学外部消息。"""
    try:
        _, learn_expr = learning.learning_flags(
            eff_cfg, "expression_learning_list", platform, gid, is_group
        )
        _, learn_jargon = learning.learning_flags(
            eff_cfg, "jargon_learning_list", platform, gid, is_group
        )
        if not (learn_expr or learn_jargon):
            return
        now = time.time()
        if st.learn_busy:
            logger.debug(f"maisoul[{gid}] 学习跳过：同会话上一批未完成")
            return
        if now - st.last_learn_ts < learning.LEARN_MIN_INTERVAL_SECONDS:
            return
        snapshot_buf = list(st.buffer)[-30:]
        learnable = sum(1 for m in snapshot_buf if str(m.get("sid")) != "self")
        if learnable < learning.LEARN_MIN_MESSAGES:
            return
        st.learn_busy = True
        st.last_learn_ts = now
        snapshot_cfg = dict(eff_cfg)
        learn_bind = _pick_task_model(P, "learner", snapshot_cfg)

        async def _run():
            try:
                summary = await learning.learn_from_chat(
                    learn_bind[0] if learn_bind else provider,
                    snapshot_cfg,
                    snapshot_buf,
                    platform,
                    gid,
                    P.learning_store,
                    model=learn_bind[1] if learn_bind else None,
                    is_group=is_group,
                )
                if summary and summary != "学习未启用":
                    logger.info(f"maisoul[{gid}] 学习: {summary}")
            except Exception:
                logger.debug("maisoul: 学习任务失败", exc_info=True)
            finally:
                st.learn_busy = False

        P._spawn(_run(), name=f"learning:{gid}")
    except Exception:
        logger.debug("maisoul: 学习任务调度失败", exc_info=True)


async def _select_expr_block(
    P,
    st,
    eff_cfg,
    platform: str,
    gid: str,
    provider,
    reason: str,
    reply_reference: str = "",
    is_group: bool = True,
) -> str:
    """表达习惯选择块（M8 公共段：independent 与 planner reply 同一实现）。"""
    use_expr, _ = learning.learning_flags(
        eff_cfg, "expression_learning_list", platform, gid, is_group
    )
    if not use_expr:
        return ""
    observe = learning.build_chat_info(list(st.buffer))
    expr_bind = _pick_task_model(P, "expression_use", eff_cfg)
    emb = _embedding_provider(P, eff_cfg)
    return await learning.select_expression_habits_block(
        expr_bind[0] if expr_bind else provider,
        P.learning_store,
        learning.share_key(eff_cfg, "expression_groups", platform, gid),
        bool(eff_cfg.get("expression_checked_only", True)),
        observe,
        str(eff_cfg.get("bot_name") or "麦麦"),
        reason,
        model=expr_bind[1] if expr_bind else None,
        mode=str(eff_cfg.get("expression_selection_mode") or "legacy"),
        embedding=emb,
        embedding_model=(
            str((getattr(emb, "provider_config", None) or {}).get("id", "") or "")
            if emb is not None
            else ""
        ),
        query_text=learning.build_expression_query_text(
            reply_reason=reason, reply_reference=reply_reference
        ),
        pool_size=int(eff_cfg.get("expression_vector_candidate_pool_size", 50) or 50),
    )


async def _deliver_reply(
    P,
    *,
    st,
    eff_cfg,
    gid,
    umo,
    event,
    provider,
    platform,
    answer: str,
    msg_id: str,
    quote_id: str,
    webchat_send=None,
    eco_event=None,
    gen_baseline: float | None = None,
    is_group: bool = True,
) -> list[str]:
    """拟人发送 + 记账 + 学习调度（M8 公共段，两条生成路径的收尾）。

    webchat_send 非 None（WebUI 单气泡）：攒段合并一次发；否则逐段
    context.send_message，首段挂 Reply(quote_id)。返回实际发送的段。
    gen_baseline 为生成开始时刻（时间戳口径）；is_group 供学习规则匹配。
    """
    # P-D 发送队列降级：生成期间新到消息 >3 条或新文本 >200 字时，回复改为
    # 引用最新一条消息（默认关 send_queue_demotion；群聊非 webchat 才有意义）
    if gen_baseline is not None and webchat_send is None:
        demoted = demote_quote(list(st.buffer), eff_cfg, gen_baseline)
        if demoted is not None:
            quote_id, msg_id = demoted
    buf: list[str] = []
    quoted = {"done": False}

    async def send(text: str) -> None:
        if webchat_send is not None:
            buf.append(text)
            return
        if quote_id and not quoted["done"]:
            quoted["done"] = True
            await P.context.send_message(
                umo, MessageChain([Reply(id=quote_id)]).message(text)
            )
        else:
            await P.context.send_message(umo, MessageChain().message(text))
        _emit_sent(P, gid, text, msg_id, "reply", event)

    typing_mult = 1.0
    emo_word = ""
    if bool(eff_cfg.get("emotion_enable", False)):  # P-B：剥标签/更新情绪/打字乘数
        import re as _re
        import time as _time

        m = _re.match(r"^\s*\[情绪[:：]\s*([^\]\s]{1,6})\s*\]\s*\n?", answer or "")
        if m:
            answer = (answer or "")[m.end() :].lstrip("\n")
            st.emotion.apply(m.group(1), _time.time())
            emo_word = m.group(1)
        typing_mult = st.emotion.typing_multiplier()
    sent = await sender.send_humanlike(send, answer, eff_cfg, typing_mult=typing_mult)
    if not sent:
        # 空白回复（后处理总开关关时不兜底，v6.28.0）：无实际发言——不记
        # 防重复/存在感账、不触发情绪累积与学习，对齐 MaiBot 发送前中止
        return sent
    if emo_word and bool(
        eff_cfg.get("emotion_feedback_enable", False)
    ):  # §6.6 同向情绪累积：发送成功才计入——失败/取消的发言不算已表达的情绪
        st.emotion_feedback.observe(emo_word)
    if webchat_send is not None and buf:
        await webchat_send("\n\n".join(buf))
        for seg_text in buf:
            _emit_sent(P, gid, seg_text, msg_id, "reply", event)
    st.record_self_reply(
        msg_id,
        sent,
        str(eff_cfg.get("bot_name") or P.config["bot_name"]),
        quote=quote_id if quoted["done"] else "",
    )
    await _eco_fire_response(
        P, eco_event if eco_event is not None else event, "\n".join(sent)
    )
    _schedule_learning(P, provider, eff_cfg, st, platform, gid, is_group)
    return sent


# ------------------------------------------------------------------ #
# Planner 决策模式：maisaka agent 循环                                  #
