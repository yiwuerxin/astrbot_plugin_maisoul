"""多人格管理 —— MaiBot 没有的扩展能力

一个 maisoul 人格 = 一套三件套覆盖包（bot_name/personality/behavior_style/
reply_style，可选 aliases/group_chat_prompt）。aliases 与 MaiBot 的
alias_names 一致：单一列表同时用于身份行与提及检测。

主配置人格：人格库固定首行，人格名即主配置 bot_name（如"麦麦"），
find_persona 对该名字（或"主配置"）返回主配置本身，因此群里发
/persona 麦麦 也能切换到它。

解析优先级（resolve_active）：
1. 会话实时人格：兼容 astrbot_plugin_persona_switch —— 它通过
   conversation_manager.update_conversation(persona_id=...) 切换，
   maisoul 用同一接口读 conv.persona_id，名字命中即生效
2. 静态群绑定：group_persona [{chat, name}]
3. 默认人格：default_persona
4. 主配置三件套（行为不变）
"""

from astrbot.api import logger

PERSONA_FIELDS = ("bot_name", "personality", "behavior_style", "reply_style",
                  "group_chat_prompt", "aliases")

MAIN_PERSONA_KEY = "主配置"


def chat_id_match(chat_id: str, target: str) -> bool:
    """群号匹配：* 通配全部，否则精确或后缀互匹配。"""
    target = str(target or "").strip()
    if not target:
        return False
    if target == "*":
        return True
    chat_id = str(chat_id or "")
    return bool(chat_id) and (target == chat_id or chat_id.endswith(target) or target.endswith(chat_id))


def main_persona(cfg) -> dict:
    """主配置包装成普通人格：全部字段取主配置本值，名字即 bot_name。"""
    bot_name = str(cfg.get("bot_name") or "麦麦").strip() or "麦麦"
    p = {"name": bot_name}
    for f in PERSONA_FIELDS:
        v = cfg.get(f)
        if v:
            p[f] = v
    return p


def find_persona(cfg, name: str) -> dict | None:
    name = str(name or "").strip()
    if not name:
        return None
    for p in (cfg.get("personas") or []):
        if isinstance(p, dict) and str(p.get("name") or "").strip() == name:
            return p
    # 主配置人格：名字即机器人昵称（如"麦麦"），库内同名人格优先
    bot_name = str(cfg.get("bot_name") or "").strip()
    if name in (bot_name, MAIN_PERSONA_KEY):
        return main_persona(cfg)
    return None


def list_persona_names(cfg) -> list[str]:
    return [str(p.get("name") or "").strip() for p in (cfg.get("personas") or [])
            if isinstance(p, dict) and str(p.get("name") or "").strip()]


def overlay(cfg, persona: dict) -> dict:
    """人格字段覆盖主配置，生成有效配置视图（浅拷贝，人格非空字段优先）。"""
    view = dict(cfg)
    for f in PERSONA_FIELDS:
        v = persona.get(f)
        if isinstance(v, str) and v.strip():
            view[f] = v
        elif isinstance(v, list) and v:
            view[f] = v
    view["active_persona"] = str(persona.get("name") or "").strip()
    return view


async def resolve_active(context, cfg, gid: str, umo: str) -> tuple[dict, str]:
    """返回 (有效配置视图, 生效人格名)。任何失败都安全回退主配置。"""
    # 1) 会话实时人格（兼容 persona_switch）
    if cfg.get("follow_persona_switch", True):
        try:
            conv_mgr = context.conversation_manager
            cid = await conv_mgr.get_curr_conversation_id(umo)
            if cid:
                conv = await conv_mgr.get_conversation(umo, cid)
                pid = str(getattr(conv, "persona_id", "") or "").strip()
                if pid:
                    p = find_persona(cfg, pid)
                    if p:
                        return overlay(cfg, p), pid
        except Exception:
            logger.debug("maisoul: 读取会话人格失败", exc_info=True)

    # 2) 静态群绑定
    for m in (cfg.get("group_persona") or []):
        if not isinstance(m, dict):
            continue
        name = str(m.get("name") or "").strip()
        if name and chat_id_match(gid, str(m.get("chat") or "")):
            p = find_persona(cfg, name)
            if p:
                return overlay(cfg, p), name

    # 3) 默认人格
    dp = str(cfg.get("default_persona") or "").strip()
    if dp:
        p = find_persona(cfg, dp)
        if p:
            return overlay(cfg, p), name_of(p)

    # 4) 主配置兜底
    return dict(cfg), f"主配置·{str(cfg.get('bot_name') or '麦麦').strip()}"


def name_of(persona: dict) -> str:
    return str(persona.get("name") or "").strip()
