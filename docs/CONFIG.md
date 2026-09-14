# 配置字段全表（_conf_schema.json）

> 开发准则入口见 [AGENTS.md](../AGENTS.md)。加配置项的流程与禁令在 AGENTS.md §5。

**命名规则**：AstrBot schema 是扁平键，MaiBot 是嵌套配置。除 collision 需加前缀外，键名/默认值/描述全部取 MaiBot 叶子名（官方 zh_CN 标签+docstring）。

## 1. MaiBot 映射表

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
| emotion_enable / emotion_feedback_enable | —（情绪子系统 maisoul 扩展） | 详见 schema |
| task_models | —（任务级模型绑定 maisoul 扩展） | 六任务默认条目 `[{task, models:[{provider,model}], strategy:"sequential"}]`（provider 填源名或精确 id，strategy∈sequential/random/balance；三处同源清单有护栏测试锁定） |

chat_prompts 条目结构 = MaiBot ExtraPromptItem：`{platform, item_id, rule_type:"group"|"private", prompt}`，platform+目标 ID **精确匹配**（无后缀/通配），多条命中换行拼接；私聊时 item_id=用户 ID。
talk_value_rules 条目：`{platform, item_id, rule_type:"group"|"private", time:"HH:MM-HH:MM或*", value}`。
学习配置条目同 LearningItem：`{platform, item_id, type:"group"|"private", use, learn}`。

## 2. maisoul 扩展字段

| 字段 | 类型/默认 | 作用 |
|---|---|---|
| mode | str `planner` | planner（默认，完全对标 maisaka 决策层，见 MAIBOT_FIDELITY.md）；independent=麦麦流水线接管；native=触发后交原生 agent |
| bot_name | str `麦麦` | 机器人昵称：麦麦显示和自称时使用的名字（identity 首行、自发回写署名）；同时是主配置人格的人格名 |
| aliases | list `[]` | 别名：别人可能用来称呼麦麦的名字，身份行与提及检测共用这一份（对应 MaiBot alias_names，单一列表） |
| personality | text | 人格设定（[personality].personality） |
| behavior_style | text | 行为风格：Planner 使用的行动准则（只进 planner 系统提示词） |
| reply_style | text | 表达风格：麦麦平时说话的风格（[personality].reply_style） |
| multiple_reply_style / multiple_probability | list / float `0` | 备用表达风格彩票池 / 彩票概率（0~1 小数，v6.28.0 对齐 MaiBot 量纲；旧百分比自动迁移，15 → 0.15） |
| group_chat_prompt | text | 群聊通用提示词 |
| chat_prompts | list `[]` | 额外 Prompt，精确匹配多条拼接 |
| chat_tools | list `["send_meme"]` | 暴露给聊天 LLM 的工具（MaiBot 等价物），弹窗从 AstrBot 注册表选取 |
| chat_skills | list `[]` | 暴露给聊天 LLM 的 AstrBot 技能，经原生 build_skills_prompt 注入 |
| maid_bridge | bool `true` | 是否把 call_maid 一并暴露（独立模式与 planner 模式的 replyer 均生效） |
| personas | list `[]` | 多人格库 `[{name, bot_name?, personality, behavior_style?, reply_style?, aliases?, group_chat_prompt?}]`——字段名与描述与 MaiBot official_configs 官方文案一致 |
| default_persona | str `` | 默认人格名；空=主配置三件套 |
| group_persona | list `[]` | 静态群绑定 `[{chat:"群号或*", name}]` |
| follow_persona_switch | bool `true` | 兼容 astrbot_plugin_persona_switch：/persona 切换后同名 maisoul 人格最高优先生效 |
| preset_dialogues | list `[]` | 预设对话 `[{user, reply}]`，注入系统提示词【预设对话】块作风格参考（人格可覆盖，PERSONA_FIELDS 已含） |
| enable | bool `true` | 总开关 |
| escape_at_wake | bool `false` | 逃生舱：false（默认）=聊天全面接管，群聊 @/唤醒前缀作为 at 档显式点名进入麦麦门控；true=恢复旧行为放行原生 agent。/指令与 heartflow 恒放行不受影响 |
| eco_injection | bool `true` | 生态注入桥：independent/planner 生成前手动触发 on_llm_request 钩子链收集注入（心弦好感/记忆/世界书）拼进 system_prompt，发言后触发 on_llm_response（记忆沉淀等回写）；native 模式原生已生效。关闭后提示词完全对标 MaiBot |

## 3. 已删除的自造键与迁移

旧版自造键（threshold、frequency、cooldown、context_size、seg_min_delay、seg_max_delay、enable_typo、typo_rate）已从 schema 删除，运行配置里的残留值无害（无消费点）；旧 nicknames 启动时自动合并进 aliases。v6.28.0：multiple_probability 百分比量纲自动迁移为 0~1 小数（>1 除以 100，幂等）。
schema 默认值必须保持中性示例（真实人设只存在运行配置，永不入库）。

## 4. 常用调法

- **话痨程度**：talk_value（全局）或 talk_value_rules（按群/时段）；单群用 chat_prompts 给语气要求。
- **私聊节奏独立调**：private_talk_value / private_chat_prompts / max_private_context_size。
- **切换发言模式**：聊天里 `/maisoul planner|independent|native`。
