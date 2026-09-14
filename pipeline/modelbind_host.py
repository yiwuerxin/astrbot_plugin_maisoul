"""任务级模型绑定宿主（M7 自 main.py 机械拆出，行为零变化）。

provider 解析/策略挑选/降级链调用；_task_model_rr 原为插件类属性
（跨实例共享），拆出后为模块级——语义不变（balance 轮转全局计数）。
"""

from __future__ import annotations

from astrbot.api import logger

from ..core import modelbind

# balance 策略轮转计数器（原插件类属性，跨实例共享语义保持；
# v6.20.3 删掉 M7 拆分残留的第二处重复定义）
_task_model_rr: dict[str, int] = {}


def _resolve_bound_model(P, cand: dict):
    """{provider, model} → (provider实例, model名)；provider 不存在返回 None。

    provider 可填源名（如 google_gemini_openai）：精确 id 未命中时取该源
    下任一已启用条目承载本次调用（同源条目共享 base_url/key，model 按次覆盖）。
    """
    try:
        inst_map = getattr(P.context, "provider_manager", None).inst_map or {}
    except Exception:
        # 降级：provider 实例表不可达按未绑定（调用方回落默认 Provider）
        logger.debug("maisoul: provider 实例表不可达", exc_info=True)
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


def _pick_task_model(P, task: str, cfg):
    """按策略选主候选（小任务子调用用）。None = 跟随默认 Provider。

    主候选解析失败（provider 删/改名）时顺延降级链重试而非静默回落默认
    Provider（v6.28.0）——小任务（emoji/学习/嵌入）换模型=人格口径漂移且
    原先无任何日志；与 _task_text_chat 的降级链同待遇，全部失效才回落。
    """
    candidates = modelbind.task_model_candidates(cfg, task)
    if not candidates:
        return None
    strategy = modelbind.task_model_strategy(cfg, task)
    chain = modelbind.build_model_chain(candidates, strategy, _task_model_rr, task)
    for cand in chain:
        resolved = _resolve_bound_model(P, cand)
        if resolved is not None:
            return resolved
        logger.warning(
            f"maisoul: 任务 {task} 绑定的 provider {cand.get('provider')} "
            "不存在，尝试下一候选"
        )
    logger.warning(f"maisoul: 任务 {task} 绑定候选全部失效，本次跟随默认 Provider")
    return None


def _embedding_provider(P, eff_cfg):
    """embedding 任务绑定的嵌入 Provider（vector_intent 表达召回用）。

    嵌入 Provider 走 AstrBot 的 EmbeddingProvider 体系（get_embeddings），
    同样登记在 inst_map：绑定解析复用 _pick_task_model；未绑定时取第一个
    可用嵌入实例，无则 None（调用方回落 legacy 抽样）。
    """
    try:
        insts = list(
            getattr(P.context.provider_manager, "embedding_provider_insts", None) or []
        )
    except Exception:
        # 降级：嵌入实例表不可达按未绑定（调用方回落 legacy 抽样）
        logger.debug("maisoul: 嵌入实例表不可达", exc_info=True)
        return None
    if not insts:
        return None
    resolved = _pick_task_model(P, "embedding", eff_cfg)
    return resolved if resolved is not None else insts[0]


def _provider_model_label(inst, model: str) -> str:
    """本次调用实际服务的模型名：按次覆盖优先，空则取 provider 当前模型。"""
    return str(model or inst.get_model() or "")


def replyer_image_capable(P, cfg) -> bool:
    """replyer 识图门控（v6.21.0）：读 AstrBot 模型条目 modalities 的「图像」
    勾选，不看 enable_image_context 开关（开关只管 Planner 决策轮）。

    口径：replyer 任务实际可能落到的模型条目（绑定候选逐个解析；无绑定 =
    当前默认 Provider）**全部**勾了图像才算支持——_task_text_chat 的降级链
    会依次试候选，任一条目不支持时带图请求落到它上面就会失败，宁可整轮
    不附图；候选全部解析失败与无绑定同口径（回落默认 Provider）。
    """
    insts = []
    for cand in modelbind.task_model_candidates(cfg, "replyer"):
        resolved = _resolve_bound_model(P, cand)
        if resolved is not None:
            insts.append(resolved[0])
    if not insts:
        insts = [P.context.get_using_provider()]
    return all(modelbind.provider_supports_image(inst) for inst in insts)


async def _task_text_chat(P, task: str, cfg, used: dict | None = None, **kwargs):
    """按任务绑定调 text_chat：策略选主候选，异常时依次降级链上后续候选；
    无绑定走 AstrBot 当前默认 Provider。

    used 非空时写入本次成功调用实际使用的 {"model", "provider"}（麦麦观察
    展示"本次调用的模型"用；text_chat 的 LLMResponse 不回传模型名）。
    """
    provider = P.context.get_using_provider()
    candidates = modelbind.task_model_candidates(cfg, task)

    def _note_replyer_used(inst) -> None:
        # §6.6 模型联动：replyer 成功调用后把实际服务的 provider 实例回填
        # facade——get_replyer_provider 优先返回它（必然可用；绑定链抽签
        # 可能落在已失效候选上）。非 replyer 任务不记录。
        if task == "replyer":
            api = getattr(P, "api", None)
            if api is not None and hasattr(api, "note_replyer_used"):
                api.note_replyer_used(inst)

    if not candidates:
        if used is not None:
            # 先写再调（v6.18.2）：失败时 used 已带本次尝试的默认 provider
            # 标签——llm.error 上报据此归因，不再恒记调用方回落的猜测值
            used["model"] = _provider_model_label(provider, "")
            used["provider"] = str(
                getattr(provider, "provider_config", {}).get("id", "") or ""
            )
        resp = await provider.text_chat(**kwargs)
        _note_replyer_used(provider)
        return resp
    strategy = modelbind.task_model_strategy(cfg, task)
    chain = modelbind.build_model_chain(candidates, strategy, _task_model_rr, task)
    last_err: Exception | None = None
    for cand in chain:
        resolved = _resolve_bound_model(P, cand)
        if resolved is None:
            logger.warning(
                f"maisoul: 任务 {task} 绑定的 provider "
                f"{cand.get('provider')} 不存在，跳过"
            )
            continue
        inst, model = resolved
        if used is not None:
            # 先写再调（v6.18.2）：失败时 used 保留实际尝试的候选，
            # 供调用方（planner llm.error）归因；成功路径重写同值不变
            used["model"] = _provider_model_label(inst, model)
            used["provider"] = str(
                getattr(inst, "provider_config", {}).get("id", "") or ""
            )
        try:
            resp = await inst.text_chat(model=model, **kwargs)
            _note_replyer_used(inst)
            return resp
        except Exception as e:
            last_err = e
            logger.warning(
                f"maisoul: 任务 {task} 模型 {cand.get('provider')}/{model} "
                f"调用失败，尝试下一候选: {e}"
            )
    if last_err is not None:
        raise last_err
    if used is not None:
        used["model"] = _provider_model_label(provider, "")
        used["provider"] = str(
            getattr(provider, "provider_config", {}).get("id", "") or ""
        )
    return await provider.text_chat(**kwargs)
