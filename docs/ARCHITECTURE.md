# 架构与数据流（astrbot_plugin_maisoul）

> 开发准则入口见 [AGENTS.md](../AGENTS.md)；本文写架构分工、数据流与目录地图。

## 1. 分工边界（核心设计）

| 层 | 归属 | 说明 |
|---|---|---|
| 全部聊天（群聊+私聊，人格/意愿/节奏/发送） | **maisoul 全权接管** | 触发门控决定说不说 → Planner 决策（planner 模式）→ 麦麦三件套 prompt 生成 → 拟人发送。私聊语义完全对标 MaiBot：private_talk_value 独立频率、max_private_context_size 独立上下文、private_chat_prompts 私聊提示词、wait 期间新消息立即唤醒（群聊不唤醒）、无空闲退避、learning/chat_prompts 规则按 rule_type=private 匹配（item_id=用户 ID） |
| 复杂 agent 操作（查资料/跑任务/工具链） | **astrbot_plugin_maid_agent** | 聊天模型调 `call_maid` 工具 → 管家 subagent 用 AstrBot 全部能力执行 → 结果回填 → 聊天模型用麦麦口吻转达发出 |
| 显式召唤（`/指令`、其他插件触发如 heartflow） | **AstrBot 原生路径** | maisoul 一律放行不拦截，全部插件（qqadmin/worldbook/maid_agent…）照常可用 |
| @机器人 / 唤醒前缀（群聊） | **maisoul 接管**（聊天全面接管政策） | 与 At 段同级作为 at 档显式点名进入门控（③ 强制触发，inevitable_at_reply 语义）与 planner forced（wait 唤醒）；`escape_at_wake=true` 可恢复旧行为（放行原生）。生态注入（心弦/记忆/世界书）由生态注入桥在麦麦管线内补齐（坑 47） |

## 2. 工具暴露策略

- **MaiBot 原生内置工具的 AstrBot 等价物** → 以同等待遇直接暴露给聊天 LLM。
  映射：MaiBot `send_emoji`（发表情包）↔ `astrbot_plugin_stealer` 的 `send_meme`；MaiBot `fetch_history`（focus 专属，部署版未开）↔ maisoul 自有 `fetch_chat_history`（buffer 数据源，坑 30b）。
  维护位置：`core/bridge.py` 的 `MAIBOT_TOOL_EQUIVALENTS`（文档用）+ 配置项 `chat_tools`（实际生效）。
- **AstrBot 技能（SKILL.md）** → 配置项 `chat_skills`，经 AstrBot 原生 `build_skills_prompt()` 注入聊天系统提示词。
- **选取入口**：WebUI「聊天工具暴露」卡的"从 AstrBot 选取"弹窗（TOOLS|SKILL 双标签 + 搜索框 + 勾选列表；搜索按名称/描述/来源插件过滤）。数据来自 `GET /astrbot_plugin_maisoul/tools`，取数完全对齐官方：工具=`FunctionToolManager.func_list + iter_builtin_tools()`（序列化字段同 ToolsService.get_tool_list，含 origin/origin_name/active），技能=`SkillManager().list_skills()`。**禁止在前端编造工具清单**；**禁止整弹窗重建 DOM**（只重绘列表区保输入焦点，勾选原位替换被点行+底栏计数）。`call_maid` 的勾选态映射「管家桥」开关（不在 chat_tools 数组），勾/取消即切 maid_bridge。
- **工具执行路径**：聊天 LLM 发起的工具调用（含 call_maid/send_meme）一律走 `bridge.call_llm_tool()` → AstrBot 原生 `FunctionToolExecutor.execute()`。装饰器注册的 llm_tool 必须走 `handler(event, **kwargs)` 路径，**不能直接 `tool.call()`**（坑 10）。
- **MaiBot 没有的能力**（如 query_favor 等 AstrBot 生态工具）→ 对聊天模型隐藏，只对 AstrBot 的 agent 模型显示（即走 `call_maid` 管家或原生唤醒路径）。
- **不为 MaiBot 已有功能重复造轮子**：偷表情/表情包管理由 `astrbot_plugin_stealer`（本身就是 MaiBot 表情系统的移植+增强）覆盖，maisoul 不再实现。

