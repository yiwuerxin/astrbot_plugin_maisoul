"""聊天工具桥 —— 聊天 LLM 的工具暴露策略与执行

暴露策略（用户设计）：
- MaiBot 原生内置工具的 AstrBot 等价物 → 以同等待遇直接暴露给聊天 LLM
  （如 MaiBot send_emoji ↔ astrbot_plugin_stealer 的 send_meme）
- MaiBot 没有的能力 → 对聊天 LLM 隐藏，统一走 call_maid 管家交给 AstrBot agent
"""

import asyncio
import time
from types import SimpleNamespace

from astrbot.api import logger

# MaiBot builtin_tool → AstrBot 等价工具名（用于 AGENTS.md 同步维护）


# 两步制工具的前置依赖必须一同暴露（坑 40：stealer 的 search_meme → send_meme，
# 候选列表挂在 event._emoji_turn_state 上；只暴露 send_meme 而缺 search_meme，
# 模型拿到的描述指向一个永远找不到的工具，表情包路径直接死局）
TOOL_DEPENDENCIES: dict[str, list[str]] = {
    "send_meme": ["search_meme"],
}


# planner deferred 池排除集：MaiBot 内置工具的等价物已由 5 个可见内置工具覆盖
# （send_emoji 内部即驱动 search→send 两步制）。再入池会让模型同一轮走两条路
# 各发一次表情（实测双发），违背"planner 唯一干活者"分工（坑 29）。
# 独立模式 chat_toolset 不受影响（它没有内置 send_emoji，两步制对必须完整）。
DEFERRED_EXCLUDE: set = {"send_meme", "search_meme"}


def complete_tool_deps(names: list) -> list:
    """把列表中工具的前置依赖补进列表（去重，保持原顺序）。"""
    out = [str(n).strip() for n in names if str(n).strip()]
    for n in list(out):
        for dep in TOOL_DEPENDENCIES.get(n, []):
            if dep not in out:
                out.append(dep)
    return out


def build_chat_toolset(context, cfg):
    """构建聊天 LLM 可见的工具集：chat_tools 等价物（含前置依赖补全）+ call_maid（若开）。"""
    try:
        from astrbot.core.agent.tool import ToolSet

        mgr = context.get_llm_tool_manager()
        tool_set = ToolSet()

        exposed = []
        for name in complete_tool_deps(cfg.get("chat_tools") or []):
            tool = mgr.get_func(name)
            if tool is None:
                continue
            tool_set.add_tool(tool)
            exposed.append(name)

        if cfg.get("maid_bridge", True):
            maid = mgr.get_func("call_maid")
            if maid is not None:
                tool_set.add_tool(maid)
                exposed.append("call_maid")

        if tool_set.empty():
            return None
        logger.debug(f"maisoul 聊天工具集: {exposed}")
        return tool_set
    except Exception:
        logger.debug("maisoul: 构建聊天工具集失败", exc_info=True)
        return None


def list_deferred_tools(context, cfg) -> list[dict]:
    """planner 的 deferred 工具池（对齐 MaiBot「第三方工具默认 deferred」）：
    chat_tools 等价物 + call_maid（若开）。返回 [{name, description, tool}]，
    经 tool_search 发现后由 planner 循环按名执行。"""
    out: list[dict] = []
    try:
        mgr = context.get_llm_tool_manager()
        names = complete_tool_deps(cfg.get("chat_tools") or [])
        if cfg.get("maid_bridge", True):
            names.append("call_maid")
        names = [n for n in names if n not in DEFERRED_EXCLUDE]
        seen: set[str] = set()
        for name in names:
            if not name or name in seen:
                continue
            tool = mgr.get_func(name)
            if tool is None:
                continue
            # builtin 工具带配置激活条件（如 web_search_* 需 provider+key）：
            # 未激活的不进池——池里出现调不通的工具会教 planner 白烧轮次
            if not getattr(tool, "active", True):
                continue
            seen.add(name)
            out.append({"name": name,
                        "description": str(getattr(tool, "description", "") or ""),
                        "tool": tool})
    except Exception:
        logger.debug("maisoul: 构建 deferred 工具池失败", exc_info=True)
    return out


class SyntheticEvent:
    """planner 定时循环里没有原始 event 时的最小替身（unified_msg_origin + get_extra）。

    plugins_name=None：钩子注册表不过滤（全部 handler 可见）——仅在极少数
    无任何真实 event 的场景兜底，正常路径应复用 PlannerState.last_event。"""

    plugins_name = None

    def __init__(self, umo, send_message=None):
        self.unified_msg_origin = umo
        self.send_message = send_message

    def get_extra(self, key, default=None):
        return default


def _result_text(r) -> str:
    contents = getattr(r, "content", None)
    if contents:
        texts = [str(getattr(c, "text", "") or "") for c in contents]
        joined = "\n".join(t for t in texts if t)
        if joined:
            return joined
    return str(r or "")


