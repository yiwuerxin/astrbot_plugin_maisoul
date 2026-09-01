"""拟人发送层 —— 对齐 MaiBot 发送路径

每段消息发送前 sleep calculate_typing_time(该段)（×typing_speed），
错字/分句/条数上限由 postprocess.process_response_segments 完成。
"""

import asyncio

from .postprocess import calculate_typing_time, process_response_segments


async def send_humanlike(send, answer: str, cfg) -> list[str]:
    """后处理 → 逐段打字延迟发送。返回实际发出的段文本。

    首段不等待（对齐 MaiBot reply 工具 typing=index>0），第 2 段起按
    calculate_typing_time(该段)×typing_speed 延迟。
    quote_previous（错字纠正引用上一条）在 AstrBot 侧尚未接引用回复原语，
    目前按普通文本发送；接通后由调用方用 Reply 组件处理。
    """
    typing_speed = float(cfg.get("typing_speed", 1.0))
    segments = process_response_segments(answer, cfg)
    sent: list[str] = []
    for index, seg in enumerate(segments):
        if index > 0:
            delay = calculate_typing_time(seg.text, typing_speed=typing_speed)
            if delay > 0:
                await asyncio.sleep(delay)
        await send(seg.text)
        sent.append(seg.text)
    return sent
