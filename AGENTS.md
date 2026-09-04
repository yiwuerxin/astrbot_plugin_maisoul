# AGENTS.md — astrbot_plugin_maisoul 开发指南

> 本文档面向后续接手的 AI/人类开发者，目标是**零阅读源码即可开始开发**。
> 所有设计决策、数据流、配置字段、测试方法、取舍清单都在这里。
> 当前版本 v6.14.0：显示名「麦麦之魂」。新增预设对话（示例对话风格参考）；含 麦麦观察/模型管理/管家桥/任务级模型绑定/聊天全面接管（@与唤醒也进麦麦管线，escape_at_wake 默认关）；planner 历史分析跨轮回灌（坑 52）+ planner 请求结构对齐部署版（消息前缀/角色/尾部/时间消息/fetch_history 移除，坑 53）+ 思考文本不回灌（坑 54）+ 工具轮协议结构对齐与 reply 后续轮（坑 55，v4f 无需换模型即收敛中文结构化正文）；表达方式 vector_intent 语义召回（嵌入任务槽 + 向量缓存 + 回落）。

---

## 1. 项目定位

**一句话**：把 MaiBot（麦麦）的 QQ 群聊发言流水线完整移植为 AstrBot 插件，**完全取代 AstrBot 原生的群聊回复路径**，同时保留 AstrBot 的插件与 agent 框架能力作为"逃生舱"。

### 1.1 分工边界（核心设计）

| 层 | 归属 | 说明 |
|---|---|---|
| 全部聊天（群聊+私聊，人格/意愿/节奏/发送） | **maisoul 全权接管** | 触发门控决定说不说 → Planner 决策（planner 模式）→ 麦麦三件套 prompt 生成 → 拟人发送。私聊语义完全对标 MaiBot：private_talk_value 独立频率、max_private_context_size 独立上下文、private_chat_prompts 私聊提示词、wait 期间新消息立即唤醒（群聊不唤醒）、无空闲退避、learning/chat_prompts 规则按 rule_type=private 匹配（item_id=用户 ID） |
| 复杂 agent 操作（查资料/跑任务/工具链） | **astrbot_plugin_maid_agent** | 聊天模型调 `call_maid` 工具 → 管家 subagent 用 AstrBot 全部能力执行 → 结果回填 → 聊天模型用麦麦口吻转达发出 |
| 显式召唤（`/指令`、其他插件触发如 heartflow） | **AstrBot 原生路径** | maisoul 一律放行不拦截，全部插件（qqadmin/worldbook/maid_agent…）照常可用 |
| @机器人 / 唤醒前缀（群聊） | **maisoul 接管**（v6.10.0 起，项目政策：聊天全面接管） | 不再放行原生：与 At 段同级作为 at 档显式点名进入门控（③ 强制触发，inevitable_at_reply 语义）与 planner forced（wait 唤醒）；`escape_at_wake=true` 可恢复旧行为（放行原生）。生态注入（心弦/记忆/世界书）由 v6.11.0 生态注入桥在麦麦管线内补齐（见坑 47） |

### 1.2 工具暴露策略（项目设计原则）

- **MaiBot 原生内置工具的 AstrBot 等价物** → 以**同等待遇直接暴露给聊天 LLM**。
  目前映射：MaiBot `send_emoji`（发表情包）↔ `astrbot_plugin_stealer` 的 `send_meme`。
  维护位置：`core/bridge.py` 的 `MAIBOT_TOOL_EQUIVALENTS`（文档用）+ 配置项 `chat_tools`（实际生效）。
- **AstrBot 技能（SKILL.md）** → 配置项 `chat_skills`，经 AstrBot 原生 `build_skills_prompt()` 注入聊天系统提示词（与 astr_main_agent 注入主 agent 同一机制）。
- **工具/技能的选取入口**：WebUI「聊天工具暴露」卡的"从 AstrBot 选取"按钮 → 弹窗（顶部 TOOLS|SKILL 双标签，下方勾选列表）。数据来自 `GET /astrbot_plugin_maisoul/tools`，取数完全对齐官方：工具=`FunctionToolManager.func_list + iter_builtin_tools()`（序列化字段同 ToolsService.get_tool_list，含 origin/origin_name/active），技能=`SkillManager().list_skills()`。**禁止在前端编造工具清单**。弹窗里 `call_maid` 的勾选态映射「管家桥」开关（不在 chat_tools 数组，badge 标"管家桥·默认"），勾/取消即切 maid_bridge。
- **工具执行路径**：聊天 LLM 发起的工具调用（含 call_maid/send_meme）一律走 `bridge.call_llm_tool()` → AstrBot 原生 `FunctionToolExecutor.execute()`（装饰器注册的 llm_tool 必须走 `_execute_local → call_local_llm_tool` 的 `handler(event, **kwargs)` 路径，**不能直接 `tool.call()`**，见 §8 坑10）。
- **MaiBot 没有的能力**（如 query_favor 好感度查询等 AstrBot 生态工具）→ **对聊天模型隐藏**，只对 AstrBot 的 agent 模型显示（即走 `call_maid` 管家或原生唤醒路径）。
- **不要为 MaiBot 已有功能重复造轮子**：偷表情/表情包管理已由 `astrbot_plugin_stealer`（本身就是 MaiBot 表情系统的移植+增强）覆盖，maisoul 不再实现。

---

## 2. 架构与数据流

