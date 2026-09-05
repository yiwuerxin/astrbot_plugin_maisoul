"""Planner 决策循环宿主（M7 自 main.py 机械拆出，行为零变化）。"""

from __future__ import annotations

import asyncio
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..core import bridge, learning, monitor, personas, planner, prompt, sender, trigger
from ..core.constants import MESSAGE_DEBOUNCE_SECONDS
try:
    from astrbot.api.event import MessageChain
except ImportError:
    from astrbot.core.message.message_event_result import MessageChain
from astrbot.api.message_components import Reply

from .ecobridge import (_eco_fire_response, _eco_inject_block, _extra_part_text,
                         xinxian_profile_block as _xinxian_profile_block)
from .events_util import _emit_sent, _monitor_stage, _resp_text
from .modelbind_host import _embedding_provider, _pick_task_model, _task_text_chat
from .replyer import _deliver_reply, _schedule_learning, _select_expr_block


def _schedule_planner(P, event: AstrMessageEvent, st, gid: str, forced: bool,
                      is_group: bool = True, send_fn=None):
    """对齐 turn_scheduler.schedule_message_turn 的调度语义。

    群聊 wait 期间新消息不唤醒；私聊 wait 期间收到新消息则结束等待进入
    Planner（MaiBot 原文行为）；空闲退避仅群聊生效。
    send_fn 非空时（WebUI 聊天页）返回待 await 的协程；其余分支一律返回
    即完的空协程（asyncio.sleep(0)）——调用方统一 await，裸 return None
    会让调用方 `await None` 抛 TypeError，进而漏掉 stop_event、事件漏进
    原生管线触发 outputpro 报错拦截（坑 48）。
    """
    import asyncio
    done = asyncio.sleep(0)
    pl = st.planner_state()
    cfg = P.config

    if is_group and pl.should_delay(cfg, st.pending_since_fire):
        logger.debug(f"maisoul[{gid}] planner: 空闲退避中，延迟处理")
        return done
    if pl.agent_state == "wait":
        if (not is_group) and not trigger.effective_talk_value(
                cfg, str(event.get_platform_name() or ""), gid, is_group=is_group) <= 0:
            logger.info(f"maisoul[{gid}] planner: 私聊 wait 期间收到新消息，结束等待并进入 Planner")
            pl.resume_from_wait()
        elif forced and pl.resume_from_wait():
            logger.info(f"maisoul[{gid}] planner: 主动触发从 wait 恢复")
        else:
            return done
    if pl.agent_state == "running":
        max_interrupt = int(cfg.get("planner_interrupt_max_consecutive_count", 0))
        if max_interrupt > 0 and pl.interrupt_count < max_interrupt and pl.running_task:
            pl.interrupt_count += 1
            logger.info(f"maisoul[{gid}] planner: 新消息打断思考（连续打断 "
                        f"{pl.interrupt_count}/{max_interrupt}）")
            pl.running_task.cancel()
            # 打断后由本条消息重新发起一轮
        else:
            return done  # 消息留待当前循环的后续轮次处理

    gen = pl.begin_cycle()  # M3：新循环换代——被打断的旧循环退出不得回写状态
    pl.agent_state = "running"
    pl.interrupt_count = 0
    umo = event.unified_msg_origin
    platform = str(event.get_platform_name() or "")
    pl.umo, pl.platform = umo, platform
    pl.is_group = is_group
    cycle_id = P._cycle_counter.get(gid, 0) + 1
    P._cycle_counter[gid] = cycle_id
    pl.last_event = event  # wait 续轮/定时循环复用同一真实 event（候选/注入都挂在 event 上）
    logger.info(f"maisoul[{gid}] planner: 启动决策循环")
    _monitor_stage(P, gid, monitor.STAGE_LOOP_START, f"循环 {cycle_id}",
                        agent_state=pl.agent_state)
    if send_fn is not None:
        done.close()  # webchat 分支返回完整循环协程；预建的空协程关闭，防未 await 告警
        return _planner_cycle(P, umo, platform, gid, st, is_group, send_fn=send_fn,
                                  event=event, gen=gen)
    pl.running_task = P._registry.spawn(
        _planner_cycle(P, umo, platform, gid, st, is_group, event=event, gen=gen),
        name=f"planner:{gid}")
    return done

def _drain_pending(P, st, pl) -> list[dict]:
    pending = [m for m in list(st.buffer) if float(m.get("ts") or 0) > pl.last_cycle_ts
               and str(m.get("sid")) != "self"]
    pl.last_cycle_ts = time.time()
    st.pending_since_fire = 0
    return pending

