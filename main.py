"""astrbot_plugin_maisoul v6.25.1 —— 麦麦(MaiBot)发言流水线深度复刻 + 管家桥

main.py 只做注册/生命周期/钩子薄壳（M7 拆分）；管线逻辑在 pipeline/ 包：
- pipeline/gating        门控：逃生舱/过滤词/双模式分发/空窗补偿
- pipeline/replyer       独立模式生成与拟人发送
- pipeline/planner_host  Planner 决策循环（maisaka agent）
- pipeline/ecobridge     生态注入桥（心弦/记忆/世界书）
- pipeline/modelbind_host 任务级模型绑定宿主
- pipeline/native        协作模式三件套注入/回声记账
- pipeline/admin         /maisoul 管理指令与 sim
- core/*                 纯逻辑（评分/触发/提示词/后处理/状态/学习/监控/桥）
- webui/routes           插件页后端 API
"""

import asyncio
import shutil
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .core import learning, modelbind, monitor
from .core.states import StateManager
from .core.taskregistry import TaskRegistry
from .pipeline import admin, gating, native
from .webui import routes as webui_routes

# 运行时数据文件清单（观察账本/学习库及 SQLite 侧车与旧版遗留）——卸载时随插件
# 目录被无条件删除，必须存进 AstrBot 持久化目录（坑 50）
_RUNTIME_DATA_FILES = (
    "data_learning.json",
    "data_monitor.db",
    "data_monitor.db-wal",
    "data_monitor.db-shm",
    "data_monitor.json.imported",
)


