"""任务级模型绑定宿主（M7 自 main.py 机械拆出，行为零变化）。

provider 解析/策略挑选/降级链调用；_task_model_rr 原为插件类属性
（跨实例共享），拆出后为模块级——语义不变（balance 轮转全局计数）。
"""

from __future__ import annotations

from astrbot.api import logger

from ..core import modelbind

# balance 策略轮转计数器（原插件类属性，跨实例共享语义保持）
_task_model_rr: dict[str, int] = {}


def _resolve_bound_model(P, cand: dict):
    """{provider, model} → (provider实例, model名)；provider 不存在返回 None。

    provider 可填源名（如 google_gemini_openai）：精确 id 未命中时取该源
    下任一已启用条目承载本次调用（同源条目共享 base_url/key，model 按次覆盖）。
    """
    try:
        inst_map = getattr(P.context, "provider_manager", None).inst_map or {}
    except Exception:
        return None
    pid = str(cand.get("provider"))
    inst = inst_map.get(pid)
    if inst is None:
        for key, candidate in inst_map.items():
            if key.split("/", 1)[0] == pid:
                inst = candidate
                break
    if inst is None:
        return None
    return inst, str(cand.get("model"))

_task_model_rr: dict[str, int] = {}  # balance 轮转计数器

def _pick_task_model(P, task: str, cfg):
    """按策略选主候选（小任务子调用用：无降级链）。None = 跟随默认 Provider。"""
    candidates = modelbind.task_model_candidates(cfg, task)
    if not candidates:
        return None
    strategy = modelbind.task_model_strategy(cfg, task)
    pick = modelbind.pick_model(candidates, strategy, _task_model_rr, task)
    return _resolve_bound_model(P, pick) if pick else None

def _embedding_provider(P, eff_cfg):
    """embedding 任务绑定的嵌入 Provider（vector_intent 表达召回用）。

    嵌入 Provider 走 AstrBot 的 EmbeddingProvider 体系（get_embeddings），
    同样登记在 inst_map：绑定解析复用 _pick_task_model；未绑定时取第一个
    可用嵌入实例，无则 None（调用方回落 legacy 抽样）。
    """
    try:
        insts = list(getattr(P.context.provider_manager,
                             "embedding_provider_insts", None) or [])
    except Exception:
        return None
    if not insts:
        return None
    resolved = _pick_task_model(P, "embedding", eff_cfg)
    return resolved if resolved is not None else insts[0]

async def _task_text_chat(P, task: str, cfg, **kwargs):
    """按任务绑定调 text_chat：策略选主候选，异常时依次降级链上后续候选；
    无绑定走 AstrBot 当前默认 Provider。"""
    provider = P.context.get_using_provider()
    candidates = modelbind.task_model_candidates(cfg, task)
    if not candidates:
        return await provider.text_chat(**kwargs)
    strategy = modelbind.task_model_strategy(cfg, task)
    chain = modelbind.build_model_chain(candidates, strategy,
                                        _task_model_rr, task)
    last_err: Exception | None = None
    for cand in chain:
        resolved = _resolve_bound_model(P, cand)
        if resolved is None:
            logger.warning(f"maisoul: 任务 {task} 绑定的 provider "
                           f"{cand.get('provider')} 不存在，跳过")
            continue
        inst, model = resolved
        try:
            return await inst.text_chat(model=model, **kwargs)
        except Exception as e:
            last_err = e
            logger.warning(f"maisoul: 任务 {task} 模型 {cand.get('provider')}/{model} "
                           f"调用失败，尝试下一候选: {e}")
    if last_err is not None:
        raise last_err
    return await provider.text_chat(**kwargs)