class _PlannerHostAdapter:
    """PlannerHost 适配器（M9）：planner 回调 → 本模块函数，P 经构造携带。"""

    def __init__(self, P):
        self._P = P

    async def planner_execute_reply(self, deps, reason: str, args: dict) -> str:
        return await _planner_execute_reply(self._P, deps, reason, args)

    def planner_schedule_wait_resume(self, st, cfg, gid: str, seconds: int) -> None:
        _schedule_wait_resume(self._P, st, cfg, gid, seconds)

    async def planner_send_emoji(self, deps) -> str:
        return await _planner_send_emoji(self._P, deps)


async def _planner_cycle(P, umo: str, platform: str, gid: str, st,
                         is_group: bool = True, send_fn=None,
                         initial_feedback: str = "",
                         event: AstrMessageEvent | None = None,
                         gen: int = 0):
    """一轮 Planner：最多 MAX_INTERNAL_ROUNDS 轮工具循环（对齐 reasoning_engine）。

    initial_feedback：wait 到期续轮时的完成回执（对齐 wait 完成工具结果消息），
    前置注入首轮 user 内容。
    """
    pl = st.planner_state()
    # 事件解析链：本轮入参 > PlannerState.last_event（wait 续轮复用）> 每周期一个合成事件。
    # 必须整周期同一对象——search_meme/send_meme 的候选挂在 event._emoji_turn_state 上，
    # 逐调用现造 SyntheticEvent 会让候选在下一步就消失（candidate_expired）。
    event = event or getattr(pl, "last_event", None)
    if event is None:
        event = bridge.SyntheticEvent(umo, send_message=P.context.send_message)
    else:
        pl.last_event = event
    cycle_id = P._cycle_counter.get(gid, 0)
    logger.debug(f"maisoul planner cycle[{gid}]: 开始执行")
    # 麦麦观察：本轮聚合数据（对齐 emit_planner_finalized 的输入结构）
    planner_llm_ms = 0.0
    planner_prompt_tokens = 0
    planner_completion_tokens = 0
    tool_count_total = [4]  # 可见内置工具数（reply/wait/send_emoji/tool_search）+ 已发现 deferred
    planner_content: str | None = None
    planner_calls: list[dict] = []
    request_messages: list[dict] | None = None
    history_count = 0
    tool_records: list[dict] = []
    pl.eco_injection = ""
    end_reason, end_detail = "", ""
    interrupted = False

    def finalize(reason: str, detail: str = "") -> None:
        P.monitor.emit_planner_finalized(
            session_id=gid, cycle_id=cycle_id,
            planner_request_messages=request_messages,
            planner_selected_history_count=history_count or None,
            planner_tool_count=tool_count_total[0],
            planner_content=planner_content,
            planner_tool_calls=planner_calls or None,
            planner_prompt_tokens=planner_prompt_tokens or None,
            planner_completion_tokens=planner_completion_tokens or None,
            planner_total_tokens=(planner_prompt_tokens + planner_completion_tokens) or None,
            planner_duration_ms=planner_llm_ms or None,
            tools=tool_records,
            time_records={},
            agent_state=pl.agent_state,
            planner_interrupted=interrupted,
            end_reason=reason, end_detail=detail,
            eco_injection=pl.eco_injection,
            planner_system_prompt=system_prompt)

    try:
        # 消息去抖：等最后一条外部消息静默 ≥1s 再开轮（对齐
        # _wait_for_message_quiet_period，连发消息自然并成一轮；新消息
        # 会顺延 last_ext_ts，循环继续等）。webchat 同步路径同样适用。
        while True:
            remaining = MESSAGE_DEBOUNCE_SECONDS - (time.time() - (st.last_ext_ts or 0.0))
            if remaining <= 0:
                break
            await asyncio.sleep(min(remaining, MESSAGE_DEBOUNCE_SECONDS))
        provider = P.context.get_using_provider()
        if provider is None:
            logger.warning("maisoul: 未配置可用的模型 Provider，planner 跳过")
            pl.set_idle_if_current(gen)
            finalize("no_provider")
            return
        logger.debug(f"maisoul planner[{gid}]: resolve 人格前")
        eff_cfg, pname = await personas.resolve_active(P.context, P.config, gid, umo)
        st.last_persona = pname
        logger.debug(f"maisoul planner[{gid}]: 人格={pname}，构建工具集")
        deps = planner.PlannerDeps(_PlannerHostAdapter(P), st, eff_cfg, event,
                                   platform, gid, is_group, send_fn=send_fn)
        deps.umo = umo
        deps.deferred_pool = bridge.list_deferred_tools(P.context, eff_cfg)
        tool_count_total[0] = 4 + len(deps.deferred_pool)
        # 系统提示词的注意事项只放通用项（对齐 _build_group_chat_attention_block）；
        # chat_prompts 命中改为请求末尾的独立 user 消息（坑 53）
        system_prompt = planner.build_planner_system(
            eff_cfg, prompt.build_attention_block(eff_cfg, gid, platform, is_group,
                                                  include_chat_prompt=False))
        attention_tail_msg = prompt.chat_attention_tail(eff_cfg, gid, platform, is_group)
        # 对齐 MaiBot：chat_history 作为上下文消息传给 planner（非本轮 pending 的历史）
        context_key = "max_context_size" if is_group else "max_private_context_size"
        # 2× KV cache 稳定窗（对齐 CONTEXT_SELECTION_CACHE_STABILITY_RATIO=2.0：
        # 选取窗口放大一倍，避免逐条追加导致前缀缓存失效）
        base_limit = int(eff_cfg.get(context_key, 40 if is_group else 60))
        context_limit = max(base_limit, base_limit * 2)
        all_buf = list(st.buffer)
        pending_now = [m for m in all_buf
                       if float(m.get("ts") or 0) > pl.last_cycle_ts
                       and str(m.get("sid")) != "self"]
        # 历史段 = 聊天记录 + 历史 planner 分析按时间交错（对齐 MaiBot 会话
        # 历史：全部聊天消息含自发消息进 user 轮 <message> 前缀，分析作为
        # assistant 轮回灌，输出格式由此自我强化，坑 52/53）；窗口在合并流
        # 上截取。pending 用对象身份排除（m not in pending_now 是逐条 dict
        # 值相等比较，O(n²)）
        pending_ids = {id(m) for m in pending_now}
        contexts, history_msgs = planner.build_history_contexts(
            [m for m in all_buf if id(m) not in pending_ids],
            pl.analysis_log, context_limit, is_group)
        history_count = len(history_msgs)
        # P-A 中期记忆：窗口裁掉的旧消息后台摘要为 {summary, cues} 存会话
        # 记忆（memory_enable 默认关；任务经注册表，失败静默）
        if bool(eff_cfg.get("memory_enable", False)):
            _fold_memory(P, st, [m for m in all_buf if id(m) not in pending_ids][
                :max(0, len(all_buf) - context_limit)])
        # 黑话参考（对齐 _refresh_jargon_reference_message：planner 侧每轮
        # 机械匹配刷新，已注入词条轮间去重；replyer 侧不再注入）
        use_jargon, _ = learning.learning_flags(
            eff_cfg, "jargon_learning_list", platform, gid, is_group)
        jargon_key = learning.share_key(eff_cfg, "jargon_groups", platform, gid)
        jargon_recent = [m.get("text") or "" for m in list(st.buffer)[-context_limit:]]
        injected_jargons: set[str] = set()
        # wait 回执等一次性前置（跨轮重建后无配对的 tool_use，维持 user 文本轮，
        # 对齐差异记录于坑 55）
        tool_feedback = initial_feedback
        turn_start = len(contexts)  # 本轮循环产生的消息起点（折叠用）

        for round_index in range(planner.MAX_INTERNAL_ROUNDS):
            planner.fold_old_turns(contexts, turn_start)
            pending = _drain_pending(P, st, pl)
            # contexts 尾部是工具结果轮（role=tool）时视为有待续轮（对齐
            # tool_continue：工具结果回填后不要求新消息即继续）
            tail_is_tool = bool(contexts) and contexts[-1].get("role") == "tool"
            if not pending and round_index > 0 and not tool_feedback and not tail_is_tool:
                end_reason = "no_new_message"
                break  # 无新消息且无待回填的工具结果 → 本轮结束
            if pending:
                _monitor_stage(P, gid, monitor.STAGE_MESSAGE_INTAKE,
                                    f"待处理消息 {len(pending)} 条",
                                    agent_state=pl.agent_state)
            # 工具回执/新消息/黑话参考各进独立 user 轮（对齐部署版请求结构：
            # 历史末尾依次是消息 → ReferenceMessage → 注入 → 时间 → 注意事项）
            if tool_feedback:
                contexts.append({"role": "user", "content": tool_feedback})
                tool_feedback = ""
            for m in pending:
                contexts.append({"role": "user",
                                 "content": planner.render_planner_message(m, is_group)})
            if use_jargon:
                new_terms: list[str] = []
                jargon_block = learning.jargon_reference_block(
                    P.learning_store, jargon_key, jargon_recent,
                    exclude=injected_jargons or None, matched_out=new_terms)
                if jargon_block:
                    injected_jargons.update(new_terms)
                    contexts.append({"role": "user", "content": jargon_block})
            round_text = f"第 {round_index + 1} 轮"
            _monitor_stage(P, gid, monitor.STAGE_PLANNER, "组织上下文并请求模型",
                                round_text=round_text, agent_state=pl.agent_state)
            # 每轮重建工具集：可见内置 + 已发现的 deferred（对齐 _build_action_tool_definitions）
            tools = planner.build_planner_toolset(deps)
            for item in deps.deferred_pool:
                if item["name"] in pl.discovered_tools:
                    tools.add_tool(item["tool"])
            # 尾部注入（对齐 _build_request_messages 的 final_user_messages：
            # 注入 → 当前时间 → 聊天专属注意事项；末尾提醒作最终 prompt）。
            # 只进本次请求、轮毕即撤，不留在 contexts 历史
            image_parts = prompt.image_context_parts(st, eff_cfg)
            tail_msgs = [x for x in (
                planner.build_deferred_reminder(deps.deferred_pool, pl.discovered_tools),
                (f"（本次请求附带最近 {len(image_parts)} 张聊天图片，"
                 "对应聊天记录中的图片占位）" if image_parts else "")) if x]
            tail_msgs.append(time.strftime("时间：%Y-%m-%d %H:%M:%S"))
            if attention_tail_msg:
                tail_msgs.append(attention_tail_msg)
            final_reminder = planner.PLANNER_FINAL_USER_REMINDER.format(
                bot_name=str(eff_cfg.get("bot_name") or "").strip() or "麦麦")
            tail_base = len(contexts)
            contexts.extend({"role": "user", "content": t} for t in tail_msgs)
            request_messages = list(contexts)
            logger.debug(f"maisoul planner[{gid}]: 发起 LLM 请求"
                         f"（第{round_index + 1}轮，contexts {len(contexts)} 条）")
            planner_bind = _pick_task_model(P, "planner", eff_cfg)
            if planner_bind is not None:
                logger.debug(f"maisoul planner[{gid}]: 任务模型 "
                             f"{planner_bind[1]}@{getattr(planner_bind[0], 'provider_config', {}).get('id', '?')}")
            llm_started = time.time()
            try:
                resp = await _task_text_chat(P, 
                    "planner", eff_cfg,
                    prompt=final_reminder,
                    session_id=f"maisoul_planner_{gid}",
                    system_prompt=system_prompt,
                    func_tool=tools,
                    contexts=contexts or None,
                    extra_user_content_parts=image_parts or None,
                )
            except Exception as e:
                P.monitor.emit_llm_error(
                    session_id=gid, task_name="planner", request_type="text_chat",
                    model_name=str(getattr(provider, "id", "") or type(provider).__name__),
                    message=str(e))
                raise
            finally:
                del contexts[tail_base:]  # 撤销尾部注入（不进历史）
            planner_llm_ms += (time.time() - llm_started) * 1000
            # token 用量累计（LLMResponse.usage：input_other+input_cached=输入，output=输出）
            usage = getattr(resp, "usage", None)
            if usage is not None:
                planner_prompt_tokens += (int(getattr(usage, "input_other", 0) or 0)
                                          + int(getattr(usage, "input_cached", 0) or 0))
                planner_completion_tokens += int(getattr(usage, "output", 0) or 0)
            logger.info(f"maisoul planner[{gid}]: LLM 返回 tools={list(getattr(resp, 'tools_call_name', None) or [])}")
            analysis = _resp_text(resp)
            reasoning = str(getattr(resp, "reasoning_content", None) or "").strip()
            visible_analysis = analysis  # 可见正文（回灌唯一来源，坑 54）
            if not analysis and reasoning:
                # Claude 等模型在工具调用轮不输出正文，把分析放进 thinking 块；
                # 展示取并集（坑 51），但回灌只认可见正文（坑 54）
                analysis = reasoning
            if analysis and pl.last_analysis:
                # 防复读（对齐 _should_replace_reasoning：与上一轮思考相似度>0.9
                # 时替换为固定反思文本，逼模型重新审视局面）。替换同样作用于
                # 回灌文本——MaiBot 的 replace_output_projection 直接改写
                # output_items 后才写历史，替换文本才是落库值（Sourcery 审查）
                from difflib import SequenceMatcher
                if SequenceMatcher(None, analysis, pl.last_analysis).ratio() > 0.9:
                    logger.info(f"maisoul planner[{gid}]: 本轮思考与上轮过相似，替换为反思提示")
                    analysis = planner.PLANNER_REFLECT_ON_REPEAT
                    if visible_analysis:
                        visible_analysis = analysis
            if analysis:
                pl.last_analysis = analysis
            if visible_analysis:
                # 回灌只记可见正文（对齐 MaiBot：思考文本不重发请求——
                # ReasoningItem 回灌恒空，纯思考轮不产生 few-shot 示例；
                # 思考文本回灌会把输出语言带偏，坑 54）
                pl.analysis_log.append({"ts": time.time(), "text": visible_analysis})
            logger.debug(f"maisoul planner[{gid}]: 正文 {len(_resp_text(resp))} 字 / "
                         f"思考 {len(reasoning)} 字 / 工具 "
                         f"{len(getattr(resp, 'tools_call_name', None) or [])} 个")
            deps.latest_reason = analysis
            planner_content = analysis

            names = list(getattr(resp, "tools_call_name", None) or [])
            args_list = list(getattr(resp, "tools_call_args", None) or [])
            planner_calls = [
                {"id": f"{cycle_id}-{round_index}-{i}", "name": str(n),
                 "arguments": (args_list[i] if i < len(args_list)
                               and isinstance(args_list[i], dict) else {})}
                for i, n in enumerate(names)]
            # 对齐 MaiBot 输出项粒度：工具轮的 assistant 轮始终存在（带
            # tool_calls，正文块仅在可见正文非空时；思考块不重发，坑 54）。
            # 旧版工具结果走纯文本 user 轮，模型看不见自己调过工具——冷启动
            # 可见正文引导失败的结构根因（坑 55）
            if names or visible_analysis:
                assistant_turn: dict = {"role": "assistant",
                                        "content": visible_analysis}
                if names:
                    assistant_turn["tool_calls"] = [
                        {"id": c["id"], "type": "function",
                         "function": {"name": c["name"],
                                      "arguments": c["arguments"]}}
                        for c in planner_calls]
                contexts.append(assistant_turn)
            if not names:
                if is_group:
                    pl.record_idle_cycle(eff_cfg)
                logger.info(f"maisoul[{gid}] planner: 无动作结束"
                            + (f"（模型陈述：{analysis[:120]}）" if analysis else "（无输出）"))
                pl.set_idle_if_current(gen)
                _monitor_stage(P, gid, monitor.STAGE_WAITING, "本轮处理结束",
                                    agent_state=pl.agent_state)
                finalize("no_action", (analysis or "")[:120])
                return
            for i, name in enumerate(names):
                args = args_list[i] if i < len(args_list) and isinstance(args_list[i], dict) else {}
                _monitor_stage(P, gid, monitor.STAGE_TOOL_PREFIX + str(name),
                                    f"第 {i + 1}/{len(names)} 个工具",
                                    round_text=round_text, agent_state=pl.agent_state)
                tool_started = time.time()
                if name == "reply":
                    result = await deps.on_reply(args)
                    tool_records.append({
                        "tool_call_id": f"{cycle_id}-{round_index}-{i}",
                        "tool_name": "reply", "tool_args": args,
                        "tool_call_source": "planner", "tool_call_source_label": "",
                        "success": "未发言" not in str(result),
                        "duration_ms": (time.time() - tool_started) * 1000,
                        "summary": str(result)[:2000]})
                    logger.info(f"maisoul[{gid}] planner reply: {result[:80]}")
                    pl.reset_backoff()
                    pl.consecutive_wait_count = 0
                    # 对齐 MaiBot：reply 正常执行后不暂停循环（tool_continue，
                    # reasoning_engine 只在未生成可见消息时告警后继续）——
                    # 模型下一轮做收尾分析（通常无工具结束），该轮产出的
                    # 可见中文正文进回灌，是格式锁定的来源（坑 55）
                    contexts.append({"role": "tool",
                                     "tool_call_id": f"{cycle_id}-{round_index}-{i}",
                                     "content": str(result)})
                    continue
                if name == "wait":
                    message = deps.on_wait(args)
                    tool_records.append({
                        "tool_call_id": f"{cycle_id}-{round_index}-{i}",
                        "tool_name": "wait", "tool_args": args,
                        "tool_call_source": "planner", "tool_call_source_label": "",
                        "success": True,
                        "duration_ms": (time.time() - tool_started) * 1000,
                        "summary": str(message)[:2000]})
                    logger.info(f"maisoul[{gid}] planner wait: {message}")
                    if "休息" in message:
                        pl.record_idle_cycle(eff_cfg)
                        pl.set_idle_if_current(gen)
                    else:
                        # wait 到期必续轮（坑 26）：调度到期回执再跑一轮——
                        # 缺失会让会话挂在 wait 直到下一条消息才动
                        _schedule_wait_resume(P, 
                            st, eff_cfg, gid,
                            max(0, int(args.get("seconds", 0) or 0)))
                    _monitor_stage(P, gid, monitor.STAGE_WAITING, "本轮处理结束",
                                        agent_state=pl.agent_state)
                    finalize("wait", str(message)[:120])
                    return
                if name == "send_emoji":
                    result = await deps.on_send_emoji()
                    tool_records.append({
                        "tool_call_id": f"{cycle_id}-{round_index}-{i}",
                        "tool_name": "send_emoji", "tool_args": args,
                        "tool_call_source": "planner", "tool_call_source_label": "",
                        "success": "失败" not in str(result) and "不存在" not in str(result),
                        "duration_ms": (time.time() - tool_started) * 1000,
                        "summary": str(result)[:2000]})
                    # 工具结果进 tool 轮（anthropic 源转 tool_result 块并合并
                    # 连续 tool 轮，对齐 MaiBot 的 ToolResultMessage，坑 55）
                    contexts.append({"role": "tool",
                                     "tool_call_id": f"{cycle_id}-{round_index}-{i}",
                                     "content": str(result)})
                    continue
                if name == "tool_search":
                    result = deps.on_tool_search(args)
                    tool_records.append({
                        "tool_call_id": f"{cycle_id}-{round_index}-{i}",
                        "tool_name": "tool_search", "tool_args": args,
                        "tool_call_source": "planner", "tool_call_source_label": "",
                        "success": "未找到" not in str(result),
                        "duration_ms": (time.time() - tool_started) * 1000,
                        "summary": str(result)[:2000]})
                    logger.info(f"maisoul[{gid}] planner tool_search: {result[:80]}")
                    contexts.append({"role": "tool",
                                     "tool_call_id": f"{cycle_id}-{round_index}-{i}",
                                     "content": str(result)})
                    continue
                deferred_item = next(
                    (it for it in deps.deferred_pool if it["name"] == name), None)
                if deferred_item is not None and name in pl.discovered_tools:
                    ev = deps.event or bridge.SyntheticEvent(
                        umo, send_message=P.context.send_message)
                    try:
                        result = await bridge.call_llm_tool(
                            P.context, ev, deferred_item["tool"], args)
                    except Exception as e:
                        result = f"执行失败 {e}"
                    tool_records.append({
                        "tool_call_id": f"{cycle_id}-{round_index}-{i}",
                        "tool_name": str(name), "tool_args": args,
                        "tool_call_source": "planner", "tool_call_source_label": "deferred",
                        "success": "失败" not in str(result),
                        "duration_ms": (time.time() - tool_started) * 1000,
                        "summary": str(result)[:2000]})
                    logger.info(f"maisoul[{gid}] planner deferred {name}: {str(result)[:80]}")
                    contexts.append({"role": "tool",
                                     "tool_call_id": f"{cycle_id}-{round_index}-{i}",
                                     "content": str(result)})
                    continue
                tool_records.append({
                    "tool_call_id": f"{cycle_id}-{round_index}-{i}",
                    "tool_name": str(name), "tool_args": args,
                    "tool_call_source": "planner", "tool_call_source_label": "",
                    "success": False, "duration_ms": (time.time() - tool_started) * 1000,
                    "summary": "未知工具"})
                contexts.append({
                    "role": "tool",
                    "tool_call_id": f"{cycle_id}-{round_index}-{i}",
                    "content": f"未知工具（若是 deferred 工具，请先调用 tool_search 发现它）"})
        pl.set_idle_if_current(gen)
        _monitor_stage(P, gid, monitor.STAGE_WAITING, "本轮处理结束",
                            agent_state=pl.agent_state)
        finalize(end_reason or "max_rounds")
    except asyncio.CancelledError:
        interrupted = True
        _monitor_stage(P, gid, monitor.STAGE_PLANNER_INTERRUPTED,
                            "收到外部中断信号", agent_state=pl.agent_state)
        finalize("interrupted")
        pl.set_idle_if_current(gen)
        raise
    except Exception as e:
        _monitor_stage(P, gid, monitor.STAGE_ERROR, str(e)[:80],
                            agent_state=pl.agent_state)
        finalize("error", str(e)[:120])
        logger.error("maisoul planner 循环异常", exc_info=True)
        pl.set_idle_if_current(gen)