```
聊天消息 (AstrBot GROUP_MESSAGE / PRIVATE_MESSAGE，v6.6 起全接管)
  │  main.py on_group_message / on_private_message → _process_chat(event, is_group)
  │  (priority=-1000，在其他被动插件之后运行；会话键：群=group_id，私聊=sender_id)
  ├─ 放行："/"指令 / heartflow_triggered 恒放行
  │        群聊 @/唤醒前缀（is_at_or_wake_command）默认不放行——explicit=True
  │        并入 at_bot 走门控（escape_at_wake=true 恢复放行，v6.10.0 前=恒放行）
  │        （私聊恒进管线——AstrBot 对私聊恒置 is_at_or_wake_command，见 §8 坑5）
  ├─ 记录：states.GroupState.record_external()  → 缓冲+积压+外部消息时间戳
  ├─ 门控：trigger.should_trigger() —— 对标 MaiBot turn_scheduler/turn_gates
  │        ① effective_talk_value = talk_value × 动态规则（talk_value_rules，
  │           目标优先级 精确5>通配4>单匹配3>空1 × 时间优先级 *>区间>空）
  │        ② talk_value ≤ 0 → 静默接收（不触发）
  │        ③ 强制触发：@（At 段或群聊唤醒前缀，v6.10.0 起同级）且 inevitable_at_reply（默认开）｜提及且 mentioned_bot_reply（默认关）
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
       └- mode=independent（默认）：core/prompt 组装 → provider.text_chat
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

**为什么 `priority=-1000`**：maisoul 是最后的闸门。reread/xinxian/outputpro 等被动监听插件（priority≥0）先跑完，maisoul 才决定拦不拦截，互不影响。
**为什么 `/指令` 放行规则在前**：指令意味着用户要点名某个插件/命令（qqadmin、/persona…），必须交给原生路径。
**为什么 v6.10.0 起 @/唤醒不再放行**（项目政策：聊天全面接管，工具 agent 才归 AstrBot）：@/唤醒也是聊天，应走麦麦管线拿拟人回复；agent 能力由 planner 的 deferred 工具池 + call_maid 管家承接，原生路径的点名单不再必需。escape_at_wake=true 可整体回退旧行为。

---

## 3. 目录结构（逐文件说明）

```
astrbot_plugin_maisoul/
├── AGENTS.md                 # 本文档
├── metadata.yaml             # AstrBot 插件元数据（name/desc/version）
├── main.py                   # 薄入口（~300行）：@register 注册、生命周期、全部钩子分发。
│                             #   只做"接线"，业务逻辑一律在 core/ 下；含旧配置迁移
├── _conf_schema.json         # 配置 schema：WebUI 与 AstrBotConfig 的字段来源（键名/默认值/
│                             #   描述逐条对标 MaiBot official_configs.py，见 §4 映射表）
├── data_char_frequency.json  # 汉字字频表（拷自 MaiBot depends-data/，错字引擎用）
├── data_learning.json        # 学习库（表达/黑话，按共享组分库）——运行时生成于
│                             #   AstrBot 持久化目录 data/plugin_data/<插件名>/（坑 50，
│                             #   卸载不删数据时幸存；首次运行自动从插件目录迁移）
├── core/                     # 核心业务（不依赖 AstrBot 运行时可单测）
│   ├── constants.py          # 评分词典/常量，逐条对齐 MaiBot reply_necessity.py；
│   │                         #   含 OUTPUT_INSTRUCTION（MaiBot 输出指令原文）
│   ├── states.py             # GroupState（buffer/recent_self/last_replies/replied_targets/
│   │                         #   pending_since_fire/ext_intervals/firing）+ StateManager
│   │                         #   方法：record_external / mark_fire / record_self_reply /
│   │                         #   recently_replied / avg_external_interval / status
│   ├── trigger.py            # 触发门控（对标 turn_scheduler/turn_gates）：
│   │                         #   effective_talk_value(动态规则) / message_trigger_threshold
│   │                         #   (frequency=ceil(1/f), necessity=ceil(1/f²)) /
│   │                         #   idle_compensation(空窗补偿) / should_trigger(总门控)
│   ├── scoring.py            # 必要性评分（对标 reply_necessity.py）：strip_noise /
│   │                         #   is_question / request_reason / opinion_reason /
│   │                         #   pressure_score / presence_penalty / freq_factor / evaluate
│   ├── typo.py               # 错字引擎（源码级移植 MaiBot typo_generator.py）：
│   │                         #   拼音同音字+声调错误+整词替换+字频加权；依赖 jieba/pypinyin
│   ├── postprocess.py        # 回复后处理（源码级移植 process_llm_response_segments）：
│   │                         #   括号心声/呃呃/超长默认回复/分句/条数上限/合并/颜文字保护
│   │                         #   /calculate_typing_time(×typing_speed)
│   ├── learning.py           # 聊天学习子系统（v6.5，对标 MaiBot 学习器）：
│   │                         #   LearningStore(JSON 持久化，按共享组分库) /
│   │                         #   keyword_reaction_block(关键词/正则+命名捕获组) /
│   │                         #   expression_habits_block(legacy 抽样注入) /
│   │                         #   jargon_reference_block(上下文命中注入) /
│   │                         #   learning_flags + share_key(学习配置/共享组匹配) /
│   │                         #   learn_from_chat(异步学习器，prompts 全部 MaiBot 原文)
│   ├── planner.py            # Planner 决策层（v6.6，对标 maisaka）：系统提示词
│   │                         #   maisaka_chat.prompt 逐字原文 / MAX_INTERNAL_ROUNDS=10 /
│   │                         #   PlannerState(wait 状态机+连续上限+空闲指数退避+打断) /
│   │                         #   工具声明(reply/wait/send_emoji/tool_search 原文) /
│   │                         #   render_pending_messages(<message>前缀)
│   ├── prompt.py             # MaiBot 三件套 prompt 组装：build_identity /
│   │                         #   select_reply_style(含彩票) / build_attention_block
│   │                         #   (通用+chat_prompts 精确匹配多条拼接) /
│   │                         #   build_system_prompt / build_final_user_message
│   │                         #   (当前时间/记录/回复信息参考/同目标防重复/结尾指令原文)
│   ├── sender.py             # send_humanlike：后处理分段 + 每段打字延迟发送
│   ├── personas.py           # 多人格（MaiBot 没有的扩展）：人格库查找 find_persona /
│   │                         #   overlay(三件套覆盖视图) / resolve_active(解析链：
│   │                         #   /persona 实时切换 > 群绑定 > 默认人格 > 主配置)；
│   │                         #   主配置人格名 = bot_name（/persona 麦麦 可切）
│   ├── monitor.py            # 麦麦观察（对齐 maisaka/monitor）：MonitorStore
│   │                         #   (JSON 事件账本，10000条/72h/每200条或60s清理) +
│   │                         #   MonitorBus(订阅队列) + Monitor.emit_*（事件名/字段
│   │                         #   对齐 MaiBot events.py 的 9 个 emit；stage.* 不落账本）
│   └── bridge.py             # 工具桥：MAIBOT_TOOL_EQUIVALENTS 映射表 /
│                             #   build_chat_toolset(暴露策略) / exec_tool_calls /
│                             #   call_llm_tool(官方 FunctionToolExecutor 路径) /
│                             #   MAID_BRIDGE_PROMPT(工具使用说明注入文案)
├── webui/
│   └── routes.py             # 插件页后端 API：GET/POST /astrbot_plugin_maisoul/config、
│                             #   GET .../status、.../tools、GET .../monitor/replay
│                             #   (since/limit 增量重放)、GET .../monitor/stream(SSE)
├── pages/dashboard/index.html# 麦麦风格 WebUI（单文件零依赖，future-retro 纸面配色；
│                             #   「麦麦观察」页 = 会话侧栏+阶段状态+时间线卡片）
└── tests/test_core.py        # 单元测试（容器内 python3 直接跑，158 用例）
```

## 4. 配置字段全表（_conf_schema.json）

**命名规则**：AstrBot schema 是扁平键，MaiBot 是嵌套配置。除 collision 需加前缀外，
键名/默认值/描述全部取 MaiBot 叶子名（官方 zh_CN 标签+docstring）。映射表：

| maisoul 键 | MaiBot 配置路径 | 默认 |
|---|---|---|
| reply_trigger_mode | chat.reply_timing.reply_trigger_mode | `frequency`（频率触发；另一值 reply_necessity） |
| talk_value | chat.reply_timing.talk_value | `1.0` |
| private_talk_value | chat.reply_timing.private_talk_value | `1.0`（私聊独立频率） |
| private_chat_prompts / max_private_context_size | chat.reply_style.private_chat_prompts / chat.max_private_context_size | MaiBot 原文 / `60` |
| mentioned_bot_reply / inevitable_at_reply | 同名 | `false` / `true` |
| enable_talk_value_rules / talk_value_rules | 同名 | `false` / 两条示例（凌晨 0.8 / 白天 1.0） |
| max_context_size | chat.max_context_size | `40` |
| enable_response_post_process / typing_speed | response_post_process.* | `true` / `1.0` |
| typo_*（enable/error_rate/min_freq/tone_error_rate/word_replace_rate/enable_correction_quote/correction_quote_probability） | chinese_typo.* | true/0.01/9/0.1/0.006/true/1.0 |
| splitter_*（enable/max_length/max_sentence_num/max_split_num/enable_kaomoji_protection/enable_overflow_return_all） | response_splitter.* | true/512/8/3/false/false |
| keyword_rules / regex_rules | keyword_reaction.* | `[]`（规则 {keywords[],regex[],reaction}，reaction 支持 [命名捕获组]） |
| expression_checked_only / expression_self_reflect / expression_selection_mode / max_expression_learner | expression.* | true / true / legacy / 3 |
| expression_vector_candidate_pool_size | expression.expression_vector_candidate_pool_size | `50`（vector_intent 每次召回进入精选的候选上限，1~50） |
| expression_learning_list / expression_groups | expression.learning_list / expression_groups | 默认全局 use+learn / 空 |
| jargon_learning_list / jargon_groups | jargon.learning_list / jargon_groups | 默认全局 use+learn / 空 |
| enable_context_optimization | chat.enable_context_optimization | `true`（自己旧发言保留最近 3 条） |
| max_consecutive_wait_count / planner_interrupt_max_consecutive_count | chat.reply_timing.* | 3 / 0 |
| no_action_backoff_*（base/cap/start/bypass） | chat.reply_timing.* | 15 / 300 / 2 / 6 |
| enable_reply_quote | chat.reply_style.enable_reply_quote | `true` |
| bot_name / aliases / personality / behavior_style / reply_style / group_chat_prompt / chat_prompts / multiple_reply_style | bot.nickname / bot.alias_names / personality.* / chat.reply_style.* | 与 MaiBot 官方默认一致（nickname=麦麦、alias_names=[]） |
| mode / maid_bridge / chat_tools / enable / escape_at_wake / personas / default_persona / group_persona / follow_persona_switch / preset_dialogues | —（maisoul 扩展或插件必需） | preset_dialogues=预设对话 `[{user, reply}]`，经 prompt.build_preset_dialogues_block 注入系统提示词【预设对话】块（人格可覆盖，PERSONA_FIELDS 已含） |

chat_prompts 条目结构 = MaiBot ExtraPromptItem：`{platform, item_id, rule_type:"group"|"private", prompt}`，
platform+目标 ID **精确匹配**（无后缀/通配），多条命中换行拼接；私聊时 item_id=用户 ID。
talk_value_rules 条目：`{platform, item_id, rule_type:"group"|"private", time:"HH:MM-HH:MM或*", value}`。
学习配置条目同 LearningItem：`{platform, item_id, type:"group"|"private", use, learn}`。

**v6.4 已删除的自造键**（页面加载时自动从状态清理；旧值 nicknames 会合并进 aliases）：
threshold、frequency、cooldown、context_size、seg_min_delay、seg_max_delay、enable_typo、typo_rate、nicknames。

| 字段 | 类型/默认 | 作用 |
|---|---|---|
| mode | str `independent` | independent=麦麦流水线接管；native=触发后交原生 agent；`planner`（待实现，完全对标 maisaka 决策层，见 §7.1） |
| bot_name | str `麦麦` | 机器人昵称：麦麦显示和自称时使用的名字（identity 首行、自发回写署名）；同时是主配置人格的人格名 |
| aliases | list `[]` | 别名：别人可能用来称呼麦麦的名字，用于辅助识别提及——身份行与提及检测共用这一份（对应 MaiBot alias_names，单一列表） |
| personality | text | 人格设定：麦麦的人格和身份设定（[personality].personality） |
| behavior_style | text | 行为风格：Planner 使用的行动准则（这里注入生成层） |
| reply_style | text | 表达风格：麦麦平时说话的风格（[personality].reply_style） |
| multiple_reply_style | list | 备用表达风格彩票池 |
| multiple_probability | int `0` | 彩票概率 % |
| group_chat_prompt | text | 群聊提示词：群聊通用提示词 |
| chat_prompts | list `[]` | 额外 Prompt `[{platform:"qq", item_id:"群号", rule_type:"group", prompt}]`，精确匹配多条拼接 |
| chat_tools | list `["send_meme"]` | 暴露给聊天 LLM 的工具（MaiBot 等价物），弹窗从 AstrBot 注册表选取 |
| chat_skills | list `[]` | 暴露给聊天 LLM 的 AstrBot 技能（SKILL.md），经原生 build_skills_prompt 注入 |
| maid_bridge | bool `true` | 是否把 call_maid 一并暴露（独立模式与 planner 模式的 replyer 均生效） |
| personas | list `[]` | 多人格库 `[{name, bot_name?(机器人昵称), personality(人格设定), behavior_style?(行为风格), reply_style?(表达风格), aliases?(别名), group_chat_prompt?(群聊提示词)}]`——字段名与描述与 MaiBot official_configs 官方文案一致 |
| default_persona | str `` | 默认人格名；空=主配置三件套 |
| group_persona | list `[]` | 静态群绑定 `[{chat:"群号或*", name}]` |
| follow_persona_switch | bool `true` | 兼容 astrbot_plugin_persona_switch：/persona 切换后同名 maisoul 人格最高优先生效 |
| enable | bool `true` | 总开关 |
| escape_at_wake | bool `false` | 逃生舱开关：false（默认）=聊天全面接管，群聊 @/唤醒前缀作为 at 档显式点名进入麦麦门控；true=恢复旧行为，@/唤醒放行原生 agent 回答。/指令与 heartflow 恒放行不受影响 |
| eco_injection | bool `true` | 生态注入桥（v6.11.0）：independent/planner 生成前手动触发 on_llm_request 钩子链收集注入（心弦好感/记忆/世界书）拼进 system_prompt，发言后触发 on_llm_response（记忆沉淀等回写）；native 模式原生已生效。关闭后提示词完全对标 MaiBot |

## 5. 开发规范

1. **质量优先**：core/ 保持纯函数/纯数据类，不 import astrbot 运行时对象（bridge.py 例外，它就是运行时边界）。
2. **改任何 core/ 逻辑必须先补测试**再改实现；测试在容器内跑（容器名/路径一律占位符，本机实际命令记在 AGENTS.local.md）：
   ```bash
   sudo docker exec <astrbot容器> python3 <容器内插件路径>/tests/test_core.py
   ```
3. main.py 只做接线；新钩子/新配置照抄现有模式（schema 加字段 → routes 自动透传 → 页面 collect() 加一行）。
4. WebUI 页面（pages/dashboard/index.html）三条铁律：
   - 单文件、零外部依赖（无 CDN）；
   - **内联 script 里绝不出现 body/script 的闭合标签字面量**（服务端注入会误伤，历史事故）；
   - 桥获取必须走 `ensureBridge()`（官方 SDK 2.5s 轮询 → 内置 postMessage 备用桥），禁止直接读全局。
5. 版本号三处同步：metadata.yaml、main.py `@register`、页面 PAGE_VERSION + status API。
6. 复刻保真原则：凡标"对齐 MaiBot xxx"的常量/公式，改动前先对照 MaiBot 官方仓库源码（github.com/Mai-with-u/MaiBot）的对应文件。
7. **需求铁律（项目首要政策）**：任何功能需求，**默认含义是"完全对标 MaiBot，功能要完全一样"**——行为、配置字段、提示词结构、参数默认值都按 MaiBot 源码来，不许自作主张做"近似/简化版"。**实现写法也要照抄 MaiBot**（MaiBot 用 SQL 表就用 SQL 表，不许以"插件侧更轻"为由换成 JSON 等变体）；**WebUI 仿照对象 = MaiBot 部署实例的构建产物**（`:dashboard端口` 的 pip 包 `maibot_dashboard` dist），不是容器里的前端源码 dump——样式、颜色、字号、圆角、图标（lucide SVG，不用 emoji）、文案必须**一模一样**：部署版有的一个不能少，部署版没有的（如暂停按钮、自造徽章）**不许自己加**；比对方法 = 抓部署 chunk 里的中文字符串与 CSS 变量/组件类。只有两种情况可以偏离：① MaiBot 没有该功能（此时按 AstrBot 生态最优实现，自己写）；② **相对 MaiBot 本身的写法确实有更好的替代**——必须先在 Issue/PR 中说明并获维护者同意才能用，不许默认采用。拿不准就先查源码再动手，不要凭记忆或直觉实现。
8. **代码规范（v6.10.0 移植自 MaiBot AGENTS.md：<https://github.com/Mai-with-u/MaiBot/blob/main/AGENTS.md>，已按插件形态适配，2026-09-02 复核上游全文补入提交卫生/UI 叠层排查/实验目录三条；不适配项：uv/pyproject、npm build、A_memorix、插件提交仓库流程、data-dashboard-style 主题与 Radix/motion 组件（本插件单文件零依赖页无此设施）、prompt 多语言模板（maisoul 提示词仅中文、直取 MaiBot 中文原文））**：
   - **import 顺序**：`from X import Y` 在前、`import X` 在后，两组各自按字母序；标准库/第三方在前、本地模块在后，块间空行分隔。core/ 内相对导入；astrbot 框架对象统一 `from astrbot...` 绝对导入（bridge.py 是唯一运行时边界）。
   - **注释**：重构时原注释可修正不可删；新增的长/复杂逻辑块必须写注释。一律简体中文。
   - **类型注解**：重构保留原注解；复杂函数、多参数函数补注解（简单变量可不加）；泛型用 `typing` 模块（`list[int]`/`dict[str, Any]`）。
   - **变量**：类型已确定（注解或源头保证）时不要写 `or` fallback 链——如 `cfg["bot_name"] or ""` 属于掩盖问题。
   - **类属性**：少用 getattr/setattr，能直接属性访问就直接写；例外：bridge.py 对 AstrBot 动态属性（`event._xxx` 等）的访问。
   - **debug 铁律**：**不许用 fallback/兜底掩盖错误**——有错就完整暴露（logger.error + 异常栈），精准定位根因，兜底难以维护。允许**显式声明的降级路径**（如二轮生成失败回退一轮），但必须留 error 日志；禁止静默吞异常（与坑 44 一脉相承）。
   - **语言**：注释、日志、WebUI 文案一律简体中文优先。
   - **WebUI**：涉及聊天流/会话显示用真实名称（群名或"xxx 的私聊"），不裸显 session_id；排查布局问题：对比展开前后 DOM 找新增元素、查 computed style 实际值（height/min-height/background/backdrop-filter），不要只看 CSS class；排查底纹/阴影/半透明/模糊/颜色叠加问题：先按 DOM 层级拆分父容器、触发器、内部装饰元素和伪元素，逐层查 computed style 的 background/background-color/background-image/backdrop-filter/box-shadow/opacity，不要只盯着截图中最显眼的子元素或只看 class（MaiBot 原文）。
   - **会话 ID**：业务代码不得自行拼会话键或造 fallback hash 写库——统一走既有会话键（群=group_id，私聊=sender_id，见坑 23）。
   - **配置**：纯配置改动只动 schema 字段 + 页面 render/collect + 本文档 §4 映射表（§5.3 模式），不需要建测试文件；main.py 里旧配置迁移逻辑保持幂等，已发布的迁移路径不可随意改动，也不擅自新增迁移步骤（对齐 MaiBot「除非明确说明不擅自新增 ConfigUpgradeHook／禁改 legacy_migration」）。
   - **changelog**：本插件无独立 changelog 文件，以 git 提交信息承载——一个功能一行、按模块分段；纯版本号提升不单独成条。


9. **Commit / PR 规范（Conventional Commits，<https://www.conventionalcommits.org>；"Angular 规范"是俗称，Angular 私规不照抄）**：
   - **Commit**：`<type>(<scope>): 祈使句摘要`——一行、动词开头，≤72 字符为硬线；正文写**为什么**（72 列手动换行）；破坏性变更用 `feat!:` 或 `BREAKING CHANGE:` footer；**不列文件清单**（仅文件移动/全局配置/对外 API 变更三种情况点名文件）
   - **PR 四段**：① 改动简述（用户可感知，即 changelog 口径）② 为什么改（背景）③ 核心改动（只挑 1-2 个关键文件或风险点；**UI 改动必须附前后截图**）④ 测试情况
   - **版本号联动**：`fix`→patch、`feat`→minor、`BREAKING CHANGE`→major（本插件版本号四处同步见坑 12）
   - **不提交无边界的格式化/ruff/导入整理/大面积实现整理**（MaiBot 原文）——这类 diff 会淹没真实改动、无法 review；确需整理时单独成提交并在正文说明范围，不与功能改动混在一起
   - **自检标准**：reviewer 不点开 Files changed 就能懂 = 合格；只写"优化"= 不及格

10. **新装纯净（项目首要红线，owner 2026-09-01 确认）**：任何改动之后，插件被安装后的状态必须等于一个**干净的新插件**——无运行时数据（学习库/观察账本/缓存/日志）、无任何本机部署信息（容器名/端口/容器内路径/会话 ID/真实人设/QQ 号）、默认值中性且对齐 MaiBot 官方。交付包内容必须 ≡ `git ls-files` 全集；提交前过 AGENTS.local.md 的红线 grep；详细验收标准见 §9「纯净交付红线」。

## 6. 常见任务

- **加配置项**：_conf_schema.json 加字段（键名/默认值/描述先查 MaiBot official_configs.py 对标）→ （若影响 prompt/评分）core 对应函数读 `cfg.get(...)` → 页面 render 对应分栏加 field + collect() 收集 → 测试补断言。
- **加 MaiBot 工具等价物**：装好对应 AstrBot 插件 → 在 `chat_tools` 默认值与页面说明中加入工具名 → MAIBOT_TOOL_EQUIVALENTS 补映射记录。
- **调"话痨程度"**：talk_value（全局）或 talk_value_rules（按群/时段）；单群用 chat_prompts 给语气要求。
- **排查为何不说话**：`docker logs <astrbot容器> | grep maisoul` 看门控明细（频率/模式/pending/阈值/空窗补偿/评分）与 planner 决策日志（LLM 返回 tools=…/reply/wait/无动作结束/学习）。
- **不接 QQ 测试整条管线**：WebUI 聊天页直接发消息（webchat=私聊事件，v6.6 起被接管）；或聊天里发 `/maisoul sim <文本>` 强制走私聊全管线并输出门控明细（v6.13.4 修复：waking_check 只剥唤醒前缀、CommandFilter 不改写 message_str，handler 里 raw 仍带命令名"maisoul"——子命令解析须先剥命令名，否则 sim 恒落状态分支）；或调 dashboard API：
  ```bash
  curl -X POST -H "Authorization: Bearer <JWT>" -H "Content-Type: application/json" \
       -d '{"session_id":"webchat!<用户名>!<uuid>","message":"晚上好","stream":false}' \
       http://<dashboard地址>:<端口>/api/chat/send
  ```
- **切换发言模式**：聊天里 `/maisoul planner|independent|native`。
- **curl 直调插件 WebAPI**：HTTP 路径前缀是 `/api/v1/plugins/extensions/<插件名>/<子路径>`（如 `/api/v1/plugins/extensions/astrbot_plugin_maisoul/config`；旧式 `/api/plugins/extensions/...` 不存在会 404）；`/api/plugin/reload`、`/api/chat/send` 则无 v1 前缀。测试工具/技能列表：`GET .../astrbot_plugin_maisoul/tools`；麦麦观察：`GET .../astrbot_plugin_maisoul/monitor/replay?since=<id>&limit=300`。
- **铸 dashboard JWT**（所有 dashboard API 都要 Bearer；600~900s 过期，过期报 "Token 过期" 就重铸）：
  ```bash
  sudo docker exec <astrbot容器> python3 -c "
  import json, time, hmac, hashlib, base64
  cfg = json.load(open('<AstrBot数据目录>/cmd_config.json', encoding='utf-8-sig'))
  s = cfg['dashboard']['jwt_secret']
  b = lambda d: base64.urlsafe_b64encode(d).rstrip(b'=')
  h = b(json.dumps({'alg':'HS256','typ':'JWT'}).encode()) + b'.' + b(json.dumps({'username':cfg['dashboard']['username'],'exp':int(time.time())+900}).encode())
  print((h + b'.' + b(hmac.new(s.encode(), h, hashlib.sha256).digest())).decode())
  "
  ```
- **拿 webchat 会话 id**：`sudo docker logs <astrbot容器> --since 2h 2>&1 | grep -o "webchat![^ ,)\"']*" | sort -u`（形如 `webchat!<用户名>!<uuid>`）。

## 7. 与 MaiBot 的保真对照 & 取舍清单（未复刻项）

**v6.4 已源码级复刻**：三件套 prompt 结构（maisaka_replyer）与输出指令原文、
触发门控全套（reply_trigger_mode 双模式：frequency 的 ceil(1/f)+空窗补偿+强制触发、
reply_necessity 的档位制评分——噪声清洗/问句正则/叫别的AI抑制/短反应惩罚/压力分/
存在感惩罚/频率倍率）、talk_value 动态规则（目标×时间优先级）、回复后处理管线
（括号心声清除/呃呃/超长默认回复/分句规则+概率合并/条数上限/合并到 max_split_num/
颜文字保护）、拼音字频错字引擎（同音字/声调/整词替换+纠正消息）、打字延迟
（×typing_speed）、同目标防重复、额外 Prompt 精确匹配多条拼接。

**v6.5 追加（聊天学习子系统，core/learning.py）**：表达学习（learn_style.prompt
原文提取 + expression_evaluation 四条基准自查 + legacy 随手抽样注入【表达习惯参考】，
库满 10 条启用；LLM 二次选择路径未接，走 MaiBot 的"直接注入"路径）、黑话学习
（learn_jargon.prompt 原文提取 + jargon_inference_with_context 含义推断，上下文
命中注入【黑话参考】）、关键词反应（keyword_rules/regex_rules + [命名捕获组] 替换）、
优化上下文（自己的旧发言只保留最近 3 条）。学习库 data_learning.json 按共享组分库，
WebUI「学习」页可视化管理，学习 API：GET/POST /astrbot_plugin_maisoul/learning。

**maisoul 独有扩展（MaiBot 没有）**：多人格管理（personas.py）——人格库+群绑定+默认人格，并兼容 astrbot_plugin_persona_switch（它通过 conversation_manager.update_conversation(persona_id=...) 切换，maisoul 读 conv.persona_id 实现同名联动；解析链见 §2 注释）。主配置人格：人格库固定首行，**人格名即主配置 bot_name（如"麦麦"），find_persona 对该名字（或"主配置"）返回主配置本身**，因此群里发 `/persona 麦麦` 就能切到主配置人格；库内同名人格优先。WebUI 首行 `openPersona('main')` 弹窗直接写回主配置（与「麦麦设置」同步），无删除/批量删除；库内人格行才有 checkbox 与删除。别名是单一列表 `aliases`（对应 MaiBot alias_names，身份行+提及检测共用）；旧 `nicknames` 键在 initialize 时自动合并进 aliases。人格弹窗字段文案与 MaiBot official_configs.py 官方标签/描述逐字一致（机器人昵称/别名/人格设定/表达风格/行为风格/群聊提示词）。

**未复刻 / 依赖 AstrBot 生态替代（均为有意取舍）**：

| MaiBot 能力 | 处理 | 原因 |
|---|---|---|
| A_memorix 长期记忆（五模式 query_memory） | 未复刻；livingmemory 经生态注入桥全模式生效（v6.11.0 起，含记忆沉淀回写） | **astrbot_plugin_livingmemory 更成熟**（完整生命周期/被动捕获/WebUI 管理），MaiBot 记忆对 maisoul 独立模式不可见 |
| emoji_system + 偷表情 | 未复刻 | **astrbot_plugin_stealer 本身就是它的移植+增强**（情绪匹配/LLM选图/WebUI管理），通过 chat_tools 暴露 send_meme 达成同等体验 |
| 行为学习/高频词学习 | 未复刻 | 收益/成本比低（行为模式需要独立聚类存储与维护任务） |
| Planner agent 循环（wait/中断/focus/注意力漂移） | **待完全复刻**（规格见 §7.1），当前以规则评分门控近似 | 用户已确认要求完全对标；近似版只是过渡 |
| 关键词反应规则（keyword_reaction） | **已复刻**（v6.5 core/learning.py） | keyword_rules/regex_rules + 命名捕获组替换 + 【关键词反应】注入 |
| 富回复（attach_pic/attach_at/引用回复） | 未复刻（引用回复原语 AstrBot 侧具备但未接） | 后续可接 MessageChain Reply 组件 |
| 世界书/好感度 | 无对应 | **AstrBot 的 worldbook/xinxian 做得更好**，经生态注入桥全模式生效（v6.11.0） |

### 7.1 已实现：Planner 决策层（v6.6，完全对标 maisaka，mode=planner 默认）

> core/planner.py + main.py 决策模式。系统提示词 = maisaka_chat.prompt 原文
> （裁去 tool_search/deferred tools/view_forward_message 三行——工具集
> 里没有这些工具，悬空引用会诱导模型调用"未知工具"；将来把 AstrBot 工具以
> deferred+tool_search 方式接进 planner 时连工具一起恢复原文）
> （已用 MaiBot 容器原文 diff 验证一致），工具声明 = builtin_tool 原文。

已移植：agent 循环（MAX_INTERNAL_ROUNDS=10，多轮工具调用，send_emoji
结果回填下一轮，contexts 跨轮累积）；工具 reply（msg_id/set_quote/reply_reference/
reply_style 枚举——篇幅由 Planner 参数指定，独立模式仍用启发式代选）、wait（连续
上限 max_consecutive_wait_count=3，超限=对话休息；期间新消息不提前打断）、
send_emoji（声明无参数=MaiBot 原样，执行桥接 send_meme）；
WAIT/RUNNING 状态机（群聊 wait 不唤醒，@/提及必回主动触发恢复 resume_from_wait）；
思考打断（planner_interrupt_max_consecutive_count=0 默认不打断）；空闲指数退避
（base15×2^n 封顶 300、起点 2、积压 6 绕过、reply 重置）；wait 到期有积压自动续轮。
behavior_style 分工已改回 MaiBot 语义：只进 planner 系统提示词（v6.13.1 起彻底落实——replyer 的 build_system_prompt 与 native 三件套注入不再追加「行动准则参考」块，MaiBot 的 replyer 模板本就没有 behavior_style）。
fetch_history 已移除（v6.13.5）：MaiBot focus 模式专属工具（要求
experimental.focus_mode，部署版 false → 请求工具集里没有它），maisoul 同步不暴露。

运行期修正（v6.6.x，均为对齐 MaiBot 行为的补齐）：
- planner 请求首轮以 contexts 注入最近历史（群 40/私聊 60 条，对齐 chat_history 传参）
- **历史分析跨轮回灌（v6.13.4，坑 52）**：contexts 初始段 = 聊天记录 + 历史
  planner 分析按时间戳交错（`planner.build_history_contexts`，分析进 assistant
  轮；2× 稳定窗在合并流上截取）；每轮分析记入 `PlannerState.analysis_log`
  （deque 200）供下一轮回灌——对齐 MaiBot `build_model_output_context_messages`
  把 planner 输出写会话历史、`select_llm_context_messages` 在合并流上选窗的
  机制。这是部署版 planner 分析呈「当前状态/分析/下一步」结构的来源（提示词
  并无此格式要求，模型自发 + 看到自己历史分析而自我强化）；maisoul 此前每轮
  重建不回灌，格式零样本漂移（中英混杂、随笔式）。工具调用/结果轮仍只在循环
  内存活（fold_old_turns 折叠，见 §7.2 工程差异）。
- 工具结果轮（tool_continue）不要求新消息即继续，直到 reply/wait/无工具/轮数上限
- contexts 顺序 user→assistant；wait 到期有积压自动续轮
- v6.13.2 planner 请求三处保真补齐（其中 ①② 在 v6.13.5 经请求 dump 复核发现
  对齐错了对象，已重做，见坑 53）：
  ① 消息渲染格式 = 部署版 `planner_messages.build_planner_prefix` 原文
  `<message msg_id="…" [quote="…"] time="…" user="…" [group_card="…"]
  [is_self_message="true"]>\n内容`（无闭合标签；v6.13.2 曾误对齐
  format_speaker_content 的可见文本格式 `HH:MM:SS[msg_id:x][说话人]内容`——
  那是 display 用的，planner 请求不用）；
  ② 全部聊天消息（含自发消息，带 is_self_message）进 **user 轮**，只有 planner
  分析进 assistant 轮（v6.13.2 曾误把自发消息放 assistant 轮）；
  ③ 每轮请求末尾追加一次性提醒原文（chat_loop_service.PLANNER_FINAL_USER_
  REMINDER_TEMPLATE："你需要输出对{bot_name}发言的分析，视情况输出文本内容的
  分析，思考是否进行工具调用"）

v6.7.0 追加：
- **planner 模式的 replyer 也接聊天工具集**（chat_tools + call_maid + chat_skills 注入，
  此前只有独立模式有——管家在 planner 默认模式下被丢失）；reply 工具的文本生成步
  可发起工具调用，结果回填后二轮生成，与独立模式同构
- 工具执行改走官方 `FunctionToolExecutor.execute()`（见 §1.2/§8 坑10），send_meme
  的 send_emoji 桥同样修复
- WebUI「聊天工具暴露」卡新增选取弹窗（TOOLS|SKILL 双标签，数据 GET /tools，
  取数对齐官方 ToolsService.get_tool_list 与 SkillManager.list_skills）
- planner 无动作时日志附模型陈述（便于排查"为什么不回"）

**麦麦观察（对齐 maisaka/monitor）**——core/monitor.py + WebUI「麦麦观察」页。
- **MaiBot 前端有两套（重要）**：MaiBot 仓库的 `dashboard/src` 是前端 TSX 源码（较新），
  但部署实例实际跑的是 pip 包 `maibot_dashboard` 的预编译 dist（另一时间点构建）。
  `/planner-monitor` 路由两套都指向时间线版 MaisakaMonitor（`routes/monitor/index.tsx`）；
  `planner-monitor.tsx`（三级页面）是源码里未被路由引用的组件。**以部署版（用户看到的
  页面）为准**，比对方法：抓 `assets/index-*.js` 里的中文字符串。
- 事件集 = **MaiBot events.py 的 9 个 emit，一个不多一个不少**：
  session.start / stage.status / stage.removed / llm.retry / llm.error /
  message.ingested / message.sent / message.updated / planner.finalized。
  **timing_gate.result / planner.response / replier.response / tool.execution 是部署前端的
  遗留渲染分支（switch 里存在），MaiBot 后端不 emit（已 grep 实锤 events.py），页面永不出现
  ——maisoul 也不准发**（"有真实信号就接上"是错的，多一张卡就是不对标；
  前端渲染分支保留 = 与部署前端代码一致）。llm.retry / message.updated 无信号同样不接。
- maisoul 实际接线：session.start（会话首次进管线）、stage.status（阶段名照抄
  reasoning_engine：启动循环/消息整理/Planner/Planner 已打断/工具执行·X/Replyer/等待消息/
  错误）、message.ingested（_record）、message.sent（reply/emoji 每段）、planner.finalized
  （每轮决策聚合：request/planner/tools/final_state 嵌套结构原样；end_reason=wait 时前端
  渲染"本轮思考暂时结束"卡、interrupted 时渲染打断卡）、llm.error（text_chat 异常）。
- **planner 文本（Planner 卡正文）**：MaiBot 取 `response.content`；AstrBot 的
  LLMResponse 同义字段是 completion_text，但 Claude 等模型在工具调用轮把分析放进
  thinking 块 → `_planner_cycle` 里 analysis = `_resp_text(resp)`，为空时回退
  `resp.reasoning_content`（漏读 thinking 块时页面永远显示"planner 本轮没有文本内容"）。
  实测 158 字分析正常落库展示。
- 接线点速查：`_record`→ingested；`_schedule_planner`→session.start+启动循环；
  `_planner_cycle`→消息整理/Planner/工具执行·X/等待消息/错误+finalized(所有出口)；
  `_planner_execute_reply`/`_generate_and_send`→Replyer 阶段+sent 每段+llm.error。
- 存储写法照抄：SQL 表 **maisaka_monitor_events**（SQLModel 表类逐字段对齐
  database_model.py：列/四个索引/默认值），record/replay/cleanup 逐行对齐
  event_store.py（含 flush 取 event_id 回写 payload_json、两条 DELETE 语句、
  每 200 条或 60s 检查清理）。MaiBot 挂自身 MySQL，maisoul 无法在 AstrBot 主库
  建表 → 独立 SQLite data_monitor.db 承载同一张表（仅会话工厂差异）；文件
  存 AstrBot 持久化目录 data/plugin_data/<插件名>/（坑 50，卸载不删数据时
  幸存；早期 JSON 账本自动一次性迁移并改名 .json.imported）。
- 推送适配：MaiBot websocket broadcast → 插件页轮询 GET /monitor/replay?since=
  （2.5s，SSE 端点已注册但本部署被适配层缓冲，见 §8 坑14；前端先试 SSE 6s 看门狗
  无帧自动降级轮询）。
- **观察页 UI = 部署版像素对齐**。仿照对象不是 TSX 源码而是
  部署实例的前端 chunk（页面逻辑 JS + lucide 图标 chunk + `assets/index-*.css` 的 `:root` 换算 hex）。要点：
  - 布局：根 `flex gap-16px lg:flex-row`；aside 整圈 border + `bg-background/45`、
    w-52/w-16 过渡 0.2s；头部 activity 图标+「聊天流」+连接绿点(h-2)+折叠 ghost 钮
    （chevron-left/right h-3.5），折叠态存 localStorage（键照抄
    maisaka-monitor-sidebar-collapsed，默认折叠）；主列=阶段条+时间线 Card
    （bg-card rounded-lg(8px) border shadow，min-h-420px）。
  - 阶段条：有状态 `bg-background`、无状态 `bg-muted/30`，均为 rounded-md(6px)
    border px-2 py-1 横向滚动；**顺序=统计chip→回到底部/清空（ml-auto）→阶段徽章组
    →更新于(ml-auto)→detail**（部署版按钮在中间）；统计 chip h-6+activity h-3+
    「统计」10px，数值只在 title 悬停（消息：/循环：/工具调用：）；阶段徽章
    default+activity h-2.5、轮次 secondary、agentState running→default 其余 outline
    （均 px-1.5 text-10px）；agentLabel 照抄 zt：空/stop→无，running→运行中，
    wait→等待中，其余原样（**idle 不映射**）。
  - 工具卡「执行结果」= tool_record.summary：**截断 2000 字符，对齐 MaiBot reasoning_engine._build_tool_result_summary 的 max_length=2000**（v6.11.3 前误用 200，观察页只见结果开头一段；模型回填不受影响，一直走全文）。
- **生态注入内容展示（v6.11.6）**：replyer 收集的注入全文经 `PlannerState.eco_injection` 带进 planner.finalized 的 `final_state.eco_injection`（maisoul 扩展字段），「推理过程」弹窗内「生态注入」区折叠展示（6 行起折）。注入本身在 v6.11.0 已生效（群聊每轮 500~600 字符，日志「生态注入桥执行 N 个钩子，收集 X 字符」可查）。
- **工具明细收纳进「推理过程」弹窗（v6.11.4，用户指定的偏离）**：部署版的「推理」ghost 按钮开的是 prompt_html_uri 推理页（maisoul 无该产物，此前因无信号不渲染）；maisoul 改为 Planner 卡挂「推理过程」按钮（mo-rbtn，仿部署 ghost sm h-6 px-2 text-10px）→ 弹窗内展示 Planner 思考全文 + 工具完整明细（moToolRow 原样）。时间线默认只显示单行工具摘要（moToolMini：名称+成败徽章+耗时）——默认看结果，点按钮看工作流。
- 卡片逐张照抄部署版渲染 switch：ingested（蓝圆头像+首字+名+时间，无框）、sent
    （绿框卡 emerald-500/30+5% + bot 图标头像 + outline 10px「已发送」）、
    timing_gate（bg-background+shadow-sm 边框卡 + amber 圆 timer + 「反应」+outline
    「react」+动作徽章带图标：continue=arrow-right default「继续执行」/wait=
    circle-pause secondary「等待」/no_action=user destructive「不回复」）、
    planner.response（无框行 + emerald 圆 brain「规划器思考」+ 6 行折叠 + 工具 chip
    secondary+wrench h-2.5）、finalized（emerald/60 左边框 Card：brain+Planner+
    outline ml-auto 时长+secondary「上下文 N 条 / 可用工具 N」；**timing_gate.action=
    no_action 时整个 finalized 不渲染**；interrupted→amber 打断卡 circle-alert
    「Planner 被新消息打断」+#cycle+默认文案）、工具块（teal/60 左边框 Card
    wrench「使用工具」+「N 个」；工具行=mono 工具名+circle-check emerald/user red
    h-3.5+h-5 徽章「执行成功/执行失败」+时长+#序号+参数 chip（h-6 mono name=value
    max-w-288 截断）+「完整调用 JSON」虚线 summary（chevron 开时旋转 90°）+
    「执行结果」框「未返回结果摘要。」；**finish 工具过滤出列表单独渲染为
    「本轮思考暂时结束 等待新的消息。」绿卡/绿条，maisoul 的 wait 伪工具记录即
    finish 语义**）、replier（purple/60 左边框 Card bot「回复器响应」+outline ml-auto
    时长+secondary/destructive「成功/失败」徽章带 circle-check/user h-3+思考过程
    details）。
  - **部署版渲染 switch 的 default=null**：未列事件一律不渲染——llm.error 落库但
    无卡（自造红卡已删，禁止再造）；token 徽章（prompt+completion）、模型名行、
    Provider 原生工具块（earth/sky）、推理按钮（file-code-corner）均因 maisoul 无
    信号不渲染。
  - 折叠文本照抄 CollapsibleText：折叠态行用**空格**拼接（非换行）、按钮
    text-primary 12px hover 下划线 + chevron-right/down h-3 + 「 展开全部 (N 行)」
    /「 收起」；贴底阈值 80px；事件行 pb-3 + fade-in 300ms。
  - 行为照抄：**时间线不过滤会话**（选中只切阶段条）；首个 session.start 自动选中
    （选中不toggle）；会话项 hover:bg-accent/50、选中 bg-accent 白字；事件计数徽章
    secondary h-4 px-1 text-10px；相对时间+当前阶段（primary 色）第二行。
  - 图标：页面内联 `MO_ICONS`+`moIcon(name,size)`（15 个：activity/bot/brain/timer/
    wrench/circle-alert/circle-check/clock/chevron-down/right/left/eraser/
    arrow-right/user/circle-pause，viewBox24 stroke2 round），**禁止 emoji**。
    提取方法：icons chunk 里 `const X=[[...]],name=a("icon",X);`，从 `a("name"` 锚点
    **向前**找 `=[` 数组（向后找会拿到下一个图标的数组）。
  - 主题 hex 集中在 `:root{--mo-*}`：primary #e06f06 / card #fbfcfc / secondary
    #f1f7f8 / muted-fg #608990 / accent #55ab49（选中会话绿底）/ border #e5eced /
    destructive #d31212 + tailwind 原样 emerald-500 #10b981、blue #3b82f6、
    amber-500 #f59e0b、teal #14b8a6、purple #a855f7、red #ef4444；字体栈
    -apple-system…Arial；徽章基类 rounded-md(6px) px-2.5 py-0.5 text-xs(12px)
    font-semibold；空态文案逐字：「等待 MaiSaka 会话…」「等待 MaiSaka 推理事件…」
    「当 MaiSaka 处理新消息时，推理过程会实时展示在这里」「当前聊天流暂无阶段状态」。
- planner.finalized 载荷字段说明：**token 用量已接通**——AstrBot
  `LLMResponse.usage`（TokenUsage：input_other+input_cached=输入、output=输出）
  真实回传，`_planner_cycle` 逐轮累计进 `planner.prompt_tokens/completion_tokens/
  total_tokens`，前端 Planner 卡头部按部署版形态渲染「输入+输出 tokens」outline
  徽章（>0 才显示）。native_tool_calls/prompt_html_uri 仍是 MaiBot Provider
  专属，缺省即无；end_reason 用 maisoul 真实出口名（reply/wait/no_action/
  max_rounds/no_new_message/interrupted/error/no_provider）。

未移植（maisoul 无等价基础设施，未伪造）：focus 专注模式、注意力漂移、行为表现
情景分析子代理、query_memory/view_forward_message/switch_chat/tool_search 工具。

**整个插件 WebUI = 部署版原版 dashboard 风格（modern）**——不止麦麦观察，
全部 7 页（概览/麦麦设置/发言节奏/人格管理/学习/管家桥/观察）与外壳都换成 MaiBot 部署版
`maibot_dashboard` dist 的 modern 主题（= 用户选的"原版 dashboard"；dist 的 CSS 基础层就是
modern，future-retro 是 303 个 `[data-dashboard-style=future-retro]` 覆盖选择器，早期
纸张风正是错抄了 future-retro）。提取来源与规格：
- **壳**：左侧栏 208px（--layout-sidebar-width 13rem）整圈 border + 右边线、Logo 区 h-20
  border-b（"MAISOUL" 800 字距 .1em）、组标题 text-sm 600 uppercase muted/60、菜单项
  h-10 rounded-lg px-3 + 图标 20px + 标签 text-base(16px) 500，hover/选中=bg-accent 绿底
  （选中时图标 text-primary）；顶栏 h-12 border-b（sticky）+ 折叠钮(chevron-left/right)+
  页名 + 版本徽章 + 保存按钮；内容区 p-4 sm:p-6 space-y-4/6 max-w-1080 滚动。
- **导航分组照抄部署版侧栏文案**：概览(首页/麦麦观察) / 麦麦配置编辑(麦麦设置/发言节奏) /
  麦麦资源管理(人格管理/学习) / 扩展与集成(管家桥)。图标 = 同一部署 icons chunk 的 lucide
  （house/activity/settings/clock/user/book-open/wrench）。
- **组件类全部按部署版 cva 抄**：panel=配置分区 `rounded-lg border bg-card p-4 sm:p-6
  space-y-3` + h3 text-base 600 + pdesc text-sm muted；frow=表单行（sm 起 label 左/控件右
  320px，wide 整行）；ui-btn（default/secondary/outline/ghost/destructive × sm/icon）；
  ui-switch h-5 w-9（选中 primary，thumb translate-x-16px）；ui-tabs（list bg-muted
  rounded-lg p-1 + active bg-background shadow-sm）；ui-tbl（th text-xs muted 500 / td
  border-b / hover bg-muted/50）；statgrid/statc=统计卡（label text-xs muted + 值
  text-lg 600 tabular-nums，概览大值 text-2xl 700 primary）；ui-badge 同观察页四变体；
  弹窗=mask 黑 50% + rounded-lg border shadow；chips=secondary 底小徽章。
- **主题令牌**：`:root` 用部署版 HSL 三元组（--primary 28.9 94.8% 45.1% 橙 / --accent
  112.7 40.2% 47.8% 绿 / --muted-foreground 188.5 20% 46.9% 等），排版 var 同部署版
  （base 16px / sm 14 / xs 12，mono=JetBrains Mono）。**禁止 future-retro 纸张令牌
  （--paper/--rust/--ink/--cream）与 Georgia 衬线**。
- **图标禁 emoji**：全站图标走页面内 `MO_ICONS`+`moIcon()`（39 个 lucide path 数据，
  提取法见 §7.1 观察页小节）；按钮图标 = save/plus/trash-2/search/pencil/x 等。
- 交互保留：collect()/save() 的全部字段 id 与行选择器（data-cp/tvr/gp/elr/egr/kwr/rxr/
  le-*/lj-*）原样保留，模板换壳不换数据面；工具/技能弹窗、人格弹窗、学习库编辑逻辑不变。
- 页面映射：概览→部署版首页统计卡+表格卡；麦麦设置→bot 配置页（h1+ui-tabs 核心/详细+
  分区）；发言节奏→bot 配置分区集；人格管理/学习→资源管理页（统计条+搜索工具栏+ui-tbl）；
  管家桥→maisoul 独有，按同一分区样式自写。

### 7.2 已知非对标项清单（v6.6 起）

1. **上下文内联（仅 replyer 单轮路径）**：MaiBot 把聊天记录/表达习惯/黑话参考作为
   独立 context 消息；maisoul 的 replyer 生成内联进一条 user message（Planner 循环
   已用 contexts 参数跨轮累积）。信息等价，结构不同。
2. **黑话高频词提示未移植**：依赖 MaiBot 独立的高频词学习器（属已取舍的学习器家族）。
3. **引用回复（部分接通）**：reply 工具 `set_quote`（默认 true）与独立模式
   `enable_reply_quote` 已接——首段 MessageChain 挂 `Reply(id=目标消息id)`，aiocqhttp
   发送侧 toDict 为 OneBot reply 段。**错字纠正 `quote_previous` 仍未接**：引用对象是
   自己刚发的消息，而 `context.send_message` 只返回 bool 拿不到 message_id（见坑 21），
   维持普通文本发送。
4. **focus/注意力漂移/情景分析子代理**：未移植（见 §7.1）。
5. **maisoul 独有扩展（MaiBot 之外的加项）**：预设对话（preset_dialogues，v6.13.0——示例对话 {user, reply} 注入【预设对话】块作风格参考，人格库条目可覆盖）、多人格、管家桥（call_maid 桥+单轮回填）、
   independent/native 模式、逃生舱（v6.10.0 起默认关闭=全面接管，escape_at_wake 可开）、总开关、native 三件套注入。
6. **用户明示同意的取舍**：A_memorix→livingmemory、偷表情→stealer、行为/高频词
   学习、mid_term_memory、世界书/好感度。
7. **工程差异（行为一致）**：错字引擎 jieba 词典进程内缓存；学习器为发言后异步任务
   （受 max_expression_learner 信号量约束）而非逐消息队列。

## 8. 关键规范与坑（按主题分组，只记结论与数值）

### 8.1 AstrBot 框架硬行为

1. **私聊/webchat 恒置 `is_at_or_wake_command=True`**（waking_check 源码行为）——逃生舱的唤醒放行只对群聊；私聊一律进管线（MaiBot 私聊语义）。
2. 原生 LLM 路径要求 `event.is_at_or_wake_command`（非 is_wake）；门控放行原生时设前者。
3. `call_event_hook` 逐 handler 捕获异常（`except BaseException: logger.error`）——其他插件在钩子里崩溃**不阻断管线**，只废掉它自己的注入。
4. **`text_chat` 唯一可按次覆盖的参数是 `model`**（openai/anthropic/gemini 三实现均为 `model or self.get_model()`）；temperature/max_tokens 只能走 provider 级 `custom_extra_body`。跨厂商用 `provider_manager.inst_map` 直调实例。
5. `context.send_message` 返回 bool 无 message_id、`event.send()` 返回 None → **引用自发消息不可行**；引用用户消息用 `MessageChain([Reply(id=…), Plain(…)])`（aiocqhttp 发送侧 toDict 为 OneBot reply 段）。
6. `provider_sources_config` 才是「模型提供商」清单；`providers_config` 是模型条目（按 `provider_source_id` 归源，同源共享上游与目录）。
7. **object 型配置 schema 逐字段要求 `type`**——任意嵌套结构必须用 **list 型 + dict 默认值**，运行时 `normalize` 规范化。
8. **metadata.name 是显示名且决定页面链接与桥接前缀**——WebAPI 必须**双前缀注册**（插件标识 + 显示名）；目录名/`@register` ID/旧 API 前缀永不可动。
9. SQLModel 表类热重载会重复注册同名表 → 定义前 `SQLModel.metadata.remove(...)`；sessionmaker 用 `class_=sqlmodel.Session`。
10. `@filter.llm_tool` 工具不能直接 `tool.call()`，必须走 `FunctionToolExecutor.execute()`；统一入口 `bridge.call_llm_tool(context, event, tool, args)`，无原始 event 用 `bridge.SyntheticEvent(umo)`。
11. 插件 WebAPI 的 HTTP 前缀带 `/api/v1`（§6）。
12. 版本四处同步：metadata.yaml（日志读它）、`@register`、PAGE_VERSION、status API。

### 8.2 插件页与桥

13. 页面跑在 sandbox iframe（无 allow-same-origin）——一切 API 必须走桥；SDK 注入晚于内联脚本且资源令牌 60s 过期 → 轮询等待 + 内置备用桥（postMessage，channel `astrbot-plugin-page`）。
14. 内联脚本**禁写 `</body>` 字面量**（注入逻辑替换首个 body 闭合标签会截断脚本，规范 §5.4）。
15. 页面内容接口有缓存：改 pages/ 后重载插件 + 浏览器 Ctrl+F5。
16. 桥 `apiGet(endpoint, params)` 支持查询参数（axios 转 query）——页面包装层必须透传 params，丢弃=增量语义失效。
17. **SSE 会被扩展层整包缓冲**（`_quart_response_to_starlette` 对 quart Response `await get_data()`）——实时数据用轮询 `/monitor/replay?since=`；SSE 端点保留，前端策略为**轮询保底常开、SSE 真正收到帧（含 stream.open）才停轮询**，看门狗要在 `await subscribeSSE` 之前注册（promise 悬死时后注册的定时器不会跑）。
18. 观察页时间线/统计必须按 `MO.sel` 过滤、`moSel` 全量重绘——单会话环境测不出，改会话 UI 必须造两个会话验证。
19. `moIngest` 按 event_id 去重（SSE+轮询双通道投递）；`moClear` 同步清 seenIds。

### 8.3 webchat

20. 聊天 API 的 SSE 流在 handler 返回即关——webchat 必须**同步 await 整个决策循环**，段落经 `event.send()` 流进当前请求；后台任务只能落 proactive 存库（页面秒回空气泡）。
21. run accumulator 对 `streaming:False` 的 plain 事件是**替换语义**（`pending_text = result_text`）——webchat 多段必须攒段、结束后 `\n\n` 合并一次 `event.send`；`_emit_sent` 仍逐段发（保观察页断句记录）。
22. webchat 不排空窗重查（流已关）、native 不排（需活跃管线）。

### 8.4 会话与调度

23. **会话键全链路一致**（群=group_id，私聊=sender_id），回声钩子同样——不一致的症状是"空回复且无日志"。改会话代码 grep 一遍 gid 来源。
24. 间隔统计四规则（`GroupState.avg_external_interval` 单点实现）：30min 样本窗、**<5s 连发不采样**、均值下限 30s、见过消息无样本回退 30s、从未见过才 None。
25. 空窗重查：`delay=(阈值-积压)×均值-空窗`，到点**无新消息也重评**（安静群主动补话）；句柄 `GroupState.defer_task`，触发/发言后 `cancel_defer()`，回调内再验 running/wait/firing。
26. **wait 到期必续轮**并注入完成回执（`planner.build_wait_completed_message` 两版原文）——只在 pending>0 时续轮，会让异步工具（管家返回 running）后的循环静默死亡。
27. 消息去抖 1.0s：开轮前等 `last_ext_ts` 静默窗（`MESSAGE_DEBOUNCE_SECONDS`）。
28. 首段零延迟：`send_humanlike` 仅 `index>0` 打字延迟（MaiBot `typing=index>0`）；webchat 合并发送时延迟前置。

### 8.5 planner 管线

29. **分工铁律：planner 唯一干活者、replyer 纯嘴**。可见工具 4 个（reply/wait/send_emoji/tool_search；fetch_history 为 MaiBot focus 专属、v6.13.5 移除）；管家与生态工具全部进 **deferred 池**（tool_search 发现后下一轮可用；打分表 1000/300/200/100/25/10、返回文案、`<system-reminder>` 模板均为 MaiBot 原文；提醒只进当次请求不进 contexts 历史；`PlannerState.discovered_tools` 会话级）。replyer 不带 func_tool（independent/native 模式例外，管家桥留 replyer 侧）。
30. （v6.13.5 废弃）fetch_history 已整体移除：MaiBot 侧它是 focus 模式专属工具（`_is_builtin_tool_enabled_by_config` 要求 `experimental.focus_mode`，部署版 false），工具集/结果格式/context_msg_ids 去重机制一并删除——部署版 planner 的 7 工具集（wait/reply/query_memory/query_person_profile/send_emoji/send_image/tool_search）里根本没有它。
31. planner 上下文 2× 稳定窗（`max(base, base×2)`），相邻消息跨日插 `时间：YYYY-MM-DD HH:MM:SS` 行。
32. 防复读：本轮思考与上轮 difflib 相似度 >0.9 → 替换固定反思文本（`planner.PLANNER_REFLECT_ON_REPEAT`）；上轮存 `PlannerState.last_analysis`。
33. **黑话参考注 planner 每轮**（`jargon_reference_block` 的 exclude/matched_out 做轮间去重）、**表达习惯注 replyer**——位置不可颠倒。
34. 本轮上下文折叠：保留最近 3 组 user/assistant，更早一次性折叠为「[已折叠的历史工具调用]」摘要——**列表内联折叠必须单趟**（while 逐对重折会在折叠块自身 1 换 1 死循环）。
35. 过滤词 ban_words/ban_msgs_regex：注册前整条丢弃（不进缓存不进门控），指令类（escape）不查（对齐 bot.py:801）。
36. 回复引用机器人 = 提及档（不算 at；当前消息或未消费积压内任一 reply_bot 命中即算）。
37. planner token 用量：`LLMResponse.usage`（input_other+input_cached=输入、output=输出）逐轮累计进 `planner.finalized` 的 planner 块。
38. 识图：Image 引用随消息入 buffer（只存引用不落盘）；`text_chat(extra_user_content_parts=[ImageURLPart(image_url={"url": …})])`——**image_url 必须传 dict**（裸字符串 pydantic 拒绝，类 docstring 有误导）；默认关，需视觉模型。
39. 任务级模型绑定（模型管理页）：配置 list 型 `task_models`，provider 填**源名**，`_resolve_bound_model` 按 `provider_source_id` 归源、精确 id 未命中取该源任一启用条目承载；三策略纯逻辑在 `core/modelbind.py`（sequential 顺序+异常降级链 / random / balance 轮转）；接入点五处：planner 轮、replyer×2、emoji 检索词、expression_use、learner。

### 8.6 工具桥/生态

40. stealer 两步制：`search_meme(query)` → `[N]` 编号候选（挂 `event._emoji_turn_state`）→ `send_meme(emoji_id)`；**search/send 必须复用同一 event 对象**（换对象= candidate_expired）。语境选择 = 子 LLM 产检索词（EMOJI_QUERY_PROMPT）→ search → 取首候选编号。v6.11.1 起暴露侧自动补全前置依赖（`bridge.TOOL_DEPENDENCIES`：send_meme→search_meme，chat_toolset 与 deferred 池同规则）——此前只暴露 send_meme，其描述指向模型找不到的 search 工具，表情包调用死局（planner 日志可见模型反复 tool_search 后放弃）。v6.11.2 再修一层：**候选挂在 event 对象上，整周期必须同一 event**——wait 续轮（_resume）不带 event 时逐调用现造 SyntheticEvent，search 存的候选下一步就消失（第二轮 candidate_expired 而首轮成功，即此差异）；修法 = `PlannerState.last_event` 持久化最近真实 event、续轮复用、真无 event 才每周期造一个合成事件（SyntheticEvent 补 `plugins_name=None` 供注入桥用）。

### 8.7 前端

41. flex 挤压口诀：**基准 0 且 min-width:0 的元素 + 兄弟要 100% 宽 = 必挤扁**（中文竖排）——wide 行纵向堆叠、普通行 wrap + 说明 `flex:1 1 240px`、不可压元素 `flex:none`。
42. CSS `var()` 嵌套回退是非法值（`hsl(var(--a, hsl(var(--b))))` → `hsl(hsl(…))` 整条失效变透明）——拆开写或用确定存在的 token。
43. 本机 node 12 不认 `?.`/`??`（语法误报）——页面 JS 检查用任一 node≥18 环境（容器或本机）`node --check`。

### 8.8 排查口诀

44. 见 `'coroutine' object is not iterable/has no attribute` → 先查上游 `async def` 调用点漏 `await`（**`or []` 挡不住协程**，协程恒为真值）。
45. 前端有渲染分支 ≠ 后端会发该事件——对标协议必须 grep **后端 emit 点**（MaiBot 为 `src/maisaka/monitor/events.py` 的 `_broadcast`，全集 9 个）；多一张卡就是不对标。
46. main.py 顶部必须 `import asyncio`；`PlannerState` 加字段后跑 webchat 端到端（缺字段的 AttributeError 走静默死亡路径）。
47. **生态注入桥必须直遍注册表，不走 `call_event_hook`**（v6.12.0 补：注入有**两个通道**——`req.system_prompt`（心弦/世界书）与 `req.extra_user_content_parts`（livingmemory 记忆召回特意走用户内容附加，它已废弃 system_prompt 方式保护前缀缓存）；桥返回 (block, extras)，extras 经 text_chat 的 extra_user_content_parts 传入（与识图同通道，二轮生成同传），只收 system_prompt 会静默丢记忆）——它逐 handler 检查 `event.is_stopped()`，而麦麦管线在 planner/independent 下早已 stop_event（静默闸门），第一个 handler 后就会中断。入口 `main._eco_inject_block`（收集 req.system_prompt 拼进 system_prompt，跳过自家 maisoul 模块防三件套重复）与 `_eco_fire_response`（LLMResponse(completion_text=全文) 触发记忆沉淀；先 `event.set_extra("maisoul_eco_resp", True)`，自家 on_llm_response 见此标记即返回，防回声重复记录）。接线点：_generate_and_send 与 _planner_execute_reply（replyer），planner 决策轮不注入——保持 maisaka 系统提示词纯净。单个插件注入异常只废它自己的注入（逐 handler 捕获，对齐框架行为）。

48. **`_schedule_planner` 所有分支必须返回 awaitable**——调用方统一 `await self._schedule_planner(...)`；裸 `return`（退避/wait 不可恢复/不打断/群聊 create_task 后的隐式 None）会让调用方 `await None` 抛 TypeError：`stop_event()` 被跳过、事件漏进原生管线（outputpro 的报错拦截「呜哇，<bot名>死掉拉～」每次触发即此因），状态机也被打乱。fire-and-forget 分支一律 `return asyncio.sleep(0)`（v6.11.5 修复，群聊路径自 v6.6 起带病）。同修：表情双路径收敛——send_meme/search_meme 移出 planner deferred 池（`bridge.DEFERRED_EXCLUDE`，内置 send_emoji 已覆盖两步制，同轮两路各发一次=双发表情）；独立模式 chat_toolset 保留完整两步制对。

49. **生态注入的 extra_user_content_parts 绝不能 `str()` 强转**（v6.12.1 修复）：生态插件往 `req.extra_user_content_parts` 写入的是 `ContentPart` 对象（TextPart/ImageURLPart）、裸 `str` 或消息组件（`Plain` 等），类型不统一；provider 侧（anthropic/openai source）逐块 `isinstance` 校验，只认 ContentPart 三类，**裸 str 直接 `ValueError: 不支持的额外内容块类型`**，replyer 请求在拼装阶段被打断 → planner 整轮循环异常 → 机器人沉默。旧代码 `[str(x) for x in parts if str(x).strip()]` 两重错：str 对象得到 pydantic repr（`type='text' text='…'`、`type=<ComponentType.Plain: 'Plain'>`），repr 非空能过 strip() 过滤，垃圾被当正文混进注入与观察页。**间歇性指纹**：livingmemory 召回是 RAG，命中才 append——附加 ≥1 段必崩、附加 0 段正常，表现"时好时坏"完全由该回合是否召回记忆决定。修法：`_normalize_extra_parts`（ContentPart 透传保留 `mark_as_temp` 的 _no_save 语义；裸 str 包 TextPart；其它对象取 `.text` 包 TextPart，取不到丢弃）+ 观察页 join 用 `_extra_part_text` 提取纯文本（无文本段以类名占位）。注意 `Plain` 与 ContentPart 不在同一棵类树上（isinstance 为 False），走 `.text` 提取分支。快捷指纹：日志出现 `不支持的额外内容块类型: <class 'str'>` 一定是调用方把本该传 ContentPart 对象的参数传成了字符串，grep 调用点的 `str(` 强转即可定位。

50. **运行时数据必须存 AstrBot 持久化目录，不能放插件目录**（v6.12.3 修复，用户实报）：AstrBot `uninstall_plugin` **无条件 `remove_dir(整个插件目录)`**，卸载弹窗的勾选框只控制配置文件（`data/config/<插件>_config.json`）与 `data/plugin_data/<插件名>/` 的清理——观察账本 `data_monitor.db`、学习库 `data_learning.json` 放插件目录里时，"未勾删除数据"的卸载也会连带删光（实机故障：人格在配置文件里幸存、麦麦观察数据全丢，正是这个不对称）。修法：`main._persistent_data_dir()` 经 `StarTools.get_data_dir("astrbot_plugin_maisoul")` 解析 `data/plugin_data/astrbot_plugin_maisoul/`，store 显式传路径；首次运行把插件目录旧文件（含 SQLite -wal/-shm 侧车与 .imported 遗留）搬过去。`data_char_frequency.json` 是随包分发的静态依赖，留在插件目录（删了重装即回）。

51. **`_resp_text` 提取 result_chain 同样禁止 `str()` 组件**（v6.13.3 修复，用户实报观察页 Planner 思考显示 `type=<ComponentType.Plain: 'Plain'> text=''`）：纯工具调用轮模型无正文，completion_text 为空 → 走 result_chain 分支，链上常是一个空 `Plain`，`"".join(str(c) ...)` 把 repr 当成思考文本，且非空结果顶掉了 `reasoning_content` 兜底——thinking 块的真实分析被 repr 冒充。修法：逐组件取 `.text` 拼接，图片等无文本段贡献空串；空结果让位给 reasoning_content。**通用教训：一切"对象转文本"的边界（生态注入/响应提取/展示 join）都取 `.text`，禁止 str() 整个对象**。（v6.13.6 补注：thinking 兜底只用于**展示**——回灌走可见正文，见坑 54。）

52. **planner 输出格式漂移的根因是"分析不回灌"，不是提示词**（v6.13.4 修复，用户实报"两边观察页 planner 输出规范完全不一致"的第二层）：部署版 MaiBot 的 planner 分析呈「当前状态/分析/下一步」结构，但其 `maisaka_chat.prompt` 原文**没有任何格式指令**（已抓部署容器 prompt 与请求 dump 实锤）——该结构是模型自发产生、再由会话历史回灌自我强化的：`build_model_output_context_messages` 把每轮 planner 输出写进 `_chat_history`，后续每次请求都带着这些旧分析（assistant 轮）作 few-shot 示例，格式即锁死。maisoul 提示词/reminder/模型均与部署版一致，但 contexts 每轮从聊天记录重建、分析不留存 → 每轮对格式都是零样本，同一模型写得随意（甚至整段漂英文）。修法见 §7.1「历史分析跨轮回灌」。**教训：对标"输出规范"不能只 diff 提示词——请求的完整消息列表（含回灌的历史输出）才是行为规格；MaiBot 侧的权威取材是 `logs/maisaka_prompt/planner/*.json` 请求 dump，不是只有 prompt 文件**。

53. **planner 请求结构五处偏离（v6.13.5 修复，v6.13.2 两处误对齐的纠正）**：回灌修完后格式仍有差距，逐项 diff 部署请求 dump 发现——① 消息前缀：部署版是 `planner_messages.build_planner_prefix` 的 `<message msg_id="…" [quote="…"] time="…" user="…" [group_card="…"] [is_self_message="true"]>\n内容`（无闭合标签），v6.13.2 抄的 `HH:MM:SS[msg_id:x][说话人]内容` 是 `format_speaker_content` 的**可见文本**格式，planner 请求根本不用（同一文件里两种格式，用途不同——抄之前必须确认函数挂在哪条调用链上）；② 自发消息进 **user 轮**带 `is_self_message="true"`（v6.13.2 误放 assistant 轮）；③ 尾部结构：部署版每条注入是**独立 user 轮**（`<system-reminder>` deferred 提醒 → `时间：YYYY-MM-DD HH:MM:SS` 每请求一条 → `当前聊天额外注意事项`（chat_prompts 命中，尾部消息而非系统提示词）→ 末尾提醒），maisoul 曾全部 `\n\n` 拼进一个 user turn 且缺时间消息；实现 = 尾部轮临时 append 进 contexts、请求完 `del`（text_chat 的 prompt 参数固定给末尾提醒原文）；④ fetch_history 是 focus 专属（见坑 30）；⑤ 自发消息回写只存首段 80 字 → planner 上下文里自发消息被截断，改存全文（quote 属性一并接通：入站消息提 Reply 组件、自发回写带发送侧引用目标）。**通用教训：功能名相同 ≠ 格式相同，"对齐 MaiBot"的验收物是逐消息 diff 请求 dump（含属性顺序与转义），不是函数名对上就算数**。

54. **planner 思考文本不得回灌；「思考/正文」是双通道，回灌只认正文（v6.13.6 修复，用户实报"格式差距更大了变成英文"）**：MaiBot 每轮输出 = ReasoningItem（思考）+ AssistantMessageItem（可见正文）+ 工具调用；**思考英文起步、正文中文是常态**（owner 观察一致），两通道文本独立。会话历史里思考块重发恒空、只有可见正文持久化为 few-shot——部署 dump 复核（终版）：冷启动前 2 轮只有英文思考（无正文，什么都不回灌），第 3 轮起每轮产出中文结构化正文（「当前状态/分析/下一步」正是 AssistantMessageItem 的 parts[].text），回灌后格式锁定。maisoul 坑 51 的 thinking 兜底把思考文本也记进回灌 → 英文思考成为强吸引子 → 语言/格式双漂移且自锁。修法：`_planner_cycle` 区分 `visible_analysis`（`_resp_text`，回灌唯一来源——analysis_log 与 in-cycle contexts 都只记它）与展示并集（观察页/防复读/latest_reason 仍取正文∪思考；MaiBot 正文空时卡片无文本，maisoul 显示思考兜底是坑 51 的有意展示差异）。**模型口径（v6.13.8 终版）：v4f 工具轮纯思考不出正文、收尾轮（无工具轮）稳定产出中文结构化正文**——正文来源是 reply/wait 之后的收尾反思轮；maisoul 曾在 reply 后立即结束循环（收尾轮不存在 → 正文永远无来源，才显得"v4f 不出正文"），v6.13.8 对齐 reply 续轮后 v4f 实测连续多轮稳定产出「分析：/当前判断：/下一步」式中文正文并跨轮回灌成功，无需换模型。**教训：①回灌里"什么进历史"和"什么给用户看"是两套口径，混用会把展示端的兜底变成上下文的毒药；②"模型不出正文"的结论，先查自己有没有给它出正文的轮次**。

55. **planner 工具轮必须走协议结构 + reply 后循环继续；dump 分析的字段陷阱（v6.13.7/8）**：①工具轮表示：MaiBot 的循环内历史是「assistant（思考空投影 + 正文 + tool_use 块）→ tool_result 块」的标准协议轮；maisoul 曾用纯文本 user 轮回执（"[send_emoji 结果]…"），思考轮干脆没有 assistant 轮 → user→user 连续、模型看不见自己调过工具。修法：assistant 轮始终存在（带 `tool_calls`，正文块仅可见正文非空时），工具结果进 `{"role":"tool","tool_call_id":…}` 轮（AstrBot anthropic 源转 tool_result 块并合并连续 tool 轮；OpenAI 源同构）；续轮判定 = contexts 尾部是否 tool 轮；wait 回执跨轮重建后无配对 tool_use，维持 user 文本轮（孤儿 tool_result 会被协议校验拒收）。②**reply 后必须续轮（v6.13.8，owner 实测"我不喜欢你"提示定位）**：MaiBot 的 reply 正常执行后 `should_pause=False` 继续循环（reasoning_engine 只在未生成可见消息时告警继续），模型下一轮做收尾分析（分析自己刚回完的状态，通常无工具结束）——**v4f 的可见中文正文就产自这类收尾轮**，写入历史成为格式锚点；maisoul 曾 reply 后立即 finalize return，收尾轮不存在 → 正文永远无来源。修法：reply 结果进 tool 轮后 `continue`，由无工具分支自然收尾。③**dump 字段陷阱（坑 54 两次误判的根源）**：MaiBot 的 ReasoningItem 文本在 `text_parts`，AssistantMessageItem 文本在 `parts[].text`——两套字段名，读错一个得到静默空串，曾据此误判"部署版正文恒空"。分析 dump 必须全量打印原始 parts 结构，禁止想当然的字段名。

## 9. 打包与发布

- **打生产 zip**：python zipfile 打包，排除 `__pycache__`、`data_learning.json`（学习库）、`data_monitor.db*`（观察账本，含 SQLite -wal/-shm 侧车）、`data_monitor.json.imported`、`*.log`、`AGENTS.local.md`、`.git/`——最稳妥的取文件方式是 `git ls-files`（天然只含干净源文件）；生产部署各自生成这两份；`data_char_frequency.json`（错字引擎依赖）必须包含。插件市场对发布包有 **16MB 上限**。
- **纯净交付红线（每次提交与发版自查）**：被提交/打包的文件里不得出现任何本机部署细节——容器名、内网 IP/端口、容器内绝对路径、webchat 会话 ID、真实人设与 QQ 号；文档示例一律用 `<astrbot容器>`、`<端口>`、`<容器内插件路径>`、`<AstrBot数据目录>` 占位符，本机实际命令只记在 AGENTS.local.md「本机环境备注」。**新装纯净验收**：交付包内容 = `git ls-files` 全集（无运行时数据、无本地信息、中性默认值），安装后插件目录只新增该部署自己生成的运行时文件（学习库/观察账本等）。**本地实验目录、临时脚本、比对用私有产物不经 owner 确认不进共享历史**（对齐 MaiBot「实验目录不入共享历史」——MaiBot 源码 dump/部署前端 chunk 只留在本机，路径记 AGENTS.local.md）。
- **分发默认值不含任何个人部署内容**：人格三件套默认 = MaiBot 官方模板原文 + 中性示例人设；实机环境细节（容器名/端口/网关）不进仓库。
- **提交 PR 流程**（本仓库）：开分支 → 提交 → `gh pr create` → 页面审阅合并；Commit/PR 格式与版本联动规则见 §5 条目 9（Conventional Commits）。
- **本地私有红线与环境备注**：写在 `AGENTS.local.md`（已列 .gitignore，永不入库）——含禁止提交的真实环境数据/隐私清单、同步前自查命令与发版自查清单。