## 3. 数据流

```
聊天消息 (AstrBot GROUP_MESSAGE / PRIVATE_MESSAGE，全接管)
  │  main.py 钩子 → pipeline/gating._process_chat(event, is_group)
  │  (priority=-1000，在其他被动插件之后运行；会话键：群=group_id，私聊=sender_id)
  ├─ 放行："/"指令 / heartflow_triggered 恒放行
  │        群聊 @/唤醒前缀（is_at_or_wake_command）默认不放行——explicit=True
  │        并入 at_bot 走门控（escape_at_wake=true 恢复放行）
  │        （私聊恒进管线——AstrBot 对私聊恒置 is_at_or_wake_command，坑 1）
  ├─ 记录：states.GroupState.record_external()  → 缓冲+积压+外部消息时间戳
  ├─ 门控：trigger.should_trigger() —— 对标 MaiBot turn_scheduler/turn_gates
  │        ① effective_talk_value = talk_value × 动态规则（talk_value_rules，
  │           目标优先级 精确5>通配4>单匹配3>空1 × 时间优先级 *>区间>空）
  │        ② talk_value ≤ 0 → 静默接收（不触发）
  │        ③ 强制触发：@（At 段或群聊唤醒前缀，同级）且 inevitable_at_reply（默认开）｜提及且 mentioned_bot_reply（默认关）
  │        ④ reply_trigger_mode（MaiBot 默认 frequency）：
  │           frequency  = 攒 ceil(1/f) 条消息，或空窗补偿（idle/平均间隔折算，
  │                        封顶 threshold-1，须 ≥1 条真实新消息）
  │           reply_necessity = scoring.evaluate 评分 ≥ 80
  │              final = (档位 + 内容分 + 压力分 − 存在感惩罚) × 频率倍率(0.5+0.5f)
  │              @=100｜提及=80｜普通=0（互斥）；压力阈值 = ceil(1/f²)
  │              压力：阈值内平方→50，超阈值对数→100，闲置+15
  │              存在感：5min 自发占比 25%~60% → 0~25 惩罚
  ├─ 未触发 → event.stop_event()（静默闸门，阻断原生兜底回复）
  └- 触发 →
       ├─ mode=native：mark_fire() + is_at_or_wake_command=True → 原生 agent
       │   （maisoul 的 on_llm_request 追加三件套注入；on_llm_response 回写群聊流）
       └- mode=independent：core/prompt 组装 → provider.text_chat
             （func_tool = chat_tools 等价物 + call_maid，见 bridge.build_chat_toolset）
             → 模型调工具则 bridge.exec_tool_calls 执行后二轮生成
             → core/postprocess.process_response_segments（对标 MaiBot 回复后处理）：
                 括号心声清除(全空→"呃呃") → 超长默认回复 → 分句(引号/冒号保护+
                 概率合并 0.2/0.6/0.7) → 每句错字(typo.py 拼音字频引擎，50% 补纠正)
                 → 条数上限(max_sentence_num→超限策略) → 合并到 max_split_num
             → core/sender.send_humanlike：每段 sleep calculate_typing_time
                 （中 0.3s/字、英 0.15s/字、单汉字 3 倍+0.3、×typing_speed）后发送
             → states.record_self_reply() 回写（上下文连续 + 防复读 + 防重复目标）
```

**planner 模式（默认）**在"触发"后走 `pipeline/planner_host` 的决策循环，规格见 [MAIBOT_FIDELITY.md](MAIBOT_FIDELITY.md) Planner 决策层一节。

设计依据：