async def _summarize_folding_task(P, st, chat_log: str, ts: float) -> None:
    from ..core import memstore as _ms

    try:
        bind = _pick_task_model(P, "summarizer", P.config)
        provider = bind[0] if bind else P.context.get_using_provider()
        if provider is None:
            return
        resp = await _task_text_chat(
            P, "summarizer", P.config,
            prompt=_ms.SUMMARIZE_PROMPT.format(chat_log=chat_log[:2000]))
        parsed = _ms.parse_summary(_resp_text(resp))
        if parsed:
            st.memory.add(parsed[0], parsed[1], ts)
    except Exception:
        logger.debug("maisoul: 中期记忆摘要失败（静默）", exc_info=True)


def _fold_memory(P, st, dropped: list) -> None:
    """窗口外旧消息 ≥4 条且距上次折叠 ≥5 分钟 → 后台摘要一轮。"""
    import time as _time
    now = _time.time()
    msgs = [m for m in (dropped or []) if str(m.get("sid") or "") != "self"
            and str(m.get("text") or "").strip()]
    if len(msgs) < 4 or now - getattr(st.memory, "last_fold_ts", 0.0) < 300:
        return
    st.memory.last_fold_ts = now
    newest_ts = max(float(m.get("ts") or 0) for m in msgs)
    chat_log = "\n".join(f"{m.get('name')}: {m.get('text')}" for m in msgs[-30:])
    P._spawn(_summarize_folding_task(P, st, chat_log, newest_ts),
             name=f"memory_fold:{getattr(st, 'gid', '')}")


