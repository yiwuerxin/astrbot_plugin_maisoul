"""astrbot_plugin_maisoul v6.1 —— 麦麦(MaiBot)发言流水线深度复刻 + 管家桥

结构：
- main.py        只做注册/生命周期/钩子分发（薄入口）
- core/constants 评分词典（逐条对齐 reply_necessity.py）
- core/states    群会话状态（缓冲/积压/存在感/防重复）
- core/scoring   档位制评分：final = (档位 + 内容 + 压力 − 存在感) × 频率倍率
- core/prompt    MaiBot 三件套 prompt 组装（maisaka_replyer 结构）
- core/sender    打字延迟模型 + 错字模拟 + 分段发送
- core/bridge    管家桥（call_maid）
- webui/routes   插件页后端 API
"""

import asyncio
import re
import shutil
import time
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Reply
from astrbot.api.star import Context, Star, register
from astrbot.core.provider.entities import LLMResponse, ProviderRequest
from astrbot.core.star.star_handler import EventType, star_handlers_registry

from .core import bridge, learning, modelbind, monitor, personas, planner, prompt, sender, trigger
from .core.constants import MESSAGE_DEBOUNCE_SECONDS, OUTPUT_INSTRUCTION
from .core.states import StateManager
from .webui import routes as webui_routes

try:
    from astrbot.api.event import MessageChain
except ImportError:  # 兼容不同小版本
    from astrbot.core.message.message_event_result import MessageChain

# 运行时数据文件清单（观察账本/学习库及 SQLite 侧车与旧版遗留）——卸载时随插件
# 目录被无条件删除，必须存进 AstrBot 持久化目录（坑 50）
_RUNTIME_DATA_FILES = (
    "data_learning.json",
    "data_monitor.db",
    "data_monitor.db-wal",
    "data_monitor.db-shm",
    "data_monitor.json.imported",
)