- **priority=-1000**：maisoul 是最后的闸门。reread/xinxian/outputpro 等被动监听插件（priority≥0）先跑完，maisoul 才决定拦不拦截，互不影响。
- **`/指令` 放行规则在前**：指令意味着用户要点名某个插件/命令，必须交给原生路径。
- **@/唤醒不放行**：@/唤醒也是聊天，应走麦麦管线拿拟人回复；agent 能力由 planner 的 deferred 工具池 + call_maid 管家承接。escape_at_wake=true 可整体回退。

## 4. 目录地图

```
astrbot_plugin_maisoul/
├── main.py                   # 薄入口：@register 注册、生命周期、钩子分发、旧配置迁移（只接线）
├── _conf_schema.json         # 配置 schema（WebUI 与 AstrBotConfig 字段来源，映射表见 docs/CONFIG.md）
├── metadata.yaml             # 插件元数据（name 是显示名且决定页面链接与桥接前缀，坑 8）
├── data_char_frequency.json  # 汉字字频表（错字引擎依赖，随包分发）
├── core/                     # 核心业务（纯函数/纯数据类，不 import AstrBot 运行时，可离线单测）
│   ├── constants.py          #   评分词典/常量（对齐 reply_necessity.py）；necessity 阈值公式单源
│   │                         #   necessity_threshold（trigger/scoring 共用，禁再各写一份）
│   ├── states.py             #   GroupState/StateManager：buffer/积压/防复读/replied_targets/间隔统计
│   ├── trigger.py            #   触发门控：effective_talk_value / message_trigger_threshold / 空窗补偿 / should_trigger
│   ├── scoring.py            #   必要性评分：strip_noise / is_question / 压力分 / 存在感惩罚 / evaluate
│   ├── mention.py            #   提及判定与 at 档构成（At 段 qq 精确匹配是"被艾特"唯一权威）
│   ├── sanitize.py           #   full_plain_text 全量文本：消息链拼原文，Reply 容器整体跳过（坑 61/65）
│   ├── typo.py               #   错字引擎（源码级移植 typo_generator.py：拼音同音/声调/整词替换+字频加权）
│   ├── postprocess.py        #   回复后处理：心声清除/分句/条数上限/合并/颜文字保护/calculate_typing_time
│   ├── sender.py             #   send_humanlike：后处理分段+每段打字延迟发送
│   ├── prompt.py             #   三件套 prompt 组装：identity/attention/final_user_message/preset_dialogues
│   ├── planner.py            #   Planner 决策层：系统提示词原文/工具声明/wait 状态机/历史分析回灌
│   ├── learning.py           #   聊天学习：表达/黑话/关键词反应；LearningStore(JSON 持久化，按共享组分库)
│   ├── history.py            #   fetch_chat_history 纯函数（排除集=planner 真实可见消息，坑 30b）
│   ├── personas.py           #   多人格：人格库/overlay/解析链（/persona 实时切换 > 群绑定 > 默认 > 主配置）
│   ├── emotion.py            #   情绪 VA 模型（12 锚点纯规则）+ EmotionFeedback 累积器（§6.6）
│   ├── freqfeedback.py       #   频率窗口反馈：10min 滚动窗 → 期望概率乘数 ×0.2~5.0
│   ├── monitor.py            #   麦麦观察：MonitorStore/MonitorBus/emit_*（事件对齐 MaiBot events.py 9 个）
│   ├── modelbind.py          #   任务级模型绑定：TASKS 白名单 + 三策略（sequential/random/balance）
│   ├── taskregistry.py       #   后台任务注册表：强引用保存+卸载取消等待（任务必有主）
│   ├── demote.py             #   发送队列降级判定（纯函数）
│   ├── apivalid.py           #   WebUI 写接口输入校验（纯函数）
│   └── bridge.py             #   工具桥（core 唯一运行时边界）：等价物映射/工具集构建/exec_tool_calls/
│                             #   call_llm_tool / MAID_BRIDGE_PROMPT
├── pipeline/                 # 管线宿主（改这里必须 docker restart，reload 不级联，坑 59）
│   ├── gating.py             #   消息门控接线（_process_chat；full_plain_text 唯一文本入口）
│   ├── planner_host.py       #   Planner 决策循环宿主（_planner_cycle / wait 续轮 / deferred 池）
│   ├── replyer.py            #   独立模式生成与发送（含 webchat 攒段合并发送）
│   ├── native.py             #   native 模式钩子体（三件套注入/群聊流回写）
│   ├── ecobridge.py          #   生态注入桥（直遍注册表不走 call_event_hook，坑 47）
│   ├── modelbind_host.py     #   任务模型绑定宿主（replyer_image_capable / note_replyer_used）
│   ├── emo_facade.py         #   情绪 facade：star_cls.api 跨插件数值面（§6.6）
│   ├── events_util.py        #   事件与观察工具
│   └── admin.py              #   /maisoul 管理指令
├── webui/routes.py           # 插件页后端 API（config/status/tools/monitor/learning/expressions）
├── pages/dashboard/          # WebUI（7 页；零外部依赖本地分离文件，规范见 MAIBOT_FIDELITY.md WebUI 一节）
│   ├── index.html            #   壳（1.3KB）：markup + 相对引用，服务端自动改写补 asset_token
│   ├── app.css               #   全站样式（modern 主题令牌）
│   ├── app.js                #   全部逻辑（PAGE_VERSION 在此）
│   └── mbrc.css.js           #   推理过程页 srcdoc 数据：MBRC_CSS（String.raw 装 dist CSS，坑 66）
├── tests/test_core.py        # 单元测试（自执行 + pytest 双入口；编号引用「坑 N」）
└── docs/                     # 开发文档（AGENTS.md 索引指向；不随发布包分发）
```