def _schedule_wait_resume(P, st, cfg, gid: str, seconds: int):
    """wait 到期：必续一轮并注入完成回执（对齐 timeout 触发 + _build_wait_completed_message）。

    旧写法只在 pending>0 时续轮——MaiBot 的 timeout 触发无论有无新消息都会
    带「等待已超时…请基于现有上下文继续下一轮思考」回执续轮，让模型消化
    等待期间的工具结果（如管家异步任务）后再决定动作。
    """
    import asyncio

    async def _resume():
        armed_at = time.time()
        await asyncio.sleep(max(0, seconds))
        pl = st.planner_state()
        if pl.agent_state == "wait" and time.time() >= pl.wait_until:
            elapsed = time.time() - armed_at
            has_new = st.pending_since_fire > 0
            receipt = planner.build_wait_completed_message(elapsed, seconds, has_new)
            gen = pl.begin_cycle()  # M3：续轮换代，旧代退出不得回写
            pl.agent_state = "running"
            await _planner_cycle(P, getattr(pl, "umo", ""), getattr(pl, "platform", ""),
                                      gid, st, getattr(pl, "is_group", True),
                                      initial_feedback=receipt, gen=gen)

    try:
        return P._spawn(_resume(), name=f"wait_resume:{gid}")
    except Exception:
        logger.debug("maisoul: wait 恢复调度失败", exc_info=True)