@register("astrbot_plugin_maisoul", "meng", "麦麦发言流水线深度复刻+管家桥+多人格", "6.14.3")
class MaiSoulPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.states = StateManager()
        data_dir = self._persistent_data_dir()
        self.learning_store = learning.LearningStore(path=data_dir / "data_learning.json")
        self.monitor = monitor.Monitor(monitor.MonitorStore(data_dir / "data_monitor.db"))
        self._cycle_counter: dict[str, int] = {}
        self._group_sessions: set[str] = set()
        self._monitor_sessions: set[str] = set()

    @staticmethod
    def _persistent_data_dir() -> Path:
        """运行时数据目录：AstrBot 的 data/plugin_data/<插件名>。

        AstrBot 卸载插件时会无条件删除整个插件目录，勾选框只控制配置文件与
        plugin_data 的清理——观察账本/学习库放插件目录里会在"未勾删除数据"
        的卸载中一起消失（坑 50）。首次运行把插件目录里的旧数据文件搬过来。
        """
        from astrbot.core.star.star_tools import StarTools

        data_dir = StarTools.get_data_dir("astrbot_plugin_maisoul")
        legacy_dir = Path(__file__).resolve().parent
        for name in _RUNTIME_DATA_FILES:
            src, dst = legacy_dir / name, data_dir / name
            if src.exists() and not dst.exists():
                shutil.move(str(src), str(dst))
                logger.info(f"maisoul: 运行时数据 {name} 已迁移至持久化目录 {data_dir}")
        return data_dir

    async def initialize(self):
        self._migrate_legacy_nicknames()
        logger.info(
            f"maisoul v6.14.3 已加载：模式={self.config['mode']} bot={self.config['bot_name']} "
            f"触发模式={self.config.get('reply_trigger_mode', 'frequency')} "
            f"talk_value={self.config.get('talk_value', 1.0)} "
            f"错字={'开' if self.config.get('typo_enable', True) else '关'} 管家桥="
            f"{'开' if self.config.get('maid_bridge', True) else '关'}")
        webui_routes.register_webui(self.context, self.config, self.states,
                                    self.learning_store, self.monitor)

    def _migrate_legacy_nicknames(self):
        """v6.3 前别名拆在 nicknames（提及检测）里：合并进统一的 aliases，避免丢词。"""
        legacy = [str(n).strip() for n in (self.config.get("nicknames") or []) if str(n).strip()]
        if not legacy:
            return
        aliases = [str(a).strip() for a in (self.config.get("aliases") or []) if str(a).strip()]
        merged = aliases + [n for n in legacy if n not in aliases]
        self.config["aliases"] = merged
        try:
            self.config.save_config()
            logger.info(f"maisoul: 旧 nicknames 已合并进 aliases → {merged}")
        except Exception:
            logger.debug("maisoul: 别名迁移保存失败（内存已生效）", exc_info=True)

    # ------------------------------------------------------------------ #
    # 门控：聊天消息评分（低优先级 = 在其他被动插件之后运行；群聊+私聊全接管）      #
    # ------------------------------------------------------------------ #
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=-1000)
    async def on_group_message(self, event: AstrMessageEvent):
        await self._process_chat(event, is_group=True)

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=-1000)
    async def on_private_message(self, event: AstrMessageEvent):
        await self._process_chat(event, is_group=False)

    async def _process_chat(self, event: AstrMessageEvent, is_group: bool):
        if not self.config["enable"]:
            return

        text = (event.message_str or "").strip()
        logger.debug(f"maisoul: 收到消息 [{event.get_platform_name()}] "
                     f"{event.get_sender_name()}: {text[:40]}")

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
                for _h in (event.get_extra("activated_handlers") or []):
                    if any(isinstance(_f, _CF)
                           for _f in (getattr(_h, "event_filters", None) or [])):
                        escape = True
                        break
            except Exception:
                pass
        explicit = False
        if is_group and event.is_at_or_wake_command:
            if self.config.get("escape_at_wake"):
                escape = True
            else:
                explicit = True
        logger.debug(f"maisoul: escape={escape} explicit={explicit} "
                     f"sender={event.get_sender_id()} self={event.get_self_id()}")
        if escape:
            self._record(event, text)
            return

        # 过滤词（对齐 [message_receive].ban_words/ban_msgs_regex：注册前整条丢弃，
        # 不进缓存不进门控；指令类消息（escape）不检查，同 MaiBot 只查非命令候选）
        if trigger.hit_ban_filter(text, self.config.get("ban_words"),
                                  self.config.get("ban_msgs_regex")):
            logger.debug(f"maisoul: 消息命中过滤词，已丢弃: {text[:30]}")
            return

        if str(event.get_sender_id()) == str(event.get_self_id()):
            logger.debug("maisoul: 自发消息，跳过")
            event.stop_event()
            return

        gid = (str(event.get_group_id()) if is_group
               else str(event.get_sender_id() or event.unified_msg_origin))
        st = self.states.get(gid)
        if is_group:
            self._group_sessions.add(gid)
        # 麦麦观察：会话首次进入管线时上报会话标识（对齐 runtime 启动时的 session.start）
        if gid not in self._monitor_sessions:
            self._monitor_sessions.add(gid)
            self.monitor.emit_session_start(
                gid, self._session_name(gid), is_group_chat=is_group,
                group_id=gid if is_group else None,
                user_id=None if is_group else gid,
                platform=str(event.get_platform_name() or ""))
        self._record(event, text, gid)
        logger.debug(f"maisoul[{gid}]: 记录完成 pending={st.pending_since_fire}")

        aliases = [str(a) for a in (self.config.get("aliases") or [])]
        bot_name = str(self.config["bot_name"])
        mentioned = any(k and k in text for k in [bot_name, *aliases])
        if not mentioned:
            # 回复引用机器人 = 提及（对齐 is_mentioned_bot_in_message 第 6 层：
            # 回复引用算 mention 不算 at；批次内任一命中即算——扫当前消息+未消费积压）
            mentioned = self._is_reply_to_bot(event) or any(
                m.get("reply_bot") for m in st.buffer
                if st.last_fire_ts and float(m.get("ts") or 0) > st.last_fire_ts)
        # 显式召唤（@ 或唤醒前缀）与 At 段同级——都算 at 档强制触发
        at_bot = self._has_at_bot(event) or explicit
        fired, detail, nec = trigger.should_trigger(
            st, self.config,
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
            self._maybe_defer_recheck(event, st, gid, is_group)
            event.stop_event()
            return

        style = nec.style if nec else ""
        logger.info(f"maisoul[{gid}] 触发发言（{detail}）")
        st.cancel_defer()
        if self.config["mode"] == "native":
            # 协作模式：交给原生 agent，生态插件的注入与出站美化生效
            st.mark_fire(self._msg_id(event))
            event.is_at_or_wake_command = True
            event.set_extra("maisoul_triggered", True)
            return

        if self.config["mode"] == "planner":
            # 决策模式：进入 maisaka Planner（调度语义对齐 turn_scheduler/runtime）
            forced = ((at_bot and self.config.get("inevitable_at_reply", True))
                      or (mentioned and self.config.get("mentioned_bot_reply", False)))
            # WebUI 聊天页同步跑完整决策：段落经 event.send 流进当前请求气泡。
            # 后台任务方式下聊天 API 会在事件结束时关流，回复只能落 proactive 存库，
            # 页面上只剩一条秒回的空气泡（_has_send_oper 会让原生 LLM 阶段自动跳过）。
            webchat = str(event.get_platform_name() or "") == "webchat"
            await self._schedule_planner(
                event, st, gid, forced, is_group,
                send_fn=self._webchat_sender(event) if webchat else None)
            event.stop_event()
            return

        # 独立模式：麦麦流水线自管生成与发送
        if st.firing:
            return
        st.firing = True
        try:
            await self._generate_and_send(event, st, detail, style, text, is_group)
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
    # ------------------------------------------------------------------ #
    def _resolve_bound_model(self, cand: dict):
        """{provider, model} → (provider实例, model名)；provider 不存在返回 None。

        provider 可填源名（如 google_gemini_openai）：精确 id 未命中时取该源
        下任一已启用条目承载本次调用（同源条目共享 base_url/key，model 按次覆盖）。
        """
        try:
            inst_map = getattr(self.context, "provider_manager", None).inst_map or {}
        except Exception:
            return None
        pid = str(cand.get("provider"))
        inst = inst_map.get(pid)
        if inst is None:
            for key, candidate in inst_map.items():
                if key.split("/", 1)[0] == pid:
                    inst = candidate
                    break
        if inst is None:
            return None
        return inst, str(cand.get("model"))

    _task_model_rr: dict[str, int] = {}  # balance 轮转计数器

    def _pick_task_model(self, task: str, cfg):
        """按策略选主候选（小任务子调用用：无降级链）。None = 跟随默认 Provider。"""
        candidates = modelbind.task_model_candidates(cfg, task)
        if not candidates:
            return None
        strategy = modelbind.task_model_strategy(cfg, task)
        pick = modelbind.pick_model(candidates, strategy, self._task_model_rr, task)
        return self._resolve_bound_model(pick) if pick else None

    def _embedding_provider(self, eff_cfg):
        """embedding 任务绑定的嵌入 Provider（vector_intent 表达召回用）。

        嵌入 Provider 走 AstrBot 的 EmbeddingProvider 体系（get_embeddings），
        同样登记在 inst_map：绑定解析复用 _pick_task_model；未绑定时取第一个
        可用嵌入实例，无则 None（调用方回落 legacy 抽样）。
        """
        try:
            insts = list(getattr(self.context.provider_manager,
                                 "embedding_provider_insts", None) or [])
        except Exception:
            return None
        if not insts:
            return None
        resolved = self._pick_task_model("embedding", eff_cfg)
        return resolved if resolved is not None else insts[0]

    async def _task_text_chat(self, task: str, cfg, **kwargs):
        """按任务绑定调 text_chat：策略选主候选，异常时依次降级链上后续候选；
        无绑定走 AstrBot 当前默认 Provider。"""
        provider = self.context.get_using_provider()
        candidates = modelbind.task_model_candidates(cfg, task)
        if not candidates:
            return await provider.text_chat(**kwargs)
        strategy = modelbind.task_model_strategy(cfg, task)
        chain = modelbind.build_model_chain(candidates, strategy,
                                            self._task_model_rr, task)
        last_err: Exception | None = None
        for cand in chain:
            resolved = self._resolve_bound_model(cand)
            if resolved is None:
                logger.warning(f"maisoul: 任务 {task} 绑定的 provider "
                               f"{cand.get('provider')} 不存在，跳过")
                continue
            inst, model = resolved
            try:
                return await inst.text_chat(model=model, **kwargs)
            except Exception as e:
                last_err = e
                logger.warning(f"maisoul: 任务 {task} 模型 {cand.get('provider')}/{model} "
                               f"调用失败，尝试下一候选: {e}")
        if last_err is not None:
            raise last_err
        return await provider.text_chat(**kwargs)


    def _maybe_defer_recheck(self, event: AstrMessageEvent, st, gid: str, is_group: bool):
        """frequency 门未触发：按 MaiBot delay 公式安排到点重查。

        无新消息也会到点重评空窗补偿——安静群中等够平均间隔 × 差额后主动开口。
        webchat（请求结束即关流）与协作模式（需活跃管线）不适用。
        """
        if str(self.config.get("reply_trigger_mode") or "frequency") != "frequency":
            return
        platform = str(event.get_platform_name() or "")
        if platform == "webchat" or self.config["mode"] == "native":
            return
        pl = st.planner_state()
        if pl.agent_state in ("running", "wait") or st.firing:
            return
        threshold = trigger.message_trigger_threshold(
            "frequency",
            trigger.effective_talk_value(self.config, platform, gid, is_group=is_group))
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
                st, self.config, at_bot=False, mentioned=False, text="",
                aliases=[], bot_name=str(self.config["bot_name"]),
                platform=platform, chat_id=gid, is_group=is_group)
            if not fired:
                logger.debug(f"maisoul[{gid}] 空窗到点重查未达标：{detail}")
                return
            logger.info(f"maisoul[{gid}] 空窗补偿到点触发（{detail}）")
            if self.config["mode"] == "planner":
                await self._schedule_planner(event, st, gid, False, is_group)
            else:
                await self._generate_and_send(event, st, detail, "", "", is_group)

        st.defer_task = asyncio.create_task(_recheck())
        logger.debug(f"maisoul[{gid}] 空窗补偿重查已排期：{delay:.1f}s 后重评")

    # ------------------------------------------------------------------ #
    # 独立模式：生成与拟人发送（含管家桥）                                   #
    # ------------------------------------------------------------------ #
    async def _generate_and_send(self, event: AstrMessageEvent, st, reason: str,
                                 style: str, trigger_text: str = "", is_group: bool = True):
        provider = self.context.get_using_provider()
        if provider is None:
            logger.warning("maisoul: 未配置可用的模型 Provider，本次跳过发言")
            return

        gid = str(event.get_group_id() or event.unified_msg_origin)
        umo = event.unified_msg_origin
        platform = str(event.get_platform_name() or "")
        eff_cfg, pname = await personas.resolve_active(
            self.context, self.config, gid, umo)
        st.last_persona = pname
        system_prompt = prompt.build_system_prompt(eff_cfg, chat_id=gid, platform=platform,
                                                   is_group=is_group)
        system_prompt += bridge.build_skills_block(eff_cfg)
        eco_block, eco_extras = await self._eco_inject_block(event, trigger_text)
        if eco_block:
            system_prompt += f"\n\n{eco_block}"

        # 学习注入块（表达习惯选择 + 关键词反应 —— 格式对齐 MaiBot 原文）；
        # 黑话参考已移至 planner 每轮注入（对齐 jargon_context_matcher 位置）
        use_expr, _ = learning.learning_flags(
            eff_cfg, "expression_learning_list", platform, gid, is_group)
        expr_block = ""
        if use_expr:
            observe = learning.build_chat_info(list(st.buffer))
            expr_bind = self._pick_task_model("expression_use", eff_cfg)
            emb = self._embedding_provider(eff_cfg)
            expr_block = await learning.select_expression_habits_block(
                expr_bind[0] if expr_bind else provider, self.learning_store,
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

        func_tool = bridge.build_chat_toolset(self.context, eff_cfg)
        if func_tool is not None:
            system_prompt += bridge.MAID_BRIDGE_PROMPT

        self._monitor_stage(gid, monitor.STAGE_REPLYER, "生成可见回复", agent_state="running")
        try:
            resp = await self._task_text_chat(
                "replyer", eff_cfg,
                prompt=user_message,
                session_id=f"maisoul_{gid}",
                system_prompt=system_prompt,
                func_tool=func_tool,
                extra_user_content_parts=eco_extras or None,
            )
        except Exception as e:
            self.monitor.emit_llm_error(
                session_id=gid, task_name="replyer", request_type="text_chat",
                model_name=str(getattr(provider, "id", "") or type(provider).__name__),
                message=str(e))
            raise

        if getattr(resp, "tools_call_name", None):
            results = await bridge.exec_tool_calls(self.context, event, resp)
            if results:
                logger.info(f"maisoul[{gid}] 管家桥执行: {results[:120]}")
                user_message += (
                    f"\n\n【管家执行结果】\n{results}\n\n"
                    "请结合结果，用你自己的口吻输出给群友的发言内容。"
                )
            resp = await self._task_text_chat(
                "replyer", eff_cfg,
                prompt=user_message,
                session_id=f"maisoul_{gid}",
                system_prompt=system_prompt,
                extra_user_content_parts=eco_extras or None,
            )

        answer = self._resp_text(resp)
        if not answer:
            logger.info(f"maisoul[{gid}] 模型未返回内容，放弃本次发言")
            return

        # WebUI 聊天页是单气泡：AstrBot 的 run accumulator 对 streaming=False 的
        # plain 事件做替换，逐段 event.send 会互相覆盖——先攒段，结束后合并一次发。
        webchat = str(event.get_platform_name() or "") == "webchat"
        webchat_buf: list[str] = []
        # 引用回复（对齐 MaiBot reply 的 set_quote 默认 true）：首段挂 Reply(触发消息)
        quote_id = (self._msg_id(event)
                    if not webchat and eff_cfg.get("enable_reply_quote", True) else "")
        quoted = {"done": False}

        async def send(text: str) -> None:
            if webchat:
                webchat_buf.append(text)
                return
            if quote_id and not quoted["done"]:
                quoted["done"] = True
                await self.context.send_message(
                    umo, MessageChain([Reply(id=quote_id)]).message(text))
            else:
                await self.context.send_message(umo, MessageChain().message(text))
            self._emit_sent(gid, text, self._msg_id(event), "reply", event)

        sent = await sender.send_humanlike(send, answer, eff_cfg)
        if webchat and webchat_buf:
            await event.send(MessageChain().message("\n\n".join(webchat_buf)))
            for seg_text in webchat_buf:
                self._emit_sent(gid, seg_text, self._msg_id(event), "reply", event)
        st.record_self_reply(self._msg_id(event), sent,
                             str(eff_cfg.get("bot_name") or self.config["bot_name"]),
                             quote=quote_id if quoted["done"] else "")
        logger.info(f"maisoul[{gid}] 已发言 {len(sent)} 段（人格={pname}）")
        await self._eco_fire_response(event, "\n".join(sent))
        self._schedule_learning(provider, eff_cfg, st, platform, gid)

    # ------------------------------------------------------------------ #
    # 生态注入桥（v6.11.0）：手动触发 on_llm_request/on_llm_response 钩子链，  #
    # 心弦好感/记忆/世界书等注入型插件在麦麦管线内同样生效                       #
    # ------------------------------------------------------------------ #
    @staticmethod
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

    @staticmethod
    def _extra_part_text(part) -> str:
        """观察页展示用：ContentPart/组件 → 纯文本（图片等无文本段以类名占位）。"""
        text = getattr(part, "text", None)
        return str(text) if text is not None else f"<{type(part).__name__}>"

    async def _eco_inject_block(self, event: AstrMessageEvent, text: str):
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
        if not self.config.get("eco_injection", True):
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
            extras = self._normalize_extra_parts(
                getattr(req, "extra_user_content_parts", None) or [])
            logger.info(f"maisoul: 生态注入桥执行 {fired} 个钩子，system {len(block)} 字符"
                        f" + 用户内容附加 {len(extras)} 段"
                        f"（好感/记忆/世界书；私聊心弦不注入属正常）")
            return block, extras
        except Exception:
            logger.error("maisoul: 生态注入桥失败", exc_info=True)
            return "", []

    async def _eco_fire_response(self, event: AstrMessageEvent, answer: str):
        """发言后触发 on_llm_response 钩子（livingmemory 记忆沉淀等生态回写）。"""
        if not self.config.get("eco_injection", True) or not (answer or "").strip():
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

    def _webchat_sender(self, event: AstrMessageEvent):
        """WebUI 聊天页发送通道：段落经 event.send 流进当前请求的气泡。"""
        async def _send(text: str) -> None:
            await event.send(MessageChain().message(text))
        return _send

    def _schedule_learning(self, provider, eff_cfg, st, platform: str, gid: str):
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
            learn_bind = self._pick_task_model("learner", snapshot_cfg)

            async def _run():
                try:
                    summary = await learning.learn_from_chat(
                        learn_bind[0] if learn_bind else provider,
                        snapshot_cfg, snapshot_buf, platform, gid,
                        self.learning_store,
                        model=learn_bind[1] if learn_bind else None)
                    if summary and summary != "学习未启用":
                        logger.info(f"maisoul[{gid}] 学习: {summary}")
                except Exception:
                    logger.debug("maisoul: 学习任务失败", exc_info=True)

            import asyncio as _asyncio
            _asyncio.create_task(_run())
        except Exception:
            logger.debug("maisoul: 学习任务调度失败", exc_info=True)

    # ------------------------------------------------------------------ #
    # Planner 决策模式：maisaka agent 循环                                  #
    # ------------------------------------------------------------------ #
    def _schedule_planner(self, event: AstrMessageEvent, st, gid: str, forced: bool,
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
        cfg = self.config

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

        pl.agent_state = "running"
        pl.interrupt_count = 0
        umo = event.unified_msg_origin
        platform = str(event.get_platform_name() or "")
        pl.umo, pl.platform = umo, platform
        pl.is_group = is_group
        cycle_id = self._cycle_counter.get(gid, 0) + 1
        self._cycle_counter[gid] = cycle_id
        pl.last_event = event  # wait 续轮/定时循环复用同一真实 event（候选/注入都挂在 event 上）
        logger.info(f"maisoul[{gid}] planner: 启动决策循环")
        self._monitor_stage(gid, monitor.STAGE_LOOP_START, f"循环 {cycle_id}",
                            agent_state=pl.agent_state)
        if send_fn is not None:
            done.close()  # webchat 分支返回完整循环协程；预建的空协程关闭，防未 await 告警
            return self._planner_cycle(umo, platform, gid, st, is_group, send_fn=send_fn,
                                      event=event)
        pl.running_task = asyncio.create_task(
            self._planner_cycle(umo, platform, gid, st, is_group, event=event))
        return done

    def _drain_pending(self, st, pl) -> list[dict]:
        pending = [m for m in list(st.buffer) if float(m.get("ts") or 0) > pl.last_cycle_ts
                   and str(m.get("sid")) != "self"]
        pl.last_cycle_ts = time.time()
        st.pending_since_fire = 0
        return pending

    async def _planner_cycle(self, umo: str, platform: str, gid: str, st,
                             is_group: bool = True, send_fn=None,
                             initial_feedback: str = "",
                             event: AstrMessageEvent | None = None):
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
            event = bridge.SyntheticEvent(umo, send_message=self.context.send_message)
        else:
            pl.last_event = event
        cycle_id = self._cycle_counter.get(gid, 0)
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
            self.monitor.emit_planner_finalized(
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
            provider = self.context.get_using_provider()
            if provider is None:
                logger.warning("maisoul: 未配置可用的模型 Provider，planner 跳过")
                pl.agent_state = "idle"
                finalize("no_provider")
                return
            logger.debug(f"maisoul planner[{gid}]: resolve 人格前")
            eff_cfg, pname = await personas.resolve_active(self.context, self.config, gid, umo)
            st.last_persona = pname
            logger.debug(f"maisoul planner[{gid}]: 人格={pname}，构建工具集")
            deps = planner.PlannerDeps(self, st, eff_cfg, event, platform, gid, is_group,
                                       send_fn=send_fn)
            deps.umo = umo
            deps.deferred_pool = bridge.list_deferred_tools(self.context, eff_cfg)
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
            # 上截取
            contexts, history_msgs = planner.build_history_contexts(
                [m for m in all_buf if m not in pending_now],
                pl.analysis_log, context_limit, is_group)
            history_count = len(history_msgs)
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
                pending = self._drain_pending(st, pl)
                # contexts 尾部是工具结果轮（role=tool）时视为有待续轮（对齐
                # tool_continue：工具结果回填后不要求新消息即继续）
                tail_is_tool = bool(contexts) and contexts[-1].get("role") == "tool"
                if not pending and round_index > 0 and not tool_feedback and not tail_is_tool:
                    end_reason = "no_new_message"
                    break  # 无新消息且无待回填的工具结果 → 本轮结束
                if pending:
                    self._monitor_stage(gid, monitor.STAGE_MESSAGE_INTAKE,
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
                        self.learning_store, jargon_key, jargon_recent,
                        exclude=injected_jargons or None, matched_out=new_terms)
                    if jargon_block:
                        injected_jargons.update(new_terms)
                        contexts.append({"role": "user", "content": jargon_block})
                round_text = f"第 {round_index + 1} 轮"
                self._monitor_stage(gid, monitor.STAGE_PLANNER, "组织上下文并请求模型",
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
                planner_bind = self._pick_task_model("planner", eff_cfg)
                if planner_bind is not None:
                    logger.debug(f"maisoul planner[{gid}]: 任务模型 "
                                 f"{planner_bind[1]}@{getattr(planner_bind[0], 'provider_config', {}).get('id', '?')}")
                llm_started = time.time()
                try:
                    resp = await self._task_text_chat(
                        "planner", eff_cfg,
                        prompt=final_reminder,
                        session_id=f"maisoul_planner_{gid}",
                        system_prompt=system_prompt,
                        func_tool=tools,
                        contexts=contexts or None,
                        extra_user_content_parts=image_parts or None,
                    )
                except Exception as e:
                    self.monitor.emit_llm_error(
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
                analysis = self._resp_text(resp)
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
                logger.debug(f"maisoul planner[{gid}]: 正文 {len(self._resp_text(resp))} 字 / "
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
                    pl.agent_state = "idle"
                    self._monitor_stage(gid, monitor.STAGE_WAITING, "本轮处理结束",
                                        agent_state=pl.agent_state)
                    finalize("no_action", (analysis or "")[:120])
                    return
                for i, name in enumerate(names):
                    args = args_list[i] if i < len(args_list) and isinstance(args_list[i], dict) else {}
                    self._monitor_stage(gid, monitor.STAGE_TOOL_PREFIX + str(name),
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
                            pl.agent_state = "idle"
                        else:
                            # wait 到期必续轮（坑 26）：调度到期回执再跑一轮——
                            # 缺失会让会话挂在 wait 直到下一条消息才动
                            self._schedule_wait_resume(
                                st, eff_cfg, gid,
                                max(0, int(args.get("seconds", 0) or 0)))
                        self._monitor_stage(gid, monitor.STAGE_WAITING, "本轮处理结束",
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
                            umo, send_message=self.context.send_message)
                        try:
                            result = await bridge.call_llm_tool(
                                self.context, ev, deferred_item["tool"], args)
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
            pl.agent_state = "idle"
            self._monitor_stage(gid, monitor.STAGE_WAITING, "本轮处理结束",
                                agent_state=pl.agent_state)
            finalize(end_reason or "max_rounds")
        except asyncio.CancelledError:
            interrupted = True
            self._monitor_stage(gid, monitor.STAGE_PLANNER_INTERRUPTED,
                                "收到外部中断信号", agent_state=pl.agent_state)
            finalize("interrupted")
            pl.agent_state = "idle"
            raise
        except Exception as e:
            self._monitor_stage(gid, monitor.STAGE_ERROR, str(e)[:80],
                                agent_state=pl.agent_state)
            finalize("error", str(e)[:120])
            logger.error("maisoul planner 循环异常", exc_info=True)
            pl.agent_state = "idle"

    def _schedule_wait_resume(self, st, cfg, gid: str, seconds: int):
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
                pl.agent_state = "running"
                await self._planner_cycle(getattr(pl, "umo", ""), getattr(pl, "platform", ""),
                                          gid, st, getattr(pl, "is_group", True),
                                          initial_feedback=receipt)

        try:
            asyncio.create_task(_resume())
        except Exception:
            logger.debug("maisoul: wait 恢复调度失败", exc_info=True)

    async def _planner_execute_reply(self, deps, reason: str, args: dict) -> str:
        """reply 工具执行：replyer 生成 + 后处理发送（reply_style/set_quote 参数生效）。"""
        st, eff_cfg = deps.st, deps.cfg
        umo, platform, gid = deps.umo, deps.platform, deps.gid
        self._monitor_stage(gid, monitor.STAGE_REPLYER, "生成可见回复")
        provider = self.context.get_using_provider()
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
        # v6.9.7 管家迁位（对齐 MaiBot 分工）：replyer 是纯生成器，不带任何工具——
        # 查资料/跑任务全部在 planner 侧经 tool_search 发现 deferred 工具完成，
        # 工作成果由 planner 写进 reply_reference 传入。管家桥仅 independent/native 模式保留。
        use_expr, _ = learning.learning_flags(eff_cfg, "expression_learning_list", platform, gid,
                                              deps.is_group)
        expr_block = ""
        if use_expr:
            observe = learning.build_chat_info(list(st.buffer))
            expr_bind = self._pick_task_model("expression_use", eff_cfg)
            emb = self._embedding_provider(eff_cfg)
            expr_block = await learning.select_expression_habits_block(
                expr_bind[0] if expr_bind else provider, self.learning_store,
                learning.share_key(eff_cfg, "expression_groups", platform, gid),
                bool(eff_cfg.get("expression_checked_only", True)),
                observe, str(eff_cfg.get("bot_name") or "麦麦"), reason,
                model=expr_bind[1] if expr_bind else None,
                mode=str(eff_cfg.get("expression_selection_mode") or "legacy"),
                embedding=emb,
                embedding_model=str((getattr(emb, "provider_config", None) or {})
.get("id", "") or "") if emb is not None else "",
                # query 对齐 _build_expression_query_text：reply 工具的
                # reply_reference 优先，否则 Planner 推理（reason）
                query_text=learning.build_expression_query_text(
                    reply_reason=reason,
                    reply_reference=str(args.get("reply_reference") or "")),
                pool_size=int(eff_cfg.get("expression_vector_candidate_pool_size", 50) or 50))
        # 黑话参考已移至 planner 每轮注入（对齐 jargon_context_matcher 位置）
        trigger_text = ""
        for m in reversed(list(st.buffer)):
            if str(m.get("msg_id") or "") == msg_id:
                trigger_text = str(m.get("text") or "")
                break
        eco_event = deps.event or bridge.SyntheticEvent(deps.umo, send_message=self.context.send_message)
        eco_block, eco_extras = await self._eco_inject_block(eco_event, trigger_text)
        if eco_block:
            system_prompt += f"\n\n{eco_block}"
        deps.st.planner_state().eco_injection = eco_block + (
            "\n\n[用户内容附加]\n"
            + "\n".join(self._extra_part_text(x) for x in eco_extras) if eco_extras else "")
        keyword_block = learning.keyword_reaction_block(eff_cfg, trigger_text)

        reference = reply_reference or (f"当前思考：\n{reason}" if reason else "")
        user_message = prompt.build_final_user_message(
            st, eff_cfg, "", reply_style,
            expression_habits=expr_block,
            keyword_reaction=keyword_block, reference_override=reference,
            is_group=deps.is_group)

        try:
            image_parts = prompt.image_context_parts(st, eff_cfg)
            resp = await self._task_text_chat(
                "replyer", eff_cfg,
                prompt=user_message, session_id=f"maisoul_{gid}", system_prompt=system_prompt,
                extra_user_content_parts=(eco_extras + (image_parts or [])) or None)
        except Exception as e:
            self.monitor.emit_llm_error(
                session_id=gid, task_name="replyer", request_type="text_chat",
                model_name=str(getattr(provider, "id", "") or type(provider).__name__),
                message=str(e))
            raise

        answer = self._resp_text(resp)
        if not answer:
            return "模型未返回内容，本次未发言"

        # WebUI 聊天页单气泡合并（同 _generate_and_send：accumulator 替换语义）
        webchat_buf: list[str] = []
        quote_id = msg_id if (set_quote and msg_id and deps.send_fn is None) else ""
        quoted = {"done": False}

        async def send(text: str) -> None:
            if deps.send_fn is not None:
                webchat_buf.append(text)
                return
            if quote_id and not quoted["done"]:
                quoted["done"] = True
                await self.context.send_message(
                    umo, MessageChain([Reply(id=quote_id)]).message(text))
            else:
                await self.context.send_message(umo, MessageChain().message(text))
            self._emit_sent(gid, text, msg_id, "reply", deps.event)

        sent = await sender.send_humanlike(send, answer, eff_cfg)
        if webchat_buf:
            await deps.send_fn("\n\n".join(webchat_buf))
            for seg_text in webchat_buf:
                self._emit_sent(gid, seg_text, msg_id, "reply", deps.event)
        st.record_self_reply(msg_id or "", sent,
                             str(eff_cfg.get("bot_name") or self.config["bot_name"]),
                             quote=quote_id if quoted["done"] else "")
        await self._eco_fire_response(eco_event, "\n".join(sent))
        self._schedule_learning(provider, eff_cfg, st, platform, gid)
        return f"已发送 {len(sent)} 段" + ("（引用回复）" if quote_id else "")

    async def _planner_send_emoji(self, deps) -> str:
        """send_emoji 工具执行：语境选择表情包（v6.9.7 重写）。

        对齐 MaiBot send_emoji 的子代理选图（emoji_selection.prompt 的精神），适配
        stealer 两步制：子 LLM 从上下文提炼检索词 → search_meme 取候选 → 取首个
        候选编号 send_meme（stealer 的 BM25+faiss 检索已按相关性排序，取首条等价
        于 MaiBot「情绪→最匹配」且省一次视觉选择）。search/send 必须复用同一
        event 对象——stealer 的候选列表挂在 event._emoji_turn_state 上。
        """
        import re as _re
        try:
            mgr = self.context.get_llm_tool_manager()
            search_tool = mgr.get_func("search_meme")
            send_tool = mgr.get_func("send_meme")
            if search_tool is None or send_tool is None:
                return ("表情包工具不可用（需要 astrbot_plugin_stealer 的 "
                        "search_meme/send_meme 两件套）")
            ev = deps.event or bridge.SyntheticEvent(
                deps.umo, send_message=self.context.send_message)
            provider = self.context.get_using_provider()
            query = ""
            if provider is not None:
                try:
                    recent = "\n".join(f"{m.get('name')}: {m.get('text')}"
                                       for m in list(deps.st.buffer)[-15:])
                    resp = await self._task_text_chat(
                        "emoji", deps.cfg,
                        prompt=planner.EMOJI_QUERY_PROMPT.format(
                            chat_context=recent or "（群聊暂无消息）",
                            bot_name=str(deps.cfg.get("bot_name") or "麦麦"),
                            reason=deps.latest_reason or "（无）"),
                        session_id=f"maisoul_emoji_{deps.gid}")
                    query = self._resp_text(resp).strip().strip('"“”‘’')
                except Exception:
                    logger.debug("maisoul: 表情检索词生成失败", exc_info=True)
            if not query:
                query = "开心"
            search_result = await bridge.call_llm_tool(
                self.context, ev, search_tool, {"query": query})
            hit = _re.search(r"\[(\d+)\]", search_result or "")
            if not hit:
                return f"表情包检索失败: {str(search_result)[:120]}"
            send_result = await bridge.call_llm_tool(
                self.context, ev, send_tool, {"emoji_id": int(hit.group(1))})
            if "发送失败" in send_result:
                return f"表情包发送失败: {send_result[:120]}"
            self._emit_sent(deps.gid, f"[表情包] {query}", "", "emoji", deps.event)
            return f"已发送表情包（检索词：{query}）"
        except Exception as e:
            return f"表情包发送失败: {e}"

    # ------------------------------------------------------------------ #
    # 协作模式：把麦麦三件套注入原生 LLM 请求；回写发言到群聊流                #
    # ------------------------------------------------------------------ #
    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req):
        if not event.get_extra("maisoul_triggered"):
            return
        if not req or not hasattr(req, "system_prompt"):
            return
        gid = str(event.get_group_id() or event.get_sender_id() or event.unified_msg_origin)
        eff_cfg, pname = await personas.resolve_active(
            self.context, self.config, gid, event.unified_msg_origin)
        inject = (
            f"\n\n【maisoul 麦麦三件套（本群主动发言由意愿评分触发｜人格={pname}）】\n"
            f"{prompt.build_identity(eff_cfg)}\n"
            f"{prompt.select_reply_style(eff_cfg)}\n"
            f"{prompt.build_preset_dialogues_block(eff_cfg)}"
            f"{OUTPUT_INSTRUCTION}"
        )
        req.system_prompt = (req.system_prompt or "") + inject

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp):
        try:
            if event.get_extra("maisoul_eco_resp"):
                return  # 生态注入桥自己触发的链，防回声重复记录
            if self.config["mode"] == "independent":
                return
            answer = (getattr(resp, "completion_text", "") or "").strip()
            if not answer:
                return
            # 会话键与 _process_chat 对齐（群=群号，私聊=发送者ID），否则私聊回声记进另一个状态
            gid = str(event.get_group_id() or event.get_sender_id()
                      or event.unified_msg_origin)
            st = self.states.get(gid)
            segs = [s for s in answer.split("\n") if s.strip()]
            st.record_self_reply("", segs, self.config["bot_name"])
            # 麦麦观察：native/逃生舱路径的回复经回声钩子补 message.sent——
            # 否则聊天流只有用户发言、没有麦麦的回复记录（生产反馈的真实缺口）
            for seg in segs:
                self._emit_sent(gid, seg, "", "reply", event)
        except Exception:
            logger.debug("maisoul: 回声记录失败", exc_info=True)

    # ------------------------------------------------------------------ #
    # 管理指令                                                             #
    # ------------------------------------------------------------------ #
    @filter.command("maisoul")
    async def maisoul_cmd(self, event: AstrMessageEvent):
        raw = (event.message_str or "").strip()
        # waking_check 只剥唤醒前缀"/"，CommandFilter 匹配不改写 message_str——
        # 此处 raw 仍带命令名"maisoul"，子命令解析先剥掉它（否则 sim 等子命令
        # 全部落到状态分支，v6.13.4 修复）
        if raw.lower().startswith("maisoul"):
            raw = raw[len("maisoul"):].strip()
        arg = raw.lower()
        if arg.startswith("sim ") and raw[4:].strip():
            # 调试：把文本灌进完整管线（门控→planner→replyer），不依赖群聊适配器
            text = raw[4:].strip()
            st = self.states.get("sim")
            st.record_external({"name": event.get_sender_name() or "测试者",
                                "sid": str(event.get_sender_id()), "msg_id": "sim",
                                "text": text, "at_bot": False, "reply_bot": False,
                                "ts": time.time()})
            aliases = [str(a) for a in (self.config.get("aliases") or [])]
            bot_name = str(self.config["bot_name"])
            fired, detail, _ = trigger.should_trigger(
                st, self.config, at_bot=False,
                mentioned=any(k and k in text for k in [bot_name, *aliases]),
                text=text, aliases=aliases, bot_name=bot_name,
                platform="sim", chat_id="sim", is_group=False)
            yield event.plain_result(f"[sim 门控] {detail}")
            if not fired:
                return
            pl = st.planner_state()
            pl.agent_state = "running"
            pl.umo = event.unified_msg_origin
            pl.platform = "webchat"
            pl.is_group = False
            await self._planner_cycle(event.unified_msg_origin, "webchat", "sim", st, False,
                                      event=event)
            yield event.plain_result(f"[sim 完成] 人格={st.last_persona}，决策结果见上方发言/日志")
            return
        if arg in ("on", "off"):
            self.config["enable"] = arg == "on"
            self.config.save_config()
            yield event.plain_result(f"maisoul 已{'启用' if arg == 'on' else '停用'}")
        elif arg in ("planner", "native", "independent"):
            self.config["mode"] = arg
            self.config.save_config()
            yield event.plain_result(
                f"maisoul 已切换到 {'协作模式（生成交给原生agent）' if arg == 'native' else '独立模式（麦麦流水线完全接管发言）'}")
        else:
            f = max(0.0, float(self.config.get("talk_value", 1.0) or 0.0))
            th = trigger.message_trigger_threshold(
                str(self.config.get("reply_trigger_mode", "frequency")), f)
            yield event.plain_result(
                f"maisoul v6.14.3状态：{'运行中' if self.config['enable'] else '已停用'} | "
                f"模式={self.config['mode']} | bot={self.config['bot_name']}\n"
                f"触发模式={self.config.get('reply_trigger_mode', 'frequency')} "
                f"talk_value={f:.3f} 阈值={th}条消息 "
                f"错字={'开' if self.config.get('typo_enable', True) else '关'} 活跃群数={len(self.states)}\n"
                f"指令：/maisoul on|off | /maisoul planner|native|independent | /maisoul sim <文本>（WebUI 调试走完整管线）"
            )

    # ------------------------------------------------------------------ #
    # 工具函数                                                             #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _msg_id(event: AstrMessageEvent) -> str:
        try:
            return str(event.message_obj.message_id or "")
        except Exception:
            return ""

    def _record(self, event: AstrMessageEvent, text: str, gid: str | None = None):
        """gid 由调用方传入（群=群号，私聊=用户ID），保证与门控使用同一会话状态。"""
        if gid is None:
            gid = str(event.get_group_id() or event.get_sender_id()
                      or event.unified_msg_origin)
        sender_name = event.get_sender_name() or str(event.get_sender_id())
        group_id = str(event.get_group_id() or "")
        self.states.get(gid).record_external({
            "name": sender_name,
            "sid": str(event.get_sender_id()),
            "msg_id": self._msg_id(event),
            "text": text if text else "[图片/表情]",
            "at_bot": self._has_at_bot(event),
            "reply_bot": self._is_reply_to_bot(event),
            "quote": self._quote_ids(event),  # 引用目标（<message quote="…"> 属性用）
            "ts": time.time(),
            "images": self._extract_image_refs(event),  # 识图上下文用（v6.9.9，只存引用）
        })
        # 麦麦观察：消息注入事件（字段对齐 emit_message_ingested）
        self.monitor.emit_message_ingested(
            gid, sender_name, text if text else "[图片/表情]",
            self._msg_id(event), time.time(),
            platform=str(event.get_platform_name() or ""),
            user_id=str(event.get_sender_id() or ""),
            group_id=group_id,
        )

    def _monitor_stage(self, gid: str, stage: str, detail: str = "",
                       round_text: str = "", agent_state: str = "") -> None:
        """阶段状态上报（stage 名对齐 MaiBot reasoning_engine；不落账本仅广播）。"""
        self.monitor.emit_stage_status(
            session_id=gid, session_name=self._session_name(gid),
            stage=stage, detail=detail, round_text=round_text,
            agent_state=agent_state)

    def _session_name(self, gid: str) -> str:
        if gid in self._group_sessions:
            return f"群 {gid}"
        return f"私聊 {gid}"

    def _emit_sent(self, gid: str, content: str, msg_id: str, source_kind: str,
                   event: AstrMessageEvent | None = None) -> None:
        """麦麦观察：自己发送的消息事件（字段对齐 emit_message_sent）。"""
        self.monitor.emit_message_sent(
            gid, str(self.config.get("bot_name") or "麦麦"), content,
            msg_id, time.time(), source_kind,
            platform=str(event.get_platform_name() or "") if event else "",
            user_id=str(event.get_sender_id() or "") if event else "",
            group_id=str(event.get_group_id() or "") if event else "",
        )

    @staticmethod
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

    @staticmethod
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

    def _has_at_bot(self, event: AstrMessageEvent) -> bool:
        try:
            for seg in event.get_messages():
                if isinstance(seg, At) and str(seg.qq) == str(event.get_self_id()):
                    return True
        except Exception:
            pass
        return False

    def _is_reply_to_bot(self, event: AstrMessageEvent) -> bool:
        try:
            for seg in event.get_messages():
                if isinstance(seg, Reply) and str(seg.sender_id) == str(event.get_self_id()):
                    return True
        except Exception:
            pass
        return False

    @staticmethod
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

    async def terminate(self):
        logger.info("maisoul v6.14.3 已卸载")
