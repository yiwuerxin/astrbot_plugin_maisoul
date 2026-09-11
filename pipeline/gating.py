"""消息门控（M7 自 main.py 机械拆出，行为零变化）。

逃生舱/过滤词/会话键/提及判定/双模式分发/空窗补偿重查。
"""

from __future__ import annotations

import asyncio

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..core import mention, sanitize, trigger
from ..core.states import session_key
from .events_util import (
    _has_at_all,
    _has_at_bot,
    _is_reply_to_bot,
    _msg_id,
    _other_at_names,
    _record,
    _session_name,
)
from .planner_host import _schedule_planner
from .replyer import _generate_and_send, _webchat_sender


async def _process_chat(P, event: AstrMessageEvent, is_group: bool):
    if not P.config["enable"]:
        return

    # 全接收（坑 61）：waking_check 命中唤醒前缀时会原地剥掉前缀改写
    # message_str（"麦麦你胖了"→"你胖了"），门控/观察页/planner 上下文全都
    # 丢失说明对象——从消息链拼全量原文，不读 wake_prefix 配置（各部署
    # 唤醒词任意多个，同步配置必漏）；链上无文本回落 message_str。
    # 同时 At 文本化（对齐 MaiBot process_at_component）：适配器会把第一个
    # @bot 从 message_str 剔除，不文本化则 planner/replyer 全程看不到点名。
    raw_text = sanitize.full_plain_text(
        event.get_messages(),
        event.message_str,
        self_id=str(event.get_self_id() or ""),
        bot_name=str(P.config["bot_name"]),
    )
    # P-F 反注入清洗：引用前缀/合并转发占位不冒充本人发言；提及判定前
    # 剥掉指向其他 AI 的开头 @呼名（At 组件的 at_bot 强判定不受影响）
    text = sanitize.sanitize_text(raw_text)
    mention_text = sanitize.strip_leading_ai_mention(
        text,
        str(P.config["bot_name"]),
        [str(a) for a in (P.config.get("aliases") or [])],
    )
    logger.debug(
        f"maisoul: 收到消息 [{event.get_platform_name()}] "
        f"{event.get_sender_name()}: {text[:40]}"
    )

    # 逃生舱：指令 / 其他插件（Heartflow 等）已触发的恒放行。
    # v6.10.0 聊天全面接管：群聊 @/唤醒前缀不再放行原生路径，而是作为
    # at 级显式点名进入麦麦门控（escape_at_wake=true 恢复旧放行行为）。
    # 私聊不做唤醒放行——AstrBot 对私聊/webchat 恒置
    # is_at_or_wake_command=True，且 MaiBot 语义中私聊消息本身就全部进入管线。
    escape = text.startswith("/") or event.get_extra("heartflow_triggered")
    if not escape:
        # 指令双处理防线：AstrBot 指令不止 "/" 一种触发形态——私聊里裸指令名
        # （"reset new"）、群聊「唤醒名 + 指令」（"<唤醒名> reset new"，waking_check
        # 剥前缀后 CommandFilter 照样命中）都会激活指令 handler；指令执行后
        # 不 stop_event，maisoul 若不识别会把它再当聊天跑一轮（双响应）。
        # 框架在 filter 阶段已算好 activated_handlers：其中有指令类过滤器
        # （CommandFilter）即视为指令，放行
        try:
            from astrbot.core.star.filter.command import CommandFilter as _CF

            for _h in event.get_extra("activated_handlers") or []:
                if any(
                    isinstance(_f, _CF)
                    for _f in (getattr(_h, "event_filters", None) or [])
                ):
                    escape = True
                    break
        except Exception:
            # 降级：activated_handlers 结构不可达时按非指令处理（仅影响双响应防线）
            logger.debug("maisoul: 指令过滤器探测失败", exc_info=True)
    explicit = False
    if is_group and event.is_at_or_wake_command:
        if P.config.get("escape_at_wake"):
            escape = True
        else:
            explicit = True
    logger.debug(
        f"maisoul: escape={escape} explicit={explicit} "
        f"sender={event.get_sender_id()} self={event.get_self_id()}"
    )
    if escape:
        _record(P, event, text)
        return

    # 过滤词（对齐 [message_receive].ban_words/ban_msgs_regex：注册前整条丢弃，
    # 不进缓存不进门控；指令类消息（escape）不检查，同 MaiBot 只查非命令候选。
    # v6.20.3：补 stop_event——只 return 不拦传播时，@机器人的违禁消息会被
    # 原生 LLM 阶段照常回复，"整条丢弃"语义名存实亡）
    if trigger.hit_ban_filter(
        text, P.config.get("ban_words"), P.config.get("ban_msgs_regex")
    ):
        logger.debug(f"maisoul: 消息命中过滤词，已丢弃: {text[:30]}")
        event.stop_event()
        return

    if str(event.get_sender_id()) == str(event.get_self_id()):
        logger.debug("maisoul: 自发消息，跳过")
        event.stop_event()
        return

    gid = session_key(event)
    st = P.states.get(gid)
    if is_group:
        P._group_sessions.add(gid)
    # 麦麦观察：会话首次进入管线时上报会话标识（对齐 runtime 启动时的 session.start）
    if gid not in P._monitor_sessions:
        # M12 同族封顶：超限时清空重来（重发 session.start 无害——前端按
        # session_id 覆盖会话卡），防大量私聊用户的长期部署内存无界增长
        if len(P._monitor_sessions) > 4096:
            P._monitor_sessions.clear()
        P._monitor_sessions.add(gid)
        P.monitor.emit_session_start(
            gid,
            _session_name(P, gid),
            is_group_chat=is_group,
            group_id=gid if is_group else None,
            user_id=None if is_group else gid,
            platform=str(event.get_platform_name() or ""),
        )
    # 记录用全量原文：含引用渲染（[回复了X的消息: 原文]，对标 MaiBot
    # processed_plain_text 进会话历史）；评分/提及用清洗后 text 不变
    _record(P, event, raw_text, gid)
    logger.debug(f"maisoul[{gid}]: 记录完成 pending={st.pending_since_fire}")

    aliases = [str(a) for a in (P.config.get("aliases") or [])]
    bot_name = str(P.config["bot_name"])
    # 提及判定走 mention.is_mentioned（前边界匹配 + 剥适配器渲染的 @他人
    # token + At 段他人昵称排除）——@小麦麦不再被"麦麦"子串误命中
    mentioned = mention.is_mentioned(
        mention_text, bot_name, aliases, exclude_names=_other_at_names(P, event)
    )
    if not mentioned:
        # 回复引用机器人 = 提及（对齐 is_mentioned_bot_in_message 第 6 层：
        # 回复引用算 mention 不算 at；批次内任一命中即算——扫当前消息+未消费积压）
        mentioned = _is_reply_to_bot(P, event) or any(
            m.get("reply_bot")
            for m in st.buffer
            if st.last_fire_ts and float(m.get("ts") or 0) > st.last_fire_ts
        )
    # 显式召唤（@ 或唤醒前缀）与 At 段同级——但框架 waking_check 对 @全体
    # 成员/引用回复也统一置唤醒标志，整体并入 at 档会把这两类升级成强制
    # 必回、绕过 mentioned_bot_reply 的默认关，故经 effective_at_bot 降级
    at_bot = mention.effective_at_bot(
        _has_at_bot(P, event),
        explicit,
        _has_at_all(P, event),
        _is_reply_to_bot(P, event),
    )
    fired, detail, nec = trigger.should_trigger(
        st,
        P.config,
        at_bot=at_bot,
        mentioned=mentioned,
        text=text,
        aliases=aliases,
        bot_name=bot_name,
        platform=str(event.get_platform_name() or ""),
        chat_id=gid,
        is_group=is_group,
    )
    logger.debug(f"maisoul[{gid}] {detail}")

    if not fired:
        _maybe_defer_recheck(P, event, st, gid, is_group)
        event.stop_event()
        return

    style = nec.style if nec else ""
    logger.info(f"maisoul[{gid}] 触发发言（{detail}）")
    st.cancel_defer()
    if P.config["mode"] == "native":
        # 协作模式：交给原生 agent，生态插件的注入与出站美化生效
        st.mark_fire(_msg_id(event))
        event.is_at_or_wake_command = True
        event.set_extra("maisoul_triggered", True)
        return

    if P.config["mode"] == "planner":
        # 决策模式：进入 maisaka Planner（调度语义对齐 turn_scheduler/runtime）
        forced = (at_bot and P.config.get("inevitable_at_reply", True)) or (
            mentioned and P.config.get("mentioned_bot_reply", False)
        )
        # WebUI 聊天页同步跑完整决策：段落经 event.send 流进当前请求气泡。
        # 后台任务方式下聊天 API 会在事件结束时关流，回复只能落 proactive 存库，
        # 页面上只剩一条秒回的空气泡（_has_send_oper 会让原生 LLM 阶段自动跳过）。
        webchat = str(event.get_platform_name() or "") == "webchat"
        await _schedule_planner(
            P,
            event,
            st,
            gid,
            forced,
            is_group,
            send_fn=_webchat_sender(P, event) if webchat else None,
        )
        event.stop_event()
        return

    # 独立模式：麦麦流水线自管生成与发送
    if st.firing:
        return
    st.firing = True
    try:
        await _generate_and_send(P, event, st, detail, style, text, is_group)
    except Exception:
        logger.error("maisoul 生成回复失败", exc_info=True)
    finally:
        st.firing = False
    event.stop_event()


