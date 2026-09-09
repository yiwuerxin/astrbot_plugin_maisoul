# REFACTOR_NOTES — v6.20.3（全量代码审查修复批次）

对 v6.20.2 全量审查（审查报告见仓库外 maisoul_code_review_v6.20.2.md）发现的缺陷集中修复。
版本顺延 v6.20.3，**零数据迁移、零配置迁移**。升级：覆盖代码文件后重启即可（含 pipeline/ 改动，
reload 不级联，坑 59）。

## 修复清单

- **embedding 任务绑定不再跨重载丢失（高）**：`core/modelbind.TASKS` 补第六任务
  "embedding"——schema 默认值与模型管理页本就提供该任务槽，但 normalize 白名单漏列，
  每次加载/重载把用户的嵌入绑定从内存剥掉、静默回落第一个嵌入实例（多嵌入部署下
  重载即失效；单嵌入时行为恰好等价故不可见）。教训入坑 62：normalize 白名单必须
  与 schema 默认值同源。
- **私聊学习规则前后端贯通（高）**：后端 `_schedule_learning`/`learn_from_chat`
  此前调 `learning_flags` 漏传 `is_group`（恒按 group 匹配，私聊 learn=False 规则
  失效照样发起学习请求）；前端 collect() 硬编码 `rule_type:'group'`/`type:'group'`
  （手工配置的 private 规则一旦保存即被改写）。现 learn 侧 is_group 全链贯通，
  页面行模板携带 data-rt/data-lt 原值、collect 按行保留（新行默认 group）。
- **WebUI 保存学习库不再清零黑话 count（高）**：collectLibEdits 对 jargons 硬编码
  `count:1`——点一次保存把全部黑话学习次数重置（抽样加权与排序依据全丢）。现读
  行内 .lj-cnt 存量值。
- **planner 折叠边界不拆散 tool 配对（中）**：fold_old_turns 按条数切边界，落在
  tool 回执上时保留区开头出现孤儿 tool 轮（配对 assistant 已折进摘要）——OpenAI
  类 Provider 协议校验拒收整轮请求。现终点回退到配对 assistant 之前整对保留。
- **过滤词命中补 stop_event（中）**：命中 ban_words/ban_msgs_regex 只 return 不拦
  传播，@机器人的违禁消息被原生 LLM 照常回复——"整条丢弃"语义名存实亡。现补
  stop_event（escape 分支保持不拦截）。
- **「怎么看」征询随 bot_name 动态构造（中）**：scoring.opinion_reason 正则硬编码
  "麦麦"，改名后"XX怎么看"不加分。现由 bot_names 动态构造（默认名下与 MaiBot
  原文字面等价，别名入式与提及档收口同口径）。
- **wait 续轮挂 running_task（中）**：续轮内联 await 不更新 pl.running_task，
  planner_interrupt 的 cancel 打在已完成的旧任务上（no-op）——打断机制对 wait
  续轮静默失效。现续轮挂 asyncio.current_task()。
- **WebUI 五处（中/低）**：发言模式按钮高亮反转修正（按 planner/independent/native
  索引映射）；moIngest 的 event_id 去重提前（重复 session.start 不再重置会话 count）；
  moCollapsible/rrSel 的 key 进 onclick JS 字符串统一走 jsq（esc 不转义单引号）；
  离开麦麦观察页拆数据通道（moTeardown：停轮询/退 SSE，此前打到页面卸载）；
  pe_nick UI 临时键不再进保存载荷。
- **杂项（低）**：modelbind_host `_task_model_rr` 重复定义删除；summarizer 死绑定
  删除（恒走默认 Provider，行为不变）；`expression_habits_block` 收敛复用
  `_sample_legacy_pool`（生产路径死代码去重）；_evict_idle 不再为淘汰检查实例化
  PlannerState；发送队列降级基线改时间戳口径（deque 滚动不漂移）；上下文上限
  0 → 空转写（旧 `[-0:]` 误取全量）；ecobridge 两个钩子循环 BaseException→Exception
  （CancelledError 放行，卸载不被拖到超时）；monitor 会话集封顶 4096；ruff F401/
  F841/F541 清零；麦麦设置子页切换前 collect（未保存编辑不丢）。

## 验证

- 新增 35 项断言（normalize 保留 embedding / 私聊 learn 短路 / 折叠无孤儿 tool 轮 /
  时间戳基线抗滚动 / 上下文 0 空转写 / 怎么看动态名），全部先在旧代码上跑红、
  修复后转绿；自跑模式 447 项检查零失败（v6.20.2 基线 412 项），pytest 24 组全绿。
- black 26.5.1 全仓 `--check` 通过；页面 JS 经 node --check 语法校验。

# REFACTOR_NOTES — v6.18.3（艾特/提及误判修复批次）

群聊实际使用反馈的触发误判修复。版本顺延 v6.18.3，聚焦「被点名」判定的精确性，
**零数据迁移**。升级：覆盖代码文件后重启（或重载插件）即可。

## 修复清单

