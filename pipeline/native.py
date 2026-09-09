"""协作（native）模式钩子体（M7 自 main.py 机械拆出，行为零变化）。"""

from __future__ import annotations

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..core import personas, prompt
from ..core.constants import OUTPUT_INSTRUCTION
from ..core.states import session_key
from .events_util import _emit_sent


async def on_llm_request_impl(P, event: AstrMessageEvent, req):
    if not event.get_extra("maisoul_triggered"):
        return
    if not req or not hasattr(req, "system_prompt"):
        return
    gid = session_key(event)
    eff_cfg, pname = await personas.resolve_active(
        P.context, P.config, gid, event.unified_msg_origin
    )
    inject = (
        f"\n\n【maisoul 麦麦三件套（本群主动发言由意愿评分触发｜人格={pname}）】\n"
        f"{prompt.build_identity(eff_cfg)}\n"
        f"{prompt.select_reply_style(eff_cfg)}\n"
        f"{prompt.build_preset_dialogues_block(eff_cfg)}"
        f"{OUTPUT_INSTRUCTION}"
    )
    req.system_prompt = (req.system_prompt or "") + inject


async def on_llm_response_impl(P, event: AstrMessageEvent, resp):
    try:
        if event.get_extra("maisoul_eco_resp"):
            return  # 生态注入桥自己触发的链，防回声重复记录
        if P.config["mode"] == "independent":
            return
        answer = (getattr(resp, "completion_text", "") or "").strip()
        if not answer:
            return
        # 会话键与 _process_chat 对齐（群=群号，私聊=发送者ID），否则私聊回声记进另一个状态
        gid = session_key(event)
        st = P.states.get(gid)
        segs = [s for s in answer.split("\n") if s.strip()]
        st.record_self_reply("", segs, P.config["bot_name"])
        # 麦麦观察：native/逃生舱路径的回复经回声钩子补 message.sent——
        # 否则聊天流只有用户发言、没有麦麦的回复记录（生产反馈的真实缺口）
        for seg in segs:
            _emit_sent(P, gid, seg, "", "reply", event)
    except Exception:
        logger.debug("maisoul: 回声记录失败", exc_info=True)


# ------------------------------------------------------------------ #
# 管理指令                                                             #