# ------------------------------------------------------------------ #
# 空窗补偿到点重查（对齐 runtime._defer_message_turn_check）              #
# ------------------------------------------------------------------ #
# ------------------------------------------------------------------ #
# 任务级模型绑定（对齐 MaiBot model_task_config：多模型 + 选择策略；        #
# 纯逻辑在 core/modelbind.py，此处只做 provider 解析与调用）               #


def _maybe_defer_recheck(P, event: AstrMessageEvent, st, gid: str, is_group: bool):
    """frequency 门未触发：按 MaiBot delay 公式安排到点重查。

    无新消息也会到点重评空窗补偿——安静群中等够平均间隔 × 差额后主动开口。
    webchat（请求结束即关流）与协作模式（需活跃管线）不适用。
    """
    if str(P.config.get("reply_trigger_mode") or "frequency") != "frequency":
        return
    platform = str(event.get_platform_name() or "")
    if platform == "webchat" or P.config["mode"] == "native":
        return
    pl = st.planner_state()
    if pl.agent_state in ("running", "wait") or st.firing:
        return
    threshold = trigger.message_trigger_threshold(
        "frequency",
        trigger.effective_talk_value(P.config, platform, gid, is_group=is_group),
    )
    delay = trigger.frequency_recheck_delay(st, st.pending_since_fire, threshold)
    if delay is None:
        return
    st.cancel_defer()

    async def _recheck():
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        st.defer_task = None
        pl = st.planner_state()
        if pl.agent_state in ("running", "wait") or st.firing:
            return
        fired, detail, _ = trigger.should_trigger(
            st,
            P.config,
            at_bot=False,
            mentioned=False,
            text="",
            aliases=[],
            bot_name=str(P.config["bot_name"]),
            platform=platform,
            chat_id=gid,
            is_group=is_group,
        )
        if not fired:
            logger.debug(f"maisoul[{gid}] 空窗到点重查未达标：{detail}")
            return
        logger.info(f"maisoul[{gid}] 空窗补偿到点触发（{detail}）")
        if P.config["mode"] == "planner":
            await _schedule_planner(P, event, st, gid, False, is_group)
        else:
            # 与 _process_chat 独立模式同构的 firing 闸：到点重查与新消息
            # 触发撞车时只跑一个，防同群双回复
            if st.firing:
                return
            st.firing = True
            try:
                await _generate_and_send(P, event, st, detail, "", "", is_group)
            except Exception:
                logger.error("maisoul 空窗补偿生成回复失败", exc_info=True)
            finally:
                st.firing = False

    st.defer_task = P._registry.spawn(_recheck(), name=f"defer_recheck:{gid}")
    logger.debug(f"maisoul[{gid}] 空窗补偿重查已排期：{delay:.1f}s 后重评")


# ------------------------------------------------------------------ #
# 独立模式：生成与拟人发送（含管家桥）                                   #