- **@他人不再被误判为提及**（新增 core/mention.py 单一真相模块）：aiocqhttp 会把
  @其他用户渲染为 `@昵称(QQ号)` 纯文本拼进 message_str（@自己首个不渲染），而门控
  提及判定是裸子串——bot 叫「麦麦」时群里 @另一个 bot「小麦麦」会被"麦麦"子串
  误命中，mentioned_bot_reply 开启时直接强制必回。现在判定前先剥渲染 @token、
  排除消息内 At 段的他人昵称，再对 bot_name/aliases 做前边界匹配（「小麦麦」
  「个麦麦」不命中，「麦麦你觉得呢」命中——中文无分词只做前边界，后缀扩展名的
  纯文本提及仍命中属已知残留，@场景由 At 段昵称排除精确兜底）。
- **@全体成员/引用回复不再升级 at 档**（gating.py + mention.effective_at_bot）：
  AstrBot waking_check 对 @bot、@全体（AtAll）、引用回复、唤醒前缀四种形态统一置
  is_at_or_wake_command=True，v6.10 起 explicit 整体并入 at_bot——@全体与引用回复
  被升级成强制必回、绕过 mentioned_bot_reply 默认关（注释承诺「引用算 mention
  不算 at」名存实亡）。现 AtAll/Reply 命中时对 explicit 降级，回落各自既有档位
  （引用走 reply_bot→提及路径），真 At 段与唤醒前缀不受影响。
- **评分提及档补 bot_name**（core/scoring.py）：档位此前只查 aliases——
  reply_necessity 模式下叫别名「小麦」能拿 80 档触发、叫主名「麦麦」反而是 0 档
  （实测 35 分 vs 115 分，与门控 mentioned 口径分裂）。现 bot_name 与 aliases
  同权，统一走 mention.is_mentioned（opinion_reason 同口径收口）。
- **配套**：admin.py sim 调试口径同步；events_util 新增 _has_at_all/
  _other_at_names（except 带留痕，AtAll 导入防御式兼容未导出的旧框架）；
  版本七处同步 v6.18.3。

## 验证

- 新增 test_mention（34 项断言）与主名入档用例，全部先在旧代码上跑红、修复后
  转绿；自跑模式 412 项检查零失败（v6.18.2 基线 381 项）。
- 误判场景确定性复现：@他人「小麦麦」开头/中段不再命中提及；@全体/引用回复
  降级回落；真 @bot、唤醒前缀、纯文本叫名行为不变。
- black 26.5.1 全仓 `--check` 通过。

# REFACTOR_NOTES — v6.18.2（健壮性修复批次）

外部评审复核后的小修批次。上游 PR#15 已占用 v6.18.1，本批次版本顺延 v6.18.2，全部为局部修复，**零行为面变更、零数据迁移**。
升级：覆盖代码文件后重启（或重载插件）即可。

## 修复清单

- **字频缓存自愈与原子落盘**（core/typo.py）：损坏的 data_char_frequency.json
  此前会让错字引擎整体抛错不可用；现在读取失败先备份 `.corrupt` 再按 jieba
  词典重建（对齐 learning 库的损坏处理惯例），落盘改 tmp+replace 原子写。
  PR review 补充形状校验：合法 JSON 但 null/列表/非数值 json.load 不抛异常，
  会在查频时才炸——形状不对同样备份重建
- **events_util 四处裸 `except: pass` 补 debug 日志**（识图引用/引用 ID/@bot
  判定/回复判定）：GOAL 验收要求 except 必带留痕——这些 helper 在反注入清洗
  路径上，此前降级完全无痕。行为不变（仍按空/False 降级），仅加 exc_info 日志。
- **观察 writer 停机丢批次**（core/monitor.py）：writer 正在 flush（to_thread
  落库中）时后续事件入队并停机，哨兵会在下一轮批量排水中被取出——旧实现
  直接 return 把已取整批丢弃（与同行注释承诺相反）；生产对应「忙碌群消息
  持续入队时卸载插件」场景。现在排水中撞哨兵先冲刷已取批次再退出。
- **writer 经 TaskRegistry 发起**：start_writer 改为必传 registry（main.py 与
  测试同步更新），消灭最后一处裸 `asyncio.create_task`——使
  「grep create_task 仅 TaskRegistry 本体」的验收声明重新成立。
- **LLM 失败上报归因**（pipeline/modelbind_host.py + planner_host.py）：
  `_task_text_chat` 改为每次尝试前写 `used`（成功路径重写同值，语义不变），
  planner 的 llm.error 据此上报**实际尝试的模型**（任务绑定/降级链场景），
  不再恒记默认 provider 标签。

## 验证

- pytest 与自执行双入口全绿（22 用例 / 381 项检查，本次新增 5 项回归，全部
  先在旧代码上验证失败后转绿；writer 丢批次用例用线程屏障钉死时序）。
- black 26.5.1 全仓 `--check` 通过；`grep create_task` 审计干净。

# REFACTOR_NOTES — v6.16.0（GOAL 双插件加固重构）