@register(
    "astrbot_plugin_maisoul", "meng", "麦麦发言流水线深度复刻+管家桥+多人格", "6.25.1"
)
class MaiSoulPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.states = StateManager()
        # §6.6 情绪-关系耦合对外数值面：心弦探测 star_cls.api 调用
        # （get_feedback/apply_emotion_event），只交换数值不渲染提示词
        from .pipeline.emo_facade import EmotionFacade

        self.api = EmotionFacade(self.states, lambda: self.config)
        data_dir = self._persistent_data_dir()
        self.learning_store = learning.LearningStore(
            path=data_dir / "data_learning.json"
        )
        self.monitor = monitor.Monitor(
            monitor.MonitorStore(data_dir / "data_monitor.db")
        )
        self._cycle_counter: dict[str, int] = {}
        self._group_sessions: set[str] = set()
        self._monitor_sessions: set[str] = set()
        # 后台任务注册表（M6）：强引用 + 具名 + 完成自动清理 + 卸载时
        # cancel_and_wait_all——事件循环只持弱引用，裸任务可能被 GC 中途
        # 丢弃；且在飞任务必须在存储关闭前取消，否则在已关连接上继续跑
        self._registry = TaskRegistry()

    def _spawn(self, coro, name: str = "") -> asyncio.Task:
        """fire-and-forget 唯一入口（经 TaskRegistry；禁止裸 create_task）。"""
        return self._registry.spawn(coro, name=name)

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
        # Phase4：task_models 规范器接线（历史任意形态 → 全任务齐全，内存态；
        # 不写回配置文件——清洗结果只影响本次运行的候选链。v6.20.3 任务清单
        # 补入 embedding，嵌入绑定不再被每次加载剥掉）
        self.config["task_models"] = modelbind.normalize_task_models(
            self.config.get("task_models")
        )
        logger.info(
            f"maisoul v6.25.1 已加载：模式={self.config['mode']} bot={self.config['bot_name']} "
            f"触发模式={self.config.get('reply_trigger_mode', 'frequency')} "
            f"talk_value={self.config.get('talk_value', 1.0)} "
            f"错字={'开' if self.config.get('typo_enable', True) else '关'} 管家桥="
            f"{'开' if self.config.get('maid_bridge', True) else '关'}"
        )
        # M10：观察账本后台 writer（emit 只入队，落库经 to_thread 移出事件循环）；
        # 经 TaskRegistry 发起（create_task 唯一入口约束，v6.18.2）
        self.monitor.start_writer(self._registry)
        # M11：错字引擎预热——首次构建要遍历两万汉字逐个 pinyin() + 读字频表
        # + jieba 词典，内联在首条回复的发送路径上会卡秒级；装载时后台线程
        # 提前完成，发送路径只取现成实例
        if self.config.get("typo_enable", True):
            from .core import typo as _typo

            async def _preheat():
                try:
                    await asyncio.to_thread(_typo.get_typo_generator, self.config)
                except Exception:
                    logger.debug(
                        "maisoul: 错字引擎预热失败（将在首次使用时构建）", exc_info=True
                    )

            self._spawn(_preheat(), name="typo_preheat")
        webui_routes.register_webui(
            self.context, self.config, self.states, self.learning_store, self.monitor
        )

    def _migrate_legacy_nicknames(self):
        """v6.3 前别名拆在 nicknames（提及检测）里：合并进统一的 aliases，避免丢词。"""
        legacy = [
            str(n).strip()
            for n in (self.config.get("nicknames") or [])
            if str(n).strip()
        ]
        if not legacy:
            return
        aliases = [
            str(a).strip() for a in (self.config.get("aliases") or []) if str(a).strip()
        ]
        merged = aliases + [n for n in legacy if n not in aliases]
        self.config["aliases"] = merged
        try:
            self.config.save_config()
            logger.info(f"maisoul: 旧 nicknames 已合并进 aliases → {merged}")
        except Exception:
            logger.debug("maisoul: 别名迁移保存失败（内存已生效）", exc_info=True)

    # ------------------------------------------------------------------ #
    # 门控钩子：聊天消息评分（低优先级 = 在其他被动插件之后运行；群聊+私聊全接管）  #
    # ------------------------------------------------------------------ #
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=-1000)
    async def on_group_message(self, event: AstrMessageEvent):
        await gating._process_chat(self, event, is_group=True)

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=-1000)
    async def on_private_message(self, event: AstrMessageEvent):
        await gating._process_chat(self, event, is_group=False)

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req):
        await native.on_llm_request_impl(self, event, req)

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp):
        await native.on_llm_response_impl(self, event, resp)

    @filter.command("maisoul")
    async def maisoul_cmd(self, event: AstrMessageEvent):
        async for r in admin.maisoul_cmd_impl(self, event):
            yield r

    @filter.llm_tool("fetch_chat_history")
    async def fetch_chat_history(
        self, event: AstrMessageEvent, keyword: str = "", limit: int = 20
    ) -> str:
        """获取当前会话中比上下文窗口更早的聊天记录原文（群聊为当前群）。
        当你需要回顾更早聊过的话题、某人之前说过什么、或查一个旧消息时调用。
        注意：图片/表情消息在记录中只有"[图片/表情]"占位，关键词搜不到图片内容。

        Args:
            keyword(string): 可选关键词，只返回内容包含该关键词的记录（找特定话题时用；图片内容无法搜索）
            limit(number): 最多返回条数，默认 20，上限 50
        """
        from .core import history as _history
        from .core.states import session_key

        gid = session_key(event)
        st = self.states.get(gid)
        try:
            is_group = bool(event.get_group_id())
        except Exception:
            is_group = False
        base = int(
            self.config.get(
                "max_private_context_size" if not is_group else "max_context_size",
                60 if not is_group else 40,
            )
        )
        # 稳定窗与 planner 同口径（坑 31：窗口 = max(base, base×2)）；
        # 排除集 = planner 本轮真实可见消息（稳定窗 included + 待排水
        # pending，v6.20.1：按 buffer 条数硬排会与分析占坑的合并流窗口
        # 错位，中间产生模型取不到的盲区）
        window = max(base, base * 2)
        records = list(st.buffer)
        pl = st.planner_state()
        seen = _history.planner_seen_ids(
            records, pl.analysis_log, window, pl.last_cycle_ts, is_group
        )
        picked = _history.fetch_history_slice(records, seen, keyword, limit)
        outside_total = max(0, len(records) - len(seen))
        return _history.render_history_result(picked, outside_total, is_group)

    async def terminate(self):
        # M6：任务必有主——先取消并等待全部在飞任务（planner 循环/空窗重查/
        # wait 续轮/学习器），再释放监控库连接池；顺序不可反，否则任务会在
        # 已关闭的连接/旧状态对象上继续跑。WebUI 路由框架无注销接口，热重载
        # 时同路由重注册即替换，无泄漏。
        await self._registry.cancel_and_wait_all(timeout=5.0)
        await self.monitor.stop_writer()  # M10：冲刷残余事件后再关连接池
        self.monitor.close()
        logger.info("maisoul v6.25.1 已卸载")