async def _planner_execute_reply(P, deps, reason: str, args: dict) -> str:
    """reply 工具执行：replyer 生成 + 后处理发送（reply_style/set_quote 参数生效）。"""
    st, eff_cfg = deps.st, deps.cfg
    umo, platform, gid = deps.umo, deps.platform, deps.gid
    reply_baseline = len(st.buffer)  # P-D：reply 开始时的缓冲基线（发送前比对）
    _monitor_stage(P, gid, monitor.STAGE_REPLYER, "生成可见回复")
    provider = P.context.get_using_provider()
    if provider is None:
        return "无可用的模型 Provider，回复失败"

    msg_id = str(args.get("msg_id") or "").strip()
    reply_reference = str(args.get("reply_reference") or "").strip()
    reply_style = str(args.get("reply_style") or "").strip()
    # 引用回复：set_quote（MaiBot 默认 true）+ enable_reply_quote 双开关；
    # 首段挂 Reply(msg_id)（aiocqhttp 发送侧 toDict 为 OneBot reply 段）
    set_quote = bool(args.get("set_quote", True)) and bool(eff_cfg.get("enable_reply_quote", True))

    system_prompt = prompt.build_system_prompt(eff_cfg, chat_id=gid, platform=platform,
                                               is_group=deps.is_group)
    system_prompt += bridge.build_skills_block(eff_cfg)
    _uid = ""
    for m in reversed(list(st.buffer)):
        if str(m.get("msg_id") or "") == msg_id:
            _uid = str(m.get("sid") or "")
            break
    system_prompt += await _xinxian_profile_block(P, gid, _uid)
    # v6.9.7 管家迁位（对齐 MaiBot 分工）：replyer 是纯生成器，不带任何工具——
    # 查资料/跑任务全部在 planner 侧经 tool_search 发现 deferred 工具完成，
    # 工作成果由 planner 写进 reply_reference 传入。管家桥仅 independent/native 模式保留。
    # M8：表达块选择走 replyer 公共段；query 的 reply_reference 优先级
    # 对齐 _build_expression_query_text（reply 工具参数 > Planner 推理）
    expr_block = await _select_expr_block(
        P, st, eff_cfg, platform, gid, provider, reason,
        reply_reference=str(args.get("reply_reference") or ""),
        is_group=deps.is_group)
    # 黑话参考已移至 planner 每轮注入（对齐 jargon_context_matcher 位置）
    trigger_text = ""
    for m in reversed(list(st.buffer)):
        if str(m.get("msg_id") or "") == msg_id:
            trigger_text = str(m.get("text") or "")
            break
    eco_event = deps.event or bridge.SyntheticEvent(deps.umo, send_message=P.context.send_message)
    eco_block, eco_extras = await _eco_inject_block(P, eco_event, trigger_text)
    if eco_block:
        system_prompt += f"\n\n{eco_block}"
    deps.st.planner_state().eco_injection = eco_block + (
        "\n\n[用户内容附加]\n"
        + "\n".join(_extra_part_text(x) for x in eco_extras) if eco_extras else "")
    keyword_block = learning.keyword_reaction_block(eff_cfg, trigger_text)

    reference = reply_reference or (f"当前思考：\n{reason}" if reason else "")
    user_message = prompt.build_final_user_message(
        st, eff_cfg, "", reply_style,
        expression_habits=expr_block,
        keyword_reaction=keyword_block, reference_override=reference,
        is_group=deps.is_group)

    try:
        image_parts = prompt.image_context_parts(st, eff_cfg)
        resp = await _task_text_chat(P, 
            "replyer", eff_cfg,
            prompt=user_message, session_id=f"maisoul_{gid}", system_prompt=system_prompt,
            extra_user_content_parts=(eco_extras + (image_parts or [])) or None)
    except Exception as e:
        P.monitor.emit_llm_error(
            session_id=gid, task_name="replyer", request_type="text_chat",
            model_name=str(getattr(provider, "id", "") or type(provider).__name__),
            message=str(e))
        raise

    answer = _resp_text(resp)
    if not answer:
        return "模型未返回内容，本次未发言"

    # M8：投递/记账/学习调度走 replyer 公共段（与 independent 同一实现）；
    # eco_event 用于回写（deps.event 为 None 时是合成事件）
    quote_id = msg_id if (set_quote and msg_id and deps.send_fn is None) else ""
    sent = await _deliver_reply(
        P, st=st, eff_cfg=eff_cfg, gid=gid, umo=umo, event=deps.event,
        provider=provider, platform=platform, answer=answer,
        msg_id=msg_id or "", quote_id=quote_id, webchat_send=deps.send_fn,
        eco_event=eco_event,
        gen_baseline=reply_baseline)
    return f"已发送 {len(sent)} 段" + ("（引用回复）" if quote_id else "")

