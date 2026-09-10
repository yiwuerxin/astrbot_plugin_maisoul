"""情绪 facade（§6.6 情绪-关系耦合的跨插件数值面）。

心弦获取方式（AstrBot 跨插件调用惯例，与 xinxian facade 同款）：

    star = context.get_registered_star("astrbot_plugin_maisoul")
    api = star.star_cls.api          # 即本类的实例
    fb = await api.get_feedback(group_id)      # 方向①：{"pfb", "valence"} | None
    ok = await api.apply_emotion_event(...)    # 方向②：外部情绪事件注入
    prov = await api.get_replyer_provider()    # 模型联动：replyer 绑定的 provider 实例 | None

只交换数值，不渲染任何提示词（P-H 教训：好感数据的唯一入口是生态注入桥，
拆 xinxian_link 的双重注入结论对情绪面同样适用）。开关关闭/会话不存在
返回 None/False，调用方（心弦适配器）自行 try/except 静默降级。
"""

from __future__ import annotations

import time

from astrbot.api import logger

from ..core.emotion import EMOTION_DELTAS
from ..core.states import StateManager


class EmotionFacade:
    """麦麦对外跨插件 API（情绪数值面 + replyer 模型联动）。"""

    def __init__(self, states: StateManager, config) -> None:
        self._states = states
        # config 传 AstrBotConfig 本体或 () -> config 的取值器；延迟读取，
        # 面板/配置文件热改开关即时生效，无需重启
        self._config = config
        # replyer 模型取值器（async () -> provider 实例 | None），main 装配时
        # bind——facade 不 import P/modelbind，保持纯边界可单测
        self._replyer_picker: object | None = None
        # replyer 最近一次成功调用实际服务的 provider 实例（modelbind_host
        # 成功路径回填）——"当前 reply 触发的模型"，且必然可用（刚成功过）；
        # 绑定链抽签（balance）可能落在已失效候选上，故只作冷启动回落
        self._last_replyer: object | None = None

    def bind_replyer_picker(self, picker) -> None:
        """装配期注入 replyer provider 取值器（main.py 接 modelbind 任务链）。"""
        self._replyer_picker = picker

    def note_replyer_used(self, prov) -> None:
        """记录 replyer 本次成功调用的 provider（modelbind_host 成功路径回填）。"""
        if prov is not None:
            self._last_replyer = prov

    async def get_replyer_provider(self) -> object | None:
        """replyer 当前使用的 LLM provider 实例（跨插件模型联动，v6.26.0）。

        优先返回最近一次 reply 实际成功服务的 provider（字面意义的"当前
        reply 触发的模型"，且必然可用）；冷启动（重启后尚无 reply）回落
        绑定链解析——modelbind 任务 "replyer" 按策略抽主候选 → 无绑定
        AstrBot 默认 provider。心弦评审据此跟随"麦麦用什么模型说话就用
        什么模型打分"（人格口径一致，也避免评审侧独立配置指向失效模型）。
        无可用 provider / 解析失败返回 None（调用方降级自己的链）。
        """
        if self._last_replyer is not None:
            return self._last_replyer
        picker = self._replyer_picker
        if picker is None:
            return None
        try:
            prov = await picker()
            return prov if prov is not None else None
        except Exception:
            # 联动是增强不是依赖：解析失败静默降级，调用方走自己的链
            logger.debug("maisoul: get_replyer_provider 解析失败", exc_info=True)
            return None

    def _cfg(self) -> dict:
        try:
            return self._config() if callable(self._config) else self._config
        except Exception:
            # 配置读不出来按全关处理（联动是增强，不是依赖）
            logger.debug("maisoul: emo_facade 读配置失败，按关闭处理", exc_info=True)
            return {}

    async def get_feedback(self, gid: str) -> dict | None:
        """方向①读数：{"pfb": int∈[-7,7], "valence": float∈[-1,1]}。

        emotion_enable / emotion_feedback_enable 任一关闭或会话不存在 → None。"""
        cfg = self._cfg()
        if not bool(cfg.get("emotion_enable", False)) or not bool(
            cfg.get("emotion_feedback_enable", False)
        ):
            return None
        st = self._states.peek(str(gid or ""))
        if st is None:
            logger.info(f"maisoul[obs] 情绪读数 gid={gid}: 会话不存在 -> None")
            return None
        v, _a = st.emotion.read(time.time())
        logger.info(
            f"maisoul[obs] 情绪读数 gid={gid}: pfb={st.emotion_feedback.pfb} "
            f"valence={v:+.2f} label={st.emotion.label(time.time())}"
        )
        return {"pfb": int(st.emotion_feedback.pfb), "valence": float(v)}

    async def apply_emotion_event(
        self, gid: str, word: str, intensity: float = 0.5
    ) -> bool:
        """方向②入口：外部情绪事件注入（如心弦好感等级跃迁）。

        word 不在情绪词表或 emotion_enable 关闭返回 False。会话不存在时
        创建（注入后情绪随会话驻留，等该群下次发言时生效进提示词）。"""
        if not bool(self._cfg().get("emotion_enable", False)):
            return False
        w = str(word or "").strip()
        if w not in EMOTION_DELTAS:
            return False
        try:
            k = max(0.0, min(1.0, float(intensity)))
        except (TypeError, ValueError):
            # 跨插件边界可能传来非数值强度：按 0 处理（事件接受但零强度），
            # EmotionState.apply 内有同款兜底，这里归一化后再传并落日志
            k = 0.0
        st = self._states.get(str(gid or ""))
        st.emotion.apply(w, time.time(), intensity=k)
        logger.info(
            f"maisoul[obs] 情绪事件注入 gid={gid} word={w} intensity={k:.2f} "
            f"-> 情绪态={st.emotion.label(time.time())}"
        )
        return True