运行时数据（学习库/观察账本）一律存 AstrBot 持久化目录 `data/plugin_data/<插件名>/`，不进插件目录（坑 50）。

## 5. 情绪-关系耦合（数值面，历史编号 §6.6）

代码注释与测试用「§6.6」检索本节。配置 `emotion_feedback_enable`（默认关）总开关。

- **方向①（maisoul → 心弦）**：EmotionFeedback 连续同向情绪累积器 pfb 钳制 ±7，经 `pipeline/emo_facade` 暴露 `star_cls.api`，供心弦插件读取调制好感增益（增益表对齐 MaiBot positive_feedback 原参数）。
- **方向②（心弦 → maisoul）**：心弦情绪事件经 `EmotionState.apply` 反向注入，intensity ∈ [0,1] 线性缩放本次增量。
- 情绪 VA 模型 12 锚点全覆盖（标签词表 12 个）。
- 与已拆除的提示词面心弦直读（P-H）不同：**只交换数值，不渲染提示词**；心弦数据唯一提示词入口=生态注入桥。
- **replyer 模型联动**（心弦评审跟随麦麦 replyer 的模型）：`EmotionFacade.get_replyer_provider()` 取值优先「replyer 最近一次成功调用实际服务的 provider」（modelbind_host 成功路径经 `note_replyer_used` 回填——必然可用；绑定链 balance 抽签可能落在已失效候选上），冷启动回落绑定链解析（modelbind 任务 "replyer" → 无绑定默认 provider）；facade 经 `bind_replyer_picker` 注入取值器保持纯边界。

## 6. 频率窗口反馈（P-E）

10 分钟滚动窗口统计自发消息数，映射为期望概率乘数 [0.2, 5.0] 分段线性：安静（0 条）→ ×5.0 鼓励开口；达标 → ×1.0 中性；超两倍 → ×0.2 压制刷屏。近 5 分钟已发过半数（超速）时只降不升。与存在感惩罚（评分内 5min 占比惩罚）互补：绝对条数速率限制 vs 占比惩罚。
