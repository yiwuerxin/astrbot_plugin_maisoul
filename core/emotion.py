"""情绪 VA 模型（P-B，GOAL Phase 3）——纯规则零 LLM 成本。

机制（对齐 MaiBot emotion 语义；研究文档缺失，锚点数值为本项目口径，
公式按 GOAL 规格实现）：
- 12 情绪词 → (valence ±, arousal 0~1) 增量表；回复后由 replyer 的情绪
  标注驱动更新（标签在发送前剥离，聊天内容不含机器痕迹）；
- 动量：连续同向增益 ×1.01^n、异向 ×0.99^n（n=连续次数）；
- 每分钟向基线 (0,0) 做 exp 衰减（读时惰性结算）；
- (v,a) 按 12 锚点最近距离转情绪文本注入 prompt；
- 打字延迟 ×1.5^arousal（激昂度越高，段间停顿越长）。

§6.6 情绪-关系耦合（v6.25.0）：EmotionFeedback 连续同向情绪累积器 +
FEEDBACK_GAIN 增益表，经 pipeline/emo_facade 向心弦插件提供数值读数；
反向的情绪事件注入走 EmotionState.apply(intensity=)。

全部内存态、纯函数（EmotionState/EmotionFeedback 为纯数据 + 方法），可单测。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# 12 情绪词 → (valence delta, arousal delta)
# 委屈/期待/安心三词 v6.25.0 补齐（N9：12 锚点中原先只有 9 个有增量定义，
# 这三词被 apply 恒视为平静 no-op，标签-驱动闭环存在死角）
EMOTION_DELTAS: dict[str, tuple[float, float]] = {
    "愤怒": (-0.60, 0.80),
    "厌恶": (-0.50, 0.40),
    "恐惧": (-0.70, 0.60),
    "悲伤": (-0.50, -0.30),
    "平静": (0.00, 0.00),
    "好奇": (0.20, 0.40),
    "开心": (0.60, 0.50),
    "兴奋": (0.80, 0.80),
    "喜爱": (0.70, 0.30),
    "委屈": (-0.35, 0.45),
    "期待": (0.30, 0.55),
    "安心": (0.25, 0.15),
}

# 12 锚点：(情绪词, v, a)——标签映射取最近欧氏距离
EMOTION_ANCHORS: list[tuple[str, float, float]] = [
    ("愤怒", -0.80, 0.90),
    ("厌恶", -0.60, 0.50),
    ("恐惧", -0.85, 0.70),
    ("悲伤", -0.70, 0.10),
    ("委屈", -0.35, 0.30),
    ("平静", 0.00, 0.00),
    ("好奇", 0.30, 0.45),
    ("开心", 0.65, 0.55),
    ("兴奋", 0.90, 0.85),
    ("喜爱", 0.80, 0.35),
    ("期待", 0.45, 0.60),
    ("安心", 0.40, 0.10),
]

_DECAY_PER_MINUTE = 0.10  # 每分钟 exp 衰减率（半衰期 ≈ 7 分钟）
_MOMENTUM_UP = 1.01
_MOMENTUM_DOWN = 0.99

# §6.6 情绪-关系耦合增益表（MaiBot positive_feedback 原参数），索引 |pfb|：
# 连续同向情绪事件越多，心弦侧同向好感增量放大/异向缩小的系数越大
FEEDBACK_GAIN: tuple[float, ...] = (1.0, 1.0, 1.1, 1.2, 1.4, 1.7, 1.9, 2.0)


@dataclass
class EmotionState:
    """单会话情绪（valence ∈ [-1,1]，arousal ∈ [0,1]）。"""

    v: float = 0.0
    a: float = 0.0
    streak_dir: int = 0  # 连续情绪方向（-1/0/+1）
    streak_n: int = 0  # 连续次数（动量指数）
    last_ts: float = 0.0
    history: list = field(default_factory=lambda: [])  # 最近情绪词（展示用，≤12）

    def _decay(self, now: float) -> None:
        """向基线 exp 衰减（读/更新前惰性结算）。"""
        if self.last_ts <= 0:
            self.last_ts = now
            return
        minutes = max(0.0, (now - self.last_ts) / 60.0)
        factor = math.exp(-_DECAY_PER_MINUTE * minutes)
        self.v *= factor
        self.a *= factor
        self.last_ts = now

    def apply(
        self, word: str, now: float, intensity: float = 1.0
    ) -> tuple[float, float]:
        """按情绪词更新（含动量）；返回更新后的 (v, a)。未知词视为平静。

        intensity ∈ [0,1] 线性缩放本次增量（§6.6 方向②外部事件注入用，
        极性判定按未缩放符号；缺省 1 = P-B 标注驱动的原行为）。"""
        self._decay(now)
        dv, da = EMOTION_DELTAS.get(str(word or "").strip(), (0.0, 0.0))
        try:
            k = max(0.0, min(1.0, float(intensity)))
        except (TypeError, ValueError):
            # 跨插件边界可能传来非数值强度（Sourcery #27）：按 0 处理而非
            # 抛异常——事件被接受但零强度（no-op），不炸调用方
            k = 0.0
        dv, da = dv * k, da * k
        direction = (dv > 0) - (dv < 0)
        # 先按旧 streak 判同/异向（Sourcery：换代后再比较会让异向也吃放大）
        same_direction = direction != 0 and self.streak_dir == direction
        first_emotion = direction != 0 and self.streak_dir == 0
        if direction != 0:
            self.streak_n = self.streak_n + 1 if same_direction else 1
            self.streak_dir = direction
        # 连续同向 ×1.01^n 放大、异向 ×0.99 收敛、首个情绪不缩放（n 截断防爆）
        n = min(self.streak_n, 50) if direction != 0 else 0
        if first_emotion:
            momentum = 1.0
        elif same_direction:
            momentum = _MOMENTUM_UP**n
        else:
            momentum = _MOMENTUM_DOWN
        self.v = max(-1.0, min(1.0, self.v + dv * momentum))
        self.a = max(0.0, min(1.0, self.a + da * momentum))
        word = str(word or "").strip() or "平静"
        self.history.append(word)
        del self.history[:-12]
        return self.v, self.a

    def read(self, now: float) -> tuple[float, float]:
        """结算衰减后的当前 (v, a)——跨插件读数的统一入口（不动 streak/历史）。"""
        self._decay(now)
        return self.v, self.a

    def label(self, now: float) -> str:
        """最近锚点情绪词（读时结算衰减）。"""
        self._decay(now)
        best, best_d = "平静", float("inf")
        for name, av, aa in EMOTION_ANCHORS:
            d = (self.v - av) ** 2 + (self.a - aa) ** 2
            if d < best_d:
                best, best_d = name, d
        return best

    def prompt_line(self, now: float) -> str:
        """注入 prompt 的情绪行（P-B：emotion_enable 开启时）。"""
        return f"你现在的情绪状态是「{self.label(now)}」（心情值 {self.v:+.2f}，激昂度 {self.a:.2f}），让语气自然地带上这份情绪。"

    def typing_multiplier(self, now: float | None = None) -> float:
        """打字延迟乘数 1.5^arousal。"""
        return 1.5**self.a


@dataclass
class EmotionFeedback:
    """连续同向情绪累积器（§6.6 positive_feedback 移植）。

    每次带极性的情绪事件把 pfb 向事件极性推一步：同向事件越推越远
    （累积），异向事件往回拉（缩小），统一为 pfb += sign，钳制 ±7；
    零极性词（平静等 valence 增量为 0）不计数。心弦侧按
    FEEDBACK_GAIN[|pfb|] 查表对同向好感增量做放大/异向缩小调制。
    挂 GroupState 随会话生命周期（内存态，不落盘）；无时间衰减——
    靠异向事件回拉 + valence 自身分钟级衰减兜底。
    """

    pfb: int = 0

    def observe(self, word: str) -> int:
        """记一次情绪事件（replyer 剥出的情绪标注）；返回更新后的 pfb。"""
        dv = EMOTION_DELTAS.get(str(word or "").strip(), (0.0, 0.0))[0]
        sign = (dv > 0) - (dv < 0)
        if sign == 0:
            return self.pfb
        self.pfb = max(-7, min(7, self.pfb + sign))
        return self.pfb

    def gain(self) -> float:
        """当前增益系数（FEEDBACK_GAIN[|pfb|]，调试/状态展示用）。"""
        return FEEDBACK_GAIN[min(abs(self.pfb), 7)]
