"""任务级模型绑定 —— 对齐 MaiBot model_task_config 的多模型 + 选择策略

配置（task_models，list 型）：[{task, models: [{provider, model}], strategy}]
（AstrBot 的 object 型 schema 要求逐字段 type 定义，故用 list 存任务条目）
策略（MaiBot 原文语义）：
- sequential 按顺序优先：优先使用靠前的模型，前面的模型不可用时再尝试后面的
- random 随机选择：每次请求从模型列表中随机选择一个
- balance 负载均衡：优先选择当前使用次数较少的模型（maisoul 以轮转近似）
"""

import random

STRATEGIES = ("sequential", "random", "balance")
# 六任务：五个聊天/学习任务 + embedding（vector_intent 表达召回的嵌入绑定；
# 与 _conf_schema.json task_models 默认值同源——漏列会让 normalize 在每次
# 加载时把该任务的用户绑定从内存剥掉，重载后静默回落第一个嵌入实例）
TASKS = ("planner", "replyer", "emoji", "learner", "expression_use", "embedding")


def _task_entry(cfg, task: str) -> dict:
    """从 list 配置里找任务条目（兼容旧 dict 形态）。"""
    tm = cfg.get("task_models")
    if isinstance(tm, dict):
        entry = tm.get(task) or {}
        return entry if isinstance(entry, dict) else {}
    for entry in tm or []:
        if isinstance(entry, dict) and entry.get("task") == task:
            return entry
    return {}


def normalize_task_models(value) -> list[dict]:
    """把任意历史形态规范成全任务齐全的 list（缺省补空）。"""
    out = []
    known = {}
    if isinstance(value, dict):
        for task, entry in value.items():
            if isinstance(entry, dict):
                known[task] = entry
    elif isinstance(value, list):
        for entry in value:
            if isinstance(entry, dict) and entry.get("task"):
                known[str(entry["task"])] = entry
    for task in TASKS:
        entry = known.get(task) or {}
        out.append(
            {
                "task": task,
                "models": [
                    m
                    for m in (entry.get("models") or [])
                    if isinstance(m, dict) and m.get("provider") and m.get("model")
                ],
                "strategy": (
                    entry.get("strategy")
                    if entry.get("strategy") in STRATEGIES
                    else "sequential"
                ),
            }
        )
    return out


def task_model_candidates(cfg, task: str) -> list[dict]:
    """任务绑定的有效候选（provider+model 双全才算数；过滤手动改坏的条目）。"""
    return [
        m
        for m in (_task_entry(cfg, task).get("models") or [])
        if isinstance(m, dict) and m.get("provider") and m.get("model")
    ]


def task_model_strategy(cfg, task: str) -> str:
    strategy = str(_task_entry(cfg, task).get("strategy") or "sequential")
    return strategy if strategy in STRATEGIES else "sequential"


def build_model_chain(
    candidates: list[dict], strategy: str, rr: dict, task: str
) -> list[dict]:
    """本次调用的尝试链：主候选在前，其余按列表顺序作降级。

    rr：balance 策略的轮转计数器（调用方持有，跨次累计）。
    """
    if not candidates:
        return []
    if strategy == "random":
        primary = random.choice(candidates)
        return [primary] + [c for c in candidates if c is not primary]
    if strategy == "balance":
        idx = rr.get(task, 0) % len(candidates)
        rr[task] = idx + 1
        return [candidates[idx]] + [c for i, c in enumerate(candidates) if i != idx]
    return list(candidates)


def pick_model(candidates: list[dict], strategy: str, rr: dict, task: str):
    """只选主候选（小任务子调用用，无降级链）。"""
    chain = build_model_chain(candidates, strategy, rr, task)
    return chain[0] if chain else None


def provider_supports_image(inst) -> bool:
    """读 AstrBot 模型条目 modalities 的「图像」勾选（v6.21.0，replyer 识图门控）。

    能力口径逐字对齐框架 astr_main_agent._provider_supports_modality：
    空列表 = 迁移遗留的未配置，按不限制处理（视为支持）；缺失/非 list =
    不支持；勾了 image = 支持。inst 为 None（无 Provider）同样不支持。
    """
    modalities = (getattr(inst, "provider_config", None) or {}).get("modalities", None)
    if modalities == []:
        return True
    return isinstance(modalities, list) and "image" in modalities
