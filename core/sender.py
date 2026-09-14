"""拟人发送层 —— 对齐 MaiBot 发送路径

每段消息发送前 sleep calculate_typing_time(该段)（×typing_speed），
错字/分句/条数上限由 postprocess.process_response_segments 完成。
"""

import asyncio

from .postprocess import calculate_typing_time, process_response_segments

# 打字拟人总时长上限（maisoul 扩展，v6.28.0）：MaiBot 同样无上限——超长
# 回复 + 低 typing_speed 的组合会把发送路径阻塞分钟级；上限保住拟人性
# 不牺牲可用性（有意偏离 MaiBot，登记见 FIDELITY §8）
MAX_TOTAL_TYPING_SECONDS = 30.0


def cap_total_delay(
    delays: list[float], budget: float = MAX_TOTAL_TYPING_SECONDS
) -> list[float]:
    """逐段延迟按总预算钳制（从前往后消耗，耗尽后不再等待）。纯函数可单测。"""
    out: list[float] = []
    remaining = max(0.0, float(budget))
    for d in delays:
        capped = min(max(0.0, float(d)), remaining)
        remaining -= capped
        out.append(capped)
    return out


async def send_humanlike(send, answer: str, cfg, typing_mult: float = 1.0) -> list[str]:
    """后处理 → 逐段打字延迟发送。返回实际发出的段文本。

    首段不等待（对齐 MaiBot reply 工具 typing=index>0），第 2 段起按
    calculate_typing_time(该段)×typing_speed 延迟；总延迟受
    MAX_TOTAL_TYPING_SECONDS 预算钳制（v6.28.0）。
    quote_previous（错字纠正引用上一条）在 AstrBot 侧尚未接引用回复原语，
    目前按普通文本发送；接通后由调用方用 Reply 组件处理。
    """
    typing_speed = float(cfg.get("typing_speed", 1.0))
    segments = process_response_segments(answer, cfg)
    delays = cap_total_delay(
        [
            calculate_typing_time(seg.text, typing_speed=typing_speed) * typing_mult
            for seg in segments[1:]
        ]
    )
    sent: list[str] = []
    for index, seg in enumerate(segments):
        if index > 0:
            delay = delays[index - 1]
            if delay > 0:
                await asyncio.sleep(delay)
        await send(seg.text)
        sent.append(seg.text)
    return sent
