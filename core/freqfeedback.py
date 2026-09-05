"""频率窗口反馈（P-E，GOAL Phase 3；对齐 MaiBot 发言频率的自适应回压）。

10 分钟滚动窗口统计自发消息数，映射为期望概率乘数 [0.2, 5.0] 分段线性：
安静（0 条）→ ×5.0 鼓励开口；达标 → ×1.0 中性；超两倍 → ×0.2 压制刷屏。
近 5 分钟已发过半数（超速）时只降不升——刚刚说太多话时即使整窗不超也
不再加成。乘进 scoring 的频率倍率（叠加而非替换）。纯函数，可单测。
"""

from __future__ import annotations

WINDOW_SECONDS = 600.0
RECENT_SECONDS = 300.0


def frequency_feedback_factor(st, cfg, now: float | None = None) -> tuple[float, str]:
    """返回 (乘数, 摘要)。st 需提供 recent_self_count(seconds)（GroupState 自带）。

    expected：窗口期望自发条数（freq_feedback_expected，默认 6，≥1）。
    """
    if not bool(cfg.get("freq_feedback_enable", False)):
        return 1.0, ""
    expected = max(1.0, float(cfg.get("freq_feedback_expected", 6) or 6))
    cnt = float(st.recent_self_count(WINDOW_SECONDS))
    ratio = cnt / expected
    if ratio <= 0:
        factor = 5.0
    elif ratio < 1.0:
        factor = 5.0 - 4.0 * ratio  # 5.0 → 1.0
    elif ratio < 2.0:
        factor = 1.0 - 0.8 * (ratio - 1.0)  # 1.0 → 0.2
    else:
        factor = 0.2
    # 近 5 分钟超速（已发 ≥ 期望的一半）：只降不升
    if factor > 1.0 and st.recent_self_count(RECENT_SECONDS) >= expected / 2:
        factor = 1.0
    return factor, f"频率反馈×{factor:.2f}（10min {cnt:.0f}/{expected:.0f}）"
