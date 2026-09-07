"""管理指令 /maisoul 逻辑（M7 自 main.py 机械拆出，行为零变化）。

异步生成器：逐条 yield plain_result，由 main.py 钩子薄壳转发。
"""

from __future__ import annotations

import time

from astrbot.api.event import AstrMessageEvent

from ..core import trigger
from .planner_host import _planner_cycle


async def maisoul_cmd_impl(P, event: AstrMessageEvent):
    # 管理指令鉴权：开关/模式切换会改持久化配置、sim 驱动完整 LLM 管线——
    # 仅管理员可用（event.is_admin 即 role=="admin"）。WebUI 聊天页的 webchat
    # 事件 role 恒 member，但整条链路在 dashboard JWT 鉴权墙内，放行以便调试。
    if not (event.is_admin() or str(event.get_platform_name() or "") == "webchat"):
        yield event.plain_result("maisoul 指令仅管理员可用")
        return
    raw = (event.message_str or "").strip()
    # waking_check 只剥唤醒前缀"/"，CommandFilter 匹配不改写 message_str——
    # 此处 raw 仍带命令名"maisoul"，子命令解析先剥掉它（否则 sim 等子命令
    # 全部落到状态分支，v6.13.4 修复）
    if raw.lower().startswith("maisoul"):
        raw = raw[len("maisoul") :].strip()
    arg = raw.lower()
    if arg.startswith("sim ") and raw[4:].strip():
        # 调试：把文本灌进完整管线（门控→planner→replyer），不依赖群聊适配器
        text = raw[4:].strip()
        st = P.states.get("sim")
        st.record_external(
            {
                "name": event.get_sender_name() or "测试者",
                "sid": str(event.get_sender_id()),
                "msg_id": "sim",
                "text": text,
                "at_bot": False,
                "reply_bot": False,
                "ts": time.time(),
            }
        )
        aliases = [str(a) for a in (P.config.get("aliases") or [])]
        bot_name = str(P.config["bot_name"])
        fired, detail, _ = trigger.should_trigger(
            st,
            P.config,
            at_bot=False,
            mentioned=any(k and k in text for k in [bot_name, *aliases]),
            text=text,
            aliases=aliases,
            bot_name=bot_name,
            platform="sim",
            chat_id="sim",
            is_group=False,
        )
        yield event.plain_result(f"[sim 门控] {detail}")
        if not fired:
            return
        pl = st.planner_state()
        gen = pl.begin_cycle()  # M3：sim 循环同样走代际守卫
        pl.agent_state = "running"
        pl.umo = event.unified_msg_origin
        pl.platform = "webchat"
        pl.is_group = False
        await _planner_cycle(
            P,
            event.unified_msg_origin,
            "webchat",
            "sim",
            st,
            False,
            event=event,
            gen=gen,
        )
        yield event.plain_result(
            f"[sim 完成] 人格={st.last_persona}，决策结果见上方发言/日志"
        )
        return
    if arg in ("on", "off"):
        P.config["enable"] = arg == "on"
        P.config.save_config()
        yield event.plain_result(f"maisoul 已{'启用' if arg == 'on' else '停用'}")
    elif arg in ("planner", "native", "independent"):
        P.config["mode"] = arg
        P.config.save_config()
        names = {
            "planner": "决策模式（maisaka Planner 循环决策，工具经 planner 调度）",
            "native": "协作模式（生成交给原生 agent，三件套注入）",
            "independent": "独立模式（麦麦流水线完全接管发言）",
        }
        yield event.plain_result(f"maisoul 已切换到 {names[arg]}")
    else:
        f = max(0.0, float(P.config.get("talk_value", 1.0) or 0.0))
        th = trigger.message_trigger_threshold(
            str(P.config.get("reply_trigger_mode", "frequency")), f
        )
        yield event.plain_result(
            f"maisoul v6.18.1 状态：{'运行中' if P.config['enable'] else '已停用'} | "
            f"模式={P.config['mode']} | bot={P.config['bot_name']}\n"
            f"触发模式={P.config.get('reply_trigger_mode', 'frequency')} "
            f"talk_value={f:.3f} 阈值={th}条消息 "
            f"错字={'开' if P.config.get('typo_enable', True) else '关'} 活跃群数={len(P.states)}\n"
            f"指令：/maisoul on|off | /maisoul planner|native|independent | /maisoul sim <文本>（WebUI 调试走完整管线）"
        )


# ------------------------------------------------------------------ #
# 工具函数                                                             #
