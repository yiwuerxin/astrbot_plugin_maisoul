"""WebUI 写入接口的输入校验 —— 纯函数，不依赖 AstrBot 运行时可单测

背景（v6.15.4）：post_config 只查键名就赋值、post_learning 整包替换零校验——
talk_value 被写成字符串会让门控层 float() 逐消息抛异常，学习库被写成 list
会让 _bucket() 的 setdefault 崩掉，插件实质瘫痪直到手改配置文件。类型门槛
在这里挡住（list 元素结构不深校验，运行时处处 isinstance 兜底，保持宽松）。
"""

# schema type → 校验器（bool 是 int 子类，先排除；int/float 同理收窄）
_TYPE_CHECKERS = {
    "bool": lambda v: isinstance(v, bool),
    "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "float": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "string": lambda v: isinstance(v, str),
    "text": lambda v: isinstance(v, str),
    "list": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
}

# 学习库写盘大小上限（字符）：正常体积远小于此，防无界巨写
MAX_LEARNING_PAYLOAD_CHARS = 8_000_000


def validate_config_payload(schema, payload: dict, current) -> tuple[dict, str | None]:
    """按 schema 类型校验待写配置。

    返回 (通过的键值对, 错误信息或 None)：键必须在现有配置里（白名单，
    新键不可经 API 注入）；schema 无该键元数据时不校验（保持旧行为）。
    """
    accepted: dict = {}
    if not isinstance(payload, dict):
        return accepted, "配置必须是对象"
    for k, v in payload.items():
        if k not in current:
            continue
        meta = (schema or {}).get(k) if isinstance(schema, dict) else None
        checker = _TYPE_CHECKERS.get(str((meta or {}).get("type") or ""))
        if checker is not None and not checker(v):
            return accepted, (f"字段 {k} 期望 {meta.get('type')} 类型，"
                              f"收到 {type(v).__name__}")
        accepted[k] = v
    return accepted, None


def validate_learning_payload(payload) -> str | None:
    """学习库结构校验：dict[共享组键] = {expressions: list, jargons: list}。

    通过返回 None，否则返回中文错误信息。条目字段内部不强校验（注入路径
    均以 .get 容错），只挡会把运行时打崩的顶层形态。
    """
    import json

    if not isinstance(payload, dict):
        return "学习库必须是对象"
    for key, bucket in payload.items():
        if not isinstance(bucket, dict):
            return f"分库 {key} 必须是对象"
        for field in ("expressions", "jargons"):
            v = bucket.get(field)
            if v is not None and not isinstance(v, list):
                return f"分库 {key}.{field} 必须是数组"
    try:
        # 与 LearningStore.save 的落盘格式同构（indent=1）——用紧凑式测量会
        # 低估磁盘体积约两成，贴近上限的载荷落盘后超限（Sourcery 审查）
        size = len(json.dumps(payload, ensure_ascii=False, indent=1))
    except (TypeError, ValueError):
        return "学习库内容无法序列化为 JSON"
    if size > MAX_LEARNING_PAYLOAD_CHARS:
        return f"学习库体积超限（{size} > {MAX_LEARNING_PAYLOAD_CHARS} 字符）"
    return None