async def call_llm_tool(context, event, tool, args: dict | None = None,
                        timeout: int = 120) -> str:
    """按 AstrBot 原生 agent 的路径执行一个 llm_tool（FunctionToolExecutor.execute）。

    装饰器注册的插件工具（如 call_maid/send_meme）不能直接 tool.call()，
    必须走 _execute_local → call_local_llm_tool 的 handler(event, **kwargs) 路径。
    核心 builtin 工具（web_search_tavily 等 FunctionTool 子类）执行时还要经
    run_context.context.context.get_config(umo) 读 provider_settings——所以
    内层必须同时携带 event 与 astrbot Context，只塞 event 会 AttributeError。
    """
    from astrbot.core.agent.run_context import ContextWrapper
    from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor

    wrapper = ContextWrapper(
        context=SimpleNamespace(event=event, context=context))
    out: list[str] = []
    agen = FunctionToolExecutor.execute(tool=tool, run_context=wrapper, **(args or {}))
    try:
        while True:
            r = await asyncio.wait_for(anext(agen), timeout=timeout)
            if r is None:
                continue
            out.append(_result_text(r))
    except StopAsyncIteration:
        pass
    return "\n".join(x for x in out if x)


async def exec_tool_calls(context, event, resp) -> str:
    """执行模型发起的工具调用（一轮），走 AstrBot 原生工具管理器。"""
    out = []
    try:
        mgr = context.get_llm_tool_manager()
        names = list(getattr(resp, "tools_call_name", None) or [])
        args_list = list(getattr(resp, "tools_call_args", None) or [])
        for i, name in enumerate(names):
            args = args_list[i] if i < len(args_list) and isinstance(args_list[i], dict) else {}
            tool = mgr.get_func(name)
            if tool is None:
                out.append(f"{name}: 工具不存在")
                continue
            try:
                result = await call_llm_tool(context, event, tool, args)
                out.append(f"{name}: {result[:500] or '（无输出）'}")
            except Exception as e:
                out.append(f"{name}: 执行失败 {e}")
    except Exception as e:
        logger.warning(f"maisoul 工具桥异常: {e}")
    return "\n".join(out)


MAID_BRIDGE_PROMPT = (
    "\n你有一位管家代理（call_maid 工具）：当聊天里出现需要查询资料、"
    "搜索、计算或执行任务才能回答的请求时，调用它并把任务描述写进 "
    "request_text；拿到结果后用你自己的口吻转达给群友。纯闲聊不要调用。"
    "\n你还有 send_meme 工具（表情包）：想在回复里带表情包时调用它，"
    "用情绪或描述选图；不需要时不用调用。"
)


def list_astrbot_tools(context) -> list[dict]:
    """列出 AstrBot 全部可用 LLM 工具（对齐官方 ToolsService.get_tool_list 的取数与序列化）。"""
    from astrbot.core.agent.mcp_client import MCPTool
    from astrbot.core.star import star_map

    mgr = context.get_llm_tool_manager()
    tools = list(mgr.func_list)
    existing_names = {t.name for t in tools}
    for tool in mgr.iter_builtin_tools():
        if tool.name not in existing_names:
            tools.append(tool)

    out = []
    for tool in tools:
        if mgr.is_builtin_tool(tool.name):
            origin, origin_name = "builtin", "AstrBot Core"
        elif isinstance(tool, MCPTool):
            origin, origin_name = "mcp", str(getattr(tool, "mcp_server_name", "") or "")
        elif tool.handler_module_path and star_map.get(tool.handler_module_path):
            origin, origin_name = "plugin", str(star_map[tool.handler_module_path].name)
        else:
            origin, origin_name = "unknown", "unknown"
        out.append({
            "name": tool.name,
            "description": tool.description,
            "active": bool(getattr(tool, "active", True)),
            "origin": origin,
            "origin_name": origin_name,
        })
    return out


def list_astrbot_skills() -> list[dict]:
    """列出 AstrBot 技能库（对齐 astr_main_agent 的 SkillManager().list_skills() 取数）。"""
    from astrbot.core.skills.skill_manager import SkillManager

    sm = SkillManager()
    return [
        {
            "name": s.name,
            "description": s.description,
            "active": s.active,
            "source_type": s.source_type,
            "source_label": s.source_label,
            "plugin_name": s.plugin_name,
            "path": s.path,
        }
        for s in sm.list_skills(active_only=False, runtime="local")
    ]


_SKILLS_CACHE: dict[tuple, tuple[float, str]] = {}  # (技能集指纹) → (ts, 块文本)


def build_skills_block(cfg) -> str:
    """把 chat_skills 选中的技能按 AstrBot 原生 build_skills_prompt 注入聊天系统提示词。

    与 astr_main_agent 注入主 agent 的方式一致：active_only=True、runtime="local"。
    M13：按技能集指纹缓存 60s——每次 replyer 都新建 SkillManager 扫描技能
    目录是纯浪费；短 TTL 兼容运行中安装/卸载技能的可见性。
    """
    names = {str(n).strip() for n in (cfg.get("chat_skills") or []) if str(n).strip()}
    if not names:
        return ""
    key = tuple(sorted(names))
    now = time.time()
    hit = _SKILLS_CACHE.get(key)
    if hit is not None and now - hit[0] < 60:
        return hit[1]
    block = _build_skills_block_uncached(names)
    if len(_SKILLS_CACHE) > 16:
        _SKILLS_CACHE.clear()
    _SKILLS_CACHE[key] = (now, block)
    return block


def _build_skills_block_uncached(names: set) -> str:
    try:
        from astrbot.core.skills.skill_manager import SkillManager, build_skills_prompt

        skills = [s for s in SkillManager().list_skills(active_only=True, runtime="local")
                  if s.name in names]
        if not skills:
            return ""
        return f"\n{build_skills_prompt(skills)}\n"
    except Exception:
        logger.debug("maisoul: 技能注入失败", exc_info=True)
        return ""