本次按外部审查报告与 GOAL 任务书完成 P0 修复、结构重构与 MaiBot 机制移植。
**所有新机制默认关闭或保持既有默认**，升级后行为不变；逐项开启见配置说明。

## 升级注意事项

- **数据零迁移**：无数据格式变更（学习库/观察账本/配置结构不变），直接覆盖代码文件后重载插件即可。
- schema（_conf_schema.json）新增键全部有默认值，旧配置无需手改；WebUI 保存后会补全这些键。
- `/maisoul sim <文本>` 冒烟：完整管线（门控→planner→replyer）不依赖群聊适配器，建议升级后跑一次。

## 修复（Phase 1，编号对应 GOAL）

- **M1** 表达自检 suitable 判定恒 False（字符串包含的空间错位）→ 结构化提取；默认 `expression_checked_only=true` 下学习功能此前隐性失效。
- **M2** 会话键收敛 `session_key()` 单一真相（上游已修语义，本次消除 6 处内联重复）。
- **M3** planner 打断竞态：被取消旧循环的退出路径会把新循环的 agent_state 清成 idle → 双循环并发；引入代际号守卫。
- **M4**（上游已修，本次核实）空窗重查已设 firing/已取消旧任务。
- **M5**（上游已修，本次核实）学习库原子写+损坏备份、WebUI 结构校验、读库结构校验。
- **M6** TaskRegistry（core/taskregistry.py）：全部 create_task 收口（强引用/具名/完成自清），terminate 先 cancel_and_wait_all 再关监控库连接池。

## 重构（Phase 2，行为零变化）

- **M7** main.py 1560 行 → ~160 行薄壳；管线逻辑机械搬移到 `pipeline/`（gating/replyer/planner_host/ecobridge/modelbind_host/native/admin/events_util）。core/webui/schema 零改动。
- **M8** 双生成路径（independent 与 planner reply）公共段收敛：`_select_expr_block` + `_deliver_reply`（发送闭包/记账/生态回写/学习调度单一实现）。
- **M9** PlannerDeps.plugin 反向耦合 → 显式 `PlannerHost` Protocol + 适配器（并修复 M7 拆分后的回调断链）。
- **M10** 监控写入移出事件循环：writer 协程批量落库（to_thread），stop_writer 优雅冲刷。
- **M11** 错字引擎装载期后台线程预热（首次构建原本卡首条回复秒级）。
- **M12** 有界化：会话表 512 LRU（活跃保护）、SSE 队列 500 丢旧保新、writer 队列 2000、discovered_tools 256。
- **M13** 技能块 60s 缓存（`m not in pending_now` O(n·m) 上游已修）。

## 机制移植（Phase 3，默认保守值）

- **P-B 情绪 VA**（`emotion_enable` 默认关）：9 情绪词→(心情值,激昂度)，动量/每分钟衰减/12 锚点标签；情绪行入提示词、[情绪:xx] 标签发送前剥离、打字节奏 ×1.5^激昂度。锚点数值因 MaiBot 研究文档缺失为本项目口径。
- **P-A 中期记忆**（v6.22.0 已整体拆除）：曾实现"窗口外旧消息后台摘要 + Jaccard 词集召回注入"，与 `fetch_chat_history`（同一 200 条 buffer、拉模式取原文、按需零成本）覆盖完全重叠且同为重启即失，召回还是无 embedding 的词面近似——拉模式更精准，拆除回归 §7 取舍清单"mid_term_memory 不复刻"的既有决策。
- **P-D 发送队列降级**（`send_queue_demotion` 默认关）：生成期新消息 >3 条或 >200 字 → 回复改引用最新一条。
- **P-E 频率窗口反馈**（`freq_feedback_enable` 默认关）：10 分钟窗自发条数→乘数[0.2,5.0]（近 5 分钟超速只降不升）。
- **P-F 反注入**（`anti_injection` 默认开）：引用前缀/转发占位清洗进门控；@其他AI 开头不计点名自己；系统提示词附两句声明。
- **P-G 错字纠错消息**：上游已实现（typo_enable_correction_quote），未重做。
- **P-H 心弦联动**（`xinxian_link` 默认关）：好心弦 facade（get_profile）把好感等级/印象/关系渲染进系统提示词，缺插件静默降级。

## 清理（Phase 4）

- 删除死代码：states.planner_last_cycle_ts、monitor.emit_stage_removed、bridge.MAIBOT_TOOL_EQUIVALENTS、constants.TYPING_*（postprocess 硬编码同值）；modelbind.normalize_task_models 由死代码改为**接线**（initialize 内存规范化）；personas.list_persona_names 保留（测试断言用，已注明）。
- 文案：/maisoul planner 切换提示此前误述为"独立模式"；enable_reply_quote 描述过期；版本号 docstring/register/metadata 三处同步。

## 验证

- `python3 tests/test_core.py`（容器内）：333 项全绿（本次新增 40+ 项，全部先失败后通过）。
- pyflakes 全量干净；`grep create_task` 仅 TaskRegistry 本体；`grep time.sleep` 仅 sender 的打字延迟（asyncio.sleep）。
