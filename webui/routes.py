"""WebUI 路由 —— 麦麦风格配置页的后端 API（config / status）"""

from astrbot.api import logger


def register_webui(context, config, states, learning_store=None, monitor=None) -> None:
    """注册 maisoul 的 WebUI API（重复注册时框架会原位替换）。"""
    try:
        from quart import jsonify, request

        async def get_config():
            return jsonify({"success": True, "data": dict(config)})

        async def get_models_list():
            """模型管理页数据源：以 AstrBot 的模型提供商（provider_sources）为准。

            每源模型 = 该源下用户添加的模型条目（provider entry 的 model 字段）
            ——与 AstrBot 模型设置页所见一致；不并 get_models() 上游目录
            （同中转的源目录完全相同，会掩盖用户自己的模型清单）。
            chat_completion 源供聊天任务选取；embedding 源（provider_type=
            embedding）单列并标 type，供 embedding 任务（vector_intent 表达
            召回）选取，页面按任务过滤。"""
            providers = []
            try:
                pm = context.provider_manager
                # 读 live 配置对象（acm.default_conf）：dashboard 的删除/更新会
                # 重绑 config["provider"] 到新列表，manager.providers_config 仍指
                # 向旧列表（AstrBot delete_provider 的怪癖）——读旧快照会出现
                # "已删除的 Provider 还在列表里"
                live_conf = getattr(getattr(pm, "acm", None), "default_conf", None)
                configs = list((live_conf or {}).get("provider")
                               or getattr(pm, "providers_config", []) or [])
                sources_conf = (live_conf or {}).get("provider_sources") \
                    or getattr(pm, "provider_sources_config", None) or []
                sources = [s for s in (sources_conf or [])
                           if isinstance(s, dict) and s.get("id")
                           and bool(s.get("enable", True))
                           and str(s.get("provider_type") or "chat_completion")
                           in ("chat_completion", "embedding")]
                source_ids = {str(s["id"]): s for s in sources}
                groups: dict = {}
                for pc in configs:
                    source = str(pc.get("provider_source_id") or "")
                    if source not in source_ids:
                        continue
                    default_model = str(pc.get("model") or "")
                    if not default_model:
                        continue
                    g = groups.setdefault(source, {"models": [], "enabled_models": []})
                    if default_model not in g["models"]:
                        g["models"].append(default_model)
                    if bool(pc.get("enable", True)) and default_model not in g["enabled_models"]:
                        g["enabled_models"].append(default_model)
                for source_id in source_ids:
                    g = groups.get(source_id) or {}
                    models = g.get("enabled_models") or g.get("models") or []
                    if not models:
                        continue
                    providers.append({
                        "id": source_id,
                        "type": "embedding" if str(
                            source_ids[source_id].get("provider_type")) == "embedding" else "",
                        "enable": True,
                        "default_model": models[0],
                        "models": models,
                    })
                # 旧式嵌入条目（自含 embedding_* 字段、无 provider_source_id，
                # AstrBot 模型设置里「嵌入模型」的形态）——禁用的也列出（带
                # enable=false，需在 AstrBot 侧启用后才可实际调用）
                seen_ids = {p["id"] for p in providers}
                for pc in configs:
                    if not isinstance(pc, dict) or str(pc.get("id") or "") in seen_ids:
                        continue
                    is_embedding = (str(pc.get("provider_type") or "") == "embedding"
                                    or str(pc.get("type") or "").endswith("embedding"))
                    emb_model = str(pc.get("embedding_model") or pc.get("model") or "").strip()
                    if not is_embedding or not pc.get("id") or not emb_model:
                        continue
                    providers.append({
                        "id": str(pc["id"]),
                        "type": "embedding",
                        "enable": bool(pc.get("enable", True)),
                        "default_model": emb_model,
                        "models": [emb_model],
                    })
                providers.sort(key=lambda p: (-len(p["models"]), p["id"]))
            except Exception:
                logger.error("maisoul: 读取 Provider 模型列表失败", exc_info=True)
            return jsonify({"success": True, "data": {"providers": providers}})

        async def post_config():
            try:
                payload = await request.get_json() or {}
            except Exception:
                payload = {}
            if not isinstance(payload, dict):
                return jsonify({"success": False, "error": "invalid payload"}), 400
            for k, v in payload.items():
                if k in config:
                    config[k] = v
            config.save_config()
            return jsonify({"success": True})

        async def get_learning():
            if learning_store is None:
                return jsonify({"success": False, "error": "learning store 未初始化"}), 500
            return jsonify({"success": True, "data": learning_store.data})

        async def post_learning():
            if learning_store is None:
                return jsonify({"success": False, "error": "learning store 未初始化"}), 500
            try:
                payload = await request.get_json() or {}
            except Exception:
                payload = {}
            if not isinstance(payload, dict):
                return jsonify({"success": False, "error": "invalid payload"}), 400
            learning_store.data = payload
            learning_store.save()
            return jsonify({"success": True})

        async def get_status():
            from ..core.trigger import effective_talk_value, message_trigger_threshold

            talk_value = effective_talk_value(config, "", "")
            mode = str(config.get("reply_trigger_mode", "frequency"))
            return jsonify({
                "success": True,
                "data": {
                    "version": "6.14.5",
                    "mode": config.get("mode"),
                    "enable": config.get("enable"),
                    "maid_bridge": bool(config.get("maid_bridge", True)),
                    "bot_name": config.get("bot_name"),
                    "reply_trigger_mode": mode,
                    "talk_value": round(talk_value, 3),
                    "trigger_threshold": message_trigger_threshold(mode, talk_value),
                    "groups": states.status_all(),
                },
            })

        async def get_tools():
            """读取 AstrBot 原生注册的 llm_tool 与技能列表（WebUI 暴露工具弹窗的数据源）。"""
            from ..core import bridge

            try:
                tools = bridge.list_astrbot_tools(context)
            except Exception:
                logger.error("maisoul: 读取 AstrBot 工具列表失败", exc_info=True)
                tools = []
            try:
                skills = bridge.list_astrbot_skills()
            except Exception:
                logger.error("maisoul: 读取 AstrBot 技能列表失败", exc_info=True)
                skills = []
            return jsonify({"success": True, "data": {"tools": tools, "skills": skills}})

        async def get_monitor_replay():
            """麦麦观察：按 event_id 重放事件账本（?since=&limit=）。"""
            if monitor is None:
                return jsonify({"success": False, "error": "monitor 未初始化"}), 500
            try:
                since = int((request.args.get("since") or "0"))
            except ValueError:
                since = 0
            try:
                limit = int((request.args.get("limit") or "300"))
            except ValueError:
                limit = 300
            return jsonify({"success": True, "data": monitor.store.replay(
                since_event_id=since, limit=limit)})

        async def get_monitor_stream():
            """麦麦观察：SSE 实时事件流（先发 hello 再转发订阅队列）。"""
            import asyncio as _aio
            import json as _json

            from quart import Response

            if monitor is None:
                return jsonify({"success": False, "error": "monitor 未初始化"}), 500

            async def _gen():
                q = monitor.bus.subscribe()
                try:
                    yield f"data: {_json.dumps({'event': 'stream.open', 'data': {}}, ensure_ascii=False)}\n\n"
                    while True:
                        try:
                            item = await _aio.wait_for(q.get(), timeout=15.0)
                        except _aio.TimeoutError:
                            yield ": heartbeat\n\n"
                            continue
                        yield f"data: {_json.dumps(item, ensure_ascii=False)}\n\n"
                finally:
                    monitor.bus.unsubscribe(q)

            return Response(_gen(), content_type="text/event-stream",
                            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

        # 双前缀注册：扩展 API 按注册路径正则匹配，而插件列表/页面链接用的是
        # metadata 显示名（已改「麦麦之魂」）——两个前缀都注册才能新旧入口都通
        bases = ("/astrbot_plugin_maisoul", "麦麦之魂")
        for base in bases:
            context.register_web_api(f"{base}/config", get_config, ["GET"], "maisoul WebUI: 读取配置")
            context.register_web_api(f"{base}/config", post_config, ["POST"], "maisoul WebUI: 保存配置")
            context.register_web_api(f"{base}/status", get_status, ["GET"], "maisoul WebUI: 运行状态")
            context.register_web_api(f"{base}/learning", get_learning, ["GET"], "maisoul WebUI: 读取学习库")
            context.register_web_api(f"{base}/learning", post_learning, ["POST"], "maisoul WebUI: 保存学习库")
            context.register_web_api(f"{base}/tools", get_tools, ["GET"], "maisoul WebUI: AstrBot 工具与技能列表")
            context.register_web_api(f"{base}/monitor/replay", get_monitor_replay, ["GET"], "maisoul WebUI: 麦麦观察事件重放")
            context.register_web_api(f"{base}/monitor/stream", get_monitor_stream, ["GET"], "maisoul WebUI: 麦麦观察实时流")
            context.register_web_api(f"{base}/models/list", get_models_list, ["GET"], "maisoul WebUI: Provider 模型列表")
        logger.info("maisoul WebUI API 已注册")
    except Exception:
        logger.error("maisoul WebUI 注册失败", exc_info=True)