async def _planner_send_emoji(P, deps) -> str:
    """send_emoji 工具执行：语境选择表情包（v6.9.7 重写）。

    对齐 MaiBot send_emoji 的子代理选图（emoji_selection.prompt 的精神），适配
    stealer 两步制：子 LLM 从上下文提炼检索词 → search_meme 取候选 → 取首个
    候选编号 send_meme（stealer 的 BM25+faiss 检索已按相关性排序，取首条等价
    于 MaiBot「情绪→最匹配」且省一次视觉选择）。search/send 必须复用同一
    event 对象——stealer 的候选列表挂在 event._emoji_turn_state 上。
    """
    import re as _re
    try:
        mgr = P.context.get_llm_tool_manager()
        search_tool = mgr.get_func("search_meme")
        send_tool = mgr.get_func("send_meme")
        if search_tool is None or send_tool is None:
            return ("表情包工具不可用（需要 astrbot_plugin_stealer 的 "
                    "search_meme/send_meme 两件套）")
        ev = deps.event or bridge.SyntheticEvent(
            deps.umo, send_message=P.context.send_message)
        provider = P.context.get_using_provider()
        query = ""
        if provider is not None:
            try:
                recent = "\n".join(f"{m.get('name')}: {m.get('text')}"
                                   for m in list(deps.st.buffer)[-15:])
                resp = await _task_text_chat(P, 
                    "emoji", deps.cfg,
                    prompt=planner.EMOJI_QUERY_PROMPT.format(
                        chat_context=recent or "（群聊暂无消息）",
                        bot_name=str(deps.cfg.get("bot_name") or "麦麦"),
                        reason=deps.latest_reason or "（无）"),
                    session_id=f"maisoul_emoji_{deps.gid}")
                query = _resp_text(resp).strip().strip('"“”‘’')
            except Exception:
                logger.debug("maisoul: 表情检索词生成失败", exc_info=True)
        if not query:
            query = "开心"
        search_result = await bridge.call_llm_tool(
            P.context, ev, search_tool, {"query": query})
        hit = _re.search(r"\[(\d+)\]", search_result or "")
        if not hit:
            return f"表情包检索失败: {str(search_result)[:120]}"
        send_result = await bridge.call_llm_tool(
            P.context, ev, send_tool, {"emoji_id": int(hit.group(1))})
        if "发送失败" in send_result:
            return f"表情包发送失败: {send_result[:120]}"
        _emit_sent(P, deps.gid, f"[表情包] {query}", "", "emoji", deps.event)
        return f"已发送表情包（检索词：{query}）"
    except Exception as e:
        return f"表情包发送失败: {e}"

# ------------------------------------------------------------------ #
# 协作模式：把麦麦三件套注入原生 LLM 请求；回写发言到群聊流                #
