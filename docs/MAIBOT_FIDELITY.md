# MaiBot 复刻保真对照与取舍清单

> 开发准则入口见 [AGENTS.md](../AGENTS.md)。本文是"对标 MaiBot"的规格书：保真总则、已复刻范围、取舍清单、Planner/观察/推理页/WebUI 的对标规格、已知非对标项。
> 参照物：MaiBot 官方仓库 <https://github.com/Mai-with-u/MaiBot> 及其部署实例（构建产物，非源码 dump）。

## 1. 保真总则（需求铁律）

1. 任何功能需求，**默认含义是"完全对标 MaiBot，功能完全一样"**——行为、配置字段、提示词结构、参数默认值都按 MaiBot 源码来，不许自作主张做"近似/简化版"。
2. **实现写法也要照抄 MaiBot**（MaiBot 用 SQL 表就用 SQL 表，不许以"插件侧更轻"为由换 JSON 等变体）。
3. **WebUI 仿照对象 = MaiBot 部署实例的构建产物**（pip 包 `maibot_dashboard` 的 dist），不是容器里的前端源码 dump——样式、颜色、字号、圆角、图标（lucide SVG，不用 emoji）、文案必须**一模一样**：部署版有的一个不能少，部署版没有的（如暂停按钮、自造徽章）**不许自己加**。比对方法 = 抓部署 chunk 里的中文字符串与 CSS 变量/组件类。
4. 只有两种情况可以偏离：① MaiBot 没有该功能（按 AstrBot 生态最优自写）；② 相对 MaiBot 的写法确实有更好的替代——须有可陈述的机制依据，并**在本文"非对标项清单"标注差异与保留理由**。
5. 机制层面不要求逐项死板对齐——确有更好效果增益的偏离/自创机制可保留，但同样必须标注差异与机制依据（例：频率窗口反馈与存在感惩罚互补——绝对条数速率限制 vs 5min 占比惩罚）。**功能重复且无增益的实现不在此列**：同职能已有等价或更优路径时应拆。
6. 拿不准就先查 MaiBot 源码再动手，不要凭记忆或直觉实现。凡标"对齐 MaiBot xxx"的常量/公式，改动前先对照官方仓库对应文件。
7. **"对齐"的验收物是逐消息 diff 请求 dump（含属性顺序与转义），不是函数名对上**；功能名相同 ≠ 格式相同（坑 53）。MaiBot 侧权威取材：`logs/maisaka_prompt/planner/*.json` 请求 dump，不只 prompt 文件。

## 2. 已复刻范围

**核心管线**：三件套 prompt 结构（maisaka_replyer）与输出指令原文；触发门控全套（frequency 的 ceil(1/f)+空窗补偿+强制触发；reply_necessity 档位制评分——噪声清洗/问句正则/叫别的AI抑制/短反应惩罚/压力分/存在感惩罚/频率倍率）；talk_value 动态规则（目标×时间优先级）；回复后处理管线（括号心声清除/呃呃/超长默认回复/分句规则+概率合并/条数上限/合并/颜文字保护）；拼音字频错字引擎（同音字/声调/整词替换+纠正消息）；打字延迟（×typing_speed）；同目标防重复；额外 Prompt 精确匹配多条拼接。

**聊天学习子系统**（core/learning.py）：表达学习（learn_style.prompt 原文 + expression_evaluation 四条基准自查 + legacy 随手抽样注入【表达习惯参考】，库满 10 条启用；LLM 二次选择路径未接，走"直接注入"路径）；黑话学习（learn_jargon.prompt 原文 + jargon_inference_with_context 含义推断，上下文命中注入【黑话参考】）；关键词反应（keyword_rules/regex_rules + [命名捕获组] 替换）；优化上下文（自己的旧发言只保留最近 3 条）。学习库按共享组分库，WebUI「学习」页可视化管理。

**Planner 决策层**（mode=planner）：见 §4。

## 3. 未复刻 / 依赖 AstrBot 生态替代（有意取舍）

| MaiBot 能力 | 处理 | 理由 |
|---|---|---|
| A_memorix 长期记忆（五模式 query_memory） | 未复刻；livingmemory 经生态注入桥全模式生效（含记忆沉淀回写） | astrbot_plugin_livingmemory 更成熟（完整生命周期/被动捕获/WebUI 管理），MaiBot 记忆对 maisoul 独立模式不可见 |
| emoji_system + 偷表情 | 未复刻 | astrbot_plugin_stealer 本身就是它的移植+增强，经 chat_tools 暴露 send_meme 达成同等体验 |
| 行为学习/高频词学习 | 未复刻 | 收益/成本比低（行为模式需独立聚类存储与维护任务） |
| 聊天回想（mid_term_memory） | 未复刻 | maisoul buffer（200 条重启即失）撑不起"持久会话历史+摘要+embedding 召回"等价机制；fetch_chat_history 拉原文 + livingmemory 已覆盖需求 |
| 关键词反应规则 | **已复刻**（learning.py） | keyword_rules/regex_rules + 命名捕获组替换 + 【关键词反应】注入 |
| 富回复（attach_pic/attach_at/引用回复） | 部分接通：引用回复已接（Reply 组件）；attach_pic/attach_at 未接 | 后续可接 MessageChain 组件 |
| 世界书/好感度 | 无对应 | AstrBot 的 worldbook/xinxian 更好，经生态注入桥全模式生效。另有情绪-关系耦合数值面（只交换数值不渲染提示词，`emotion_feedback_enable` 默认关，见 ARCHITECTURE.md §5），与提示词面注入无关 |

**加 MaiBot 工具等价物的流程**：装好对应 AstrBot 插件 → 在 `chat_tools` 默认值与页面说明加工具名 → `bridge.MAIBOT_TOOL_EQUIVALENTS` 补映射记录。

## 4. Planner 决策层规格（对标 maisaka）

系统提示词 = `maisaka_chat.prompt` 逐字原文（已用 MaiBot 容器原文 diff 验证一致），**裁去 tool_search/deferred tools/view_forward_message 三行**——工具集里没有这些工具时悬空引用会诱导模型调用"未知工具"；将来把 AstrBot 工具以 deferred+tool_search 接进 planner 时连工具一起恢复原文。**有意扩展（差异标注）：系统提示词补「@名字」释义一句**（maisaka 原文没有，配套 At 文本化，坑 63）。工具声明 = builtin_tool 原文。

- **agent 循环**：MAX_INTERNAL_ROUNDS=10，多轮工具调用，工具结果回填下一轮，contexts 跨轮累积。
- **工具集（可见 4 个）**：reply（msg_id/set_quote/reply_reference/reply_style 枚举——篇幅由 Planner 参数指定）、wait（连续上限 max_consecutive_wait_count=3，超限=对话休息；期间新消息不提前打断）、send_emoji（声明无参数=MaiBot 原样，执行桥接 send_meme 两步制）、tool_search。fetch_history 不暴露（MaiBot focus 模式专属，部署版未开）。
- **分工铁律：planner 唯一干活者、replyer 纯嘴**。管家与生态工具全部进 deferred 池（tool_search 发现后下一轮可用；打分表 1000/300/200/100/25/10、返回文案、`<system-reminder>` 模板均为 MaiBot 原文；提醒只进当次请求不进 contexts 历史；discovered_tools 会话级）。replyer 不带 func_tool（independent/native 模式例外，管家桥留 replyer 侧）。
- **WAIT/RUNNING 状态机**：群聊 wait 不唤醒，@/提及必回主动触发恢复；思考打断 planner_interrupt_max_consecutive_count=0 默不打断；空闲指数退避（base15×2^n 封顶 300、起点 2、积压 6 绕过、reply 重置）；wait 到期有积压自动续轮并注入完成回执（build_wait_completed_message 原文；无积压不续轮会让异步工具后的循环静默死亡，坑 26）。
- **请求结构**（坑 53 口径，验收=diff 部署请求 dump）：
  - 消息渲染 = 部署版 `planner_messages.build_planner_prefix` 原文：`<message msg_id="…" [quote="…"] time="…" user="…" [group_card="…"] [is_self_message="true"]>\n内容`（无闭合标签）。
  - 全部聊天消息（含自发消息，带 is_self_message）进 **user 轮**；只有 planner 分析进 assistant 轮。
  - 尾部结构：每条注入是**独立 user 轮**（`<system-reminder>` deferred 提醒 → `时间：YYYY-MM-DD HH:MM:SS` 每请求一条 → `当前聊天额外注意事项`（chat_prompts 命中，尾部消息而非系统提示词）→ 末尾提醒原文 chat_loop_service.PLANNER_FINAL_USER_REMINDER_TEMPLATE）。实现=尾部轮临时 append、请求完 `del`。
  - 首轮以 contexts 注入最近历史（群 40/私聊 60 条，对齐 chat_history 传参）；上下文 2× 稳定窗（`max(base, base×2)`），相邻消息跨日插 `时间：YYYY-MM-DD HH:MM:SS` 行。
- **历史分析跨轮回灌**（坑 52）：contexts 初始段 = 聊天记录 + 历史 planner 分析按时间戳交错（分析进 assistant 轮；稳定窗在合并流上截取）；每轮分析记入 analysis_log（deque 200）供下一轮回灌——这是部署版 planner 分析呈「当前状态/分析/下一步」结构的来源（提示词并无此格式要求，模型自发+看历史分析自我强化）。**回灌只认可见正文**（坑 54）：`visible_analysis`（`_resp_text`）是 analysis_log 与 in-cycle contexts 的唯一来源；思考文本只用于展示（正文∪思考）——把思考回灌会让英文思考成为强吸引子导致语言/格式双漂移。
- **reply 后必须续轮**（坑 55）：reply 正常执行后继续循环，模型下一轮做收尾分析（无工具自然收尾）——可见中文正文产自收尾轮，写入历史成为格式锚点；reply 后立即结束会让正文永远无来源。
- **工具轮协议结构**（坑 55）：assistant 轮始终存在（带 tool_calls，正文块仅可见正文非空时），工具结果进 `{"role":"tool","tool_call_id":…}` 轮；wait 回执跨轮重建后无配对 tool_use，维持 user 文本轮（孤儿 tool_result 会被协议校验拒收）。
- **本轮上下文折叠**：保留最近 3 组 user/assistant，更早一次性折叠为「[已折叠的历史工具调用]」摘要——列表内联折叠必须单趟（while 逐对重折会死循环，坑 34）。
- **防复读**：本轮思考与上轮 difflib 相似度 >0.9 → 替换固定反思文本（PLANNER_REFLECT_ON_REPEAT）。
- **黑话参考注 planner 每轮（exclude/matched_out 轮间去重）、表达习惯注 replyer**——位置不可颠倒（坑 33）。
- **token 用量**：LLMResponse.usage（input_other+input_cached=输入、output=输出）逐轮累计进 planner.finalized。
- **模型名上报**：`_task_text_chat` 经 `used` 参数上报成功调用实际服务的 `{"model", "provider"}`（按次覆盖、空回落 provider.get_model；降级链以最终成功者为准）；planner.model_name=整循环去重拼接，request.messages[].model_name 逐轮标注（仅进监控副本不回灌）。
- **识图门控分岔**（坑 38）：Planner 决策轮看 `enable_image_context`（默认关）；replyer 生成轮读 AstrBot 模型条目 `modalities` 的「图像」勾选——`modelbind.provider_supports_image` 口径逐字对齐框架 `astr_main_agent._provider_supports_modality`（**空列表=迁移遗留未配置=不限制=支持；缺失/非 list=不支持**），判定入口 `modelbind_host.replyer_image_capable`（绑定链**全部**候选都勾才算支持；无绑定=当前默认 Provider）。模型能力单一事实来源是 AstrBot 的勾选，不读 sanitize_context_by_modalities。
- **任务级模型绑定**：配置 `task_models`，provider 填**源名**，`_resolve_bound_model` 按 provider_source_id 归源、精确 id 未命中取该源任一启用条目承载；三策略在 core/modelbind.py；接入点五处：planner 轮、replyer×2、emoji 检索词、expression_use、learner。任务清单三处同源（坑 62）：schema task_models 默认值 / modelbind.TASKS（normalize 白名单）/ 模型管理页 MD_TASKS——改一处必查另两处。
- **行为差异对照**：behavior_style 只进 planner 系统提示词（replyer/native 的三件套不追加「行动准则参考」块——MaiBot replyer 模板本就没有 behavior_style）。
- **未移植**（无等价基础设施，未伪造）：focus 专注模式、注意力漂移、行为表现情景分析子代理、query_memory/view_forward_message/switch_chat 工具。

## 5. 麦麦观察（对齐 maisaka/monitor）

- **MaiBot 前端有两套（重要）**：仓库 `dashboard/src` 是前端 TSX 源码（较新），部署实例实际跑 pip 包 `maibot_dashboard` 的预编译 dist（另一时间点构建）。**以部署版（用户看到的页面）为准**，比对方法：抓 `assets/index-*.js` 里的中文字符串。
- **事件集 = MaiBot events.py 的 9 个 emit，一个不多一个少**：session.start / stage.status / stage.removed / llm.retry / llm.error / message.ingested / message.sent / message.updated / planner.finalized。**timing_gate.result / planner.response / replier.response / tool.execution 是部署前端的遗留渲染分支，MaiBot 后端不 emit——maisoul 也不准发**（"有真实信号就接上"是错的，多一张卡就是不对标；前端渲染分支保留=与部署前端一致）。llm.retry / message.updated 无信号同样不接。
- 接线：session.start（会话首次进管线）、stage.status（阶段名照抄 reasoning_engine：启动循环/消息整理/Planner/Planner 已打断/工具执行·X/Replyer/等待消息/错误）、message.ingested（_record）、message.sent（reply/emoji 每段）、planner.finalized（每轮决策聚合 request/planner/tools/final_state 嵌套结构原样；end_reason 用 maisoul 真实出口名 reply/wait/no_action/max_rounds/no_new_message/interrupted/error/no_provider）、llm.error（text_chat 异常）。
- planner 文本：MaiBot 取 response.content；AstrBot 同义字段 completion_text，thinking 块分析为空时回退 resp.reasoning_content（仅展示）。
- **存储写法照抄**：SQL 表 **maisaka_monitor_events**（SQLModel 表类逐字段对齐 database_model.py：列/四个索引/默认值），record/replay/cleanup 逐行对齐 event_store.py（含 flush 取 event_id 回写 payload_json、两条 DELETE 语句、每 200 条或 60s 检查清理）。MaiBot 挂 MySQL，maisoul 独立 SQLite data_monitor.db 承载同一张表（仅会话工厂差异）；**建表收窄到 maisaka 单表——禁用共享 metadata 全量 create_all**（会把核心空表建进生产库）。文件存 AstrBot 持久化目录 data/plugin_data/<插件名>/（坑 50）。
- 推送适配：MaiBot websocket broadcast → 插件页轮询 GET /monitor/replay?since=（2.5s；SSE 被扩展层缓冲，坑 17——轮询保底常开、SSE 真正收到帧才停轮询，看门狗在 await subscribeSSE 之前注册）。
- **观察页 UI = 部署版像素对齐**（仿照对象=部署 chunk，非 TSX 源码）：
  - 布局：根 `flex gap-16px lg:flex-row`；aside 整圈 border + `bg-background/45`、w-52/w-16 过渡 0.2s；头部 activity 图标+「聊天流」+连接绿点(h-2)+折叠 ghost 钮（chevron-left/right h-3.5），折叠态存 localStorage（键照抄 maisaka-monitor-sidebar-collapsed，默认折叠）；主列=阶段条+时间线 Card（bg-card rounded-lg(8px) border shadow，min-h-420px）。
  - 阶段条：有状态 `bg-background`、无状态 `bg-muted/30`，均 rounded-md(6px) border px-2 py-1 横向滚动；顺序=统计chip→回到底部/清空（ml-auto）→阶段徽章组→更新于(ml-auto)→detail；统计 chip h-6+activity h-3+「统计」10px，数值只在 title 悬停；阶段徽章 default+activity h-2.5、轮次 secondary、agentState running→default 其余 outline（px-1.5 text-10px）；agentLabel 照抄：空/stop→无，running→运行中，wait→等待中，其余原样（**idle 不映射**）。
  - 卡片渲染 switch 照抄部署版：ingested（蓝圆头像+首字+名+时间，无框）、sent（绿框卡 emerald-500/30+5% + bot 图标头像 + outline 10px「已发送」）、planner.response（无框行+emerald 圆 brain「规划器思考」+6 行折叠+工具 chip）、finalized（emerald/60 左边框 Card；timing_gate.action=no_action 时整个 finalized 不渲染；interrupted→amber 打断卡）、工具块（teal/60 左边框；工具行=mono 工具名+成败徽章+时长+参数 chip+「完整调用 JSON」折叠+「执行结果」框；finish/wait 伪工具单独渲染绿卡「本轮思考暂时结束 等待新的消息。」）、replier（purple/60 左边框 Card+成功/失败徽章+思考过程 details）。
  - **渲染 switch 的 default=null**：未列事件一律不渲染——llm.error 落库但无卡（禁止自造红卡）；token 徽章、模型名行、Provider 原生工具块均因无信号不渲染。
  - 折叠文本照抄 CollapsibleText：折叠态行用**空格**拼接、按钮 text-primary 12px hover 下划线+chevron h-3+「 展开全部 (N 行)」/「 收起」；贴底阈值 80px；事件行 pb-3 + fade-in 300ms。
  - 行为照抄：时间线不过滤会话（选中只切阶段条）；首个 session.start 自动选中；会话项 hover:bg-accent/50、选中 bg-accent 白字；事件计数徽章 secondary；相对时间+当前阶段第二行。
  - 工具卡「执行结果」摘要截断 **2000 字符**（对齐 reasoning_engine._build_tool_result_summary 的 max_length=2000；模型回填走全文不受影响）。
- 图标：页面内联 `MO_ICONS`+`moIcon(name,size)`（lucide path 数据，viewBox24 stroke2 round），**禁止 emoji**。提取方法：icons chunk 里 `const X=[[...]],name=a("icon",X);`，从 `a("name"` 锚点**向前**找 `=[` 数组（向后找会拿到下一个图标的数组）。

## 6. 推理过程页（部署版逐像素复刻，"没有一丝差距"）

点击 Planner 卡「推理过程」→ iframe（srcdoc 隔离文档）内渲染与部署版 /reasoning-process 同构页面：同类名 DOM（记录行=工具名+时间+耗时/Token/大小、item 卡=序号+角色徽章+类型 mono 标+内容 pre+「完整 Item JSON」折叠、头=页签+条数/页码+过滤/搜索、底=分页+Token 合计）+ **原版 CSS 整体内嵌**（dist index CSS，JetBrains Mono base64 内联，`<style id=mbrc media=none>` 运行时注入 iframe；该构建产物已获授权嵌入仓库，AGPL 同族、来源版本已记录）。

- 颜色映射（实抓）：system=cyan、他人消息=emerald、自发消息=orange、reasoning=indigo、工具调用=fuchsia、工具结果=violet、assistant=amber。
- 数据侧：planner.finalized 的 request 带 `system_prompt`/`messages[].tool_calls`/`messages[].tool_call_id`/`messages[].reasoning`（maisoul 扩展字段，MaiBot 从 dump 文件取）；旧事件无字段时对应卡片自然缺失。
- **master-detail 布局**（对齐部署版）：grid 左列记录列表（返回监控+条数/筛选/列表）右列选中详情；记录粒度=每轮动作一条（按 request.messages 的 assistant.tool_calls 分轮，轮=assistant 工具轮+紧随 tool 回执，标题=该轮工具名，无动作收尾轮标题为空）；宿主 reason 模式整页接管（fixed 铺满插件页视口、隐藏阶段条），返回监控后复位。
- **「类型」双流程**（仿部署版 stage 机制）：工具栏「类型」按钮进类型选择视图（主流程分组 72px 竖标签+阶段卡网格，中文名照抄 planner=规划器/replyer=回复器，无记录的阶段不渲染）；planner=master-detail；replyer=每轮带 replyer 块的 finalized 一条记录（标题=输出预览 output_preview，meta=模型/耗时/循环），详情=「请求 Items」+「输出 Items」（Items 徽章后带「模型：xxx」）+「生态注入」区（**仅回复器流程渲染**——注入内容本就来自 replyer，planner 决策轮零注入，坑 47）。
- **postMessage 跨源协议（铁律）**：dashboard 嵌插件页的 iframe 带 `sandbox="allow-scripts allow-forms allow-downloads"`，无 allow-same-origin → 插件页与其中 srcdoc 都是透明源，`parent.*` 直调与 contentDocument 直写全部抛 SecurityError。**一切 srcdoc/子 frame 交互按跨源设计，postMessage 是唯一可靠通道**：上行 `{__moReason:1,cmd:set/page/rec/sel/back}`，下行 `{__moReasonUpd:1,...}` 直写 srcdoc DOM + 重放过滤。宿主在 startMonitor 挂一次上行监听。测试 harness 必须复刻真实 sandbox 属性，用真实点击（命中测试）+ 计算样式断言。
- 分页=真分页（RR_PAGE=20，初始页跳到选中轮所在页）；类型过滤按 tools 数组归类；动作过滤只匹配记录工具名行（.rr-rec 挂 data-tools）、搜索匹配全文（含隐藏体），两者 AND。
- 前端工程教训：① srcdoc iframe 实宽约一千像素，Tailwind `lg:`(1024) 永不触发——任意值断点自降（如 48rem）并连带覆盖 `lg:*` 变体；② JS 模板字符串里写 CSS 转义选择器（`.lg\:`、`\[`）必须**双反斜杠**——单个会被字符串转义吃掉，选择器非法整条规则被浏览器静默丢弃（styleSheets 里查不到）；③ 分页条等元素位置以实抓 DOM 节点顺序为准，不以视觉截图推断（左右列高度联动的 hidden 前提要核 computed style）。
- srcdoc 显隐必须用 `.rr-hide{display:none!important}`（同特异性后声明的 display:flex 会盖掉普通 display:none）——**验证显隐要断言 getComputedStyle().display，查类名会漏层叠 bug**。
- **入口门控必须与目标视图的记录生成条件同源**（「推理过程」按钮以 rrRoundsOf(e).length 为条件——以 tools.length 为条件会出现"数据在、看不见"：零工具 no_action 轮有记录却没入口）。
- **渲染幂等**：同轮新事件不重建 iframe（MO.reasonBuilt 记住已渲染轮次），视图级切换（stage 切换）才整体重建。
- 挂在长生命周期状态上的"单轮素材"字段（replyer_trace/replyer_reasoning/eco_injection 等）**开轮清空最稳**——写点与清点必须配对，否则异常中断/后续轮会复读过期块（坑 64）；会话级计数器自增点要覆盖所有开轮入口（wait 续轮也要自增+补阶段上报）。
- **思考素材链路**：思考只在推理过程独立页呈现（时间线 planner.response/replier.response 卡已删并有回归用例锁死）；每轮 reasoning 记 reasoning_by_idx，仅监控序列化副本附到 request.messages[].reasoning，**回灌 contexts 永不带思考**（坑 54 不破）；replyer 思考走 planner.reasoning。

## 7. WebUI 全站对标（modern 主题）

整个插件 WebUI（不止观察页）= MaiBot 部署版 dist 的 modern 主题（dist CSS 基础层就是 modern；**禁止 future-retro 纸张令牌（--paper/--rust/--ink/--cream）与 Georgia 衬线**）。

- **壳**：左侧栏 208px（--layout-sidebar-width 13rem）整圈 border+右边线、Logo 区 h-20 border-b（"MAISOUL" 800 字距 .1em）、组标题 text-sm 600 uppercase muted/60、菜单项 h-10 rounded-lg px-3+图标 20px+标签 text-base 500，hover/选中=bg-accent 绿底（选中图标 text-primary）；顶栏 h-12 border-b sticky+折叠钮+页名+版本徽章+保存按钮；内容区 p-4 sm:p-6 space-y-4/6 max-w-1080 滚动。
- **导航分组照抄部署版侧栏文案**：概览(首页/麦麦观察) / 麦麦配置编辑(麦麦设置/发言节奏) / 麦麦资源管理(人格管理/学习) / 扩展与集成(管家桥)。图标=同 icons chunk 的 lucide（house/activity/settings/clock/user/book-open/wrench）。
- **组件类全部按部署版 cva 抄**：panel=配置分区 `rounded-lg border bg-card p-4 sm:p-6 space-y-3`+h3 text-base 600+pdesc text-sm muted；frow=表单行（sm 起 label 左/控件右 320px，wide 整行）；ui-btn（default/secondary/outline/ghost/destructive × sm/icon）；ui-switch h-5 w-9（选中 primary，thumb translate-x-16px）；ui-tabs（list bg-muted rounded-lg p-1+active bg-background shadow-sm）；ui-tbl（th text-xs muted 500/td border-b/hover bg-muted/50）；statgrid/statc 统计卡；ui-badge 四变体；弹窗=mask 黑 50%+rounded-lg border shadow；chips。
- **主题令牌**：`:root` 用部署版 HSL 三元组（--primary 28.9 94.8% 45.1% 橙 / --accent 112.7 40.2% 47.8% 绿 / --muted-foreground 188.5 20% 46.9% 等），排版 var 同部署版（base 16px/sm 14/xs 12，mono=JetBrains Mono）。观察页内部样式变量沿用 `--mo-*` 前缀（历史命名）：primary #e06f06 / card #fbfcfc / secondary #f1f7f8 / muted-fg #608990 / accent #55ab49（选中会话绿底）/ border #e5eced / destructive #d31212 + tailwind 原样 emerald-500 #10b981、blue #3b82f6、amber-500 #f59e0b、teal #14b8a6、purple #a855f7、red #ef4444；字体栈 -apple-system…Arial；徽章基类 rounded-md(6px) px-2.5 py-0.5 text-xs(12px) font-semibold。观察页空态文案逐字：「等待 MaiSaka 会话…」「等待 MaiSaka 推理事件…」「当 MaiSaka 处理新消息时，推理过程会实时展示在这里」「当前聊天流暂无阶段状态」。
- **图标禁 emoji**：全站图标走 `MO_ICONS`+`moIcon()`（lucide path 数据，提取法见 §5）。
- 交互保留：collect()/save() 的全部字段 id 与行选择器（data-cp/tvr/gp/elr/egr/kwr/rxr/le-*/lj-*）原样保留，模板换壳不换数据面；工具/技能弹窗、人格弹窗、学习库编辑逻辑不变。
- 页面映射：概览→部署版首页统计卡+表格卡；麦麦设置→bot 配置页（h1+ui-tabs 核心/详细+分区）；发言节奏→bot 配置分区集；人格管理/学习→资源管理页（统计条+搜索工具栏+ui-tbl）；管家桥→maisoul 独有，按同一分区样式自写。
- WebUI 排查口诀：对比展开前后 DOM 找新增元素、查 computed style 实际值，不要只看 CSS class；排查底纹/阴影/半透明/模糊/颜色叠加：先按 DOM 层级拆父容器/触发器/内部装饰/伪元素，逐层查 background/backdrop-filter/box-shadow/opacity（MaiBot 原文）。

## 8. 已知非对标项清单

1. **上下文内联（仅 replyer 单轮路径）**：MaiBot 把聊天记录/表达习惯/黑话参考作独立 context 消息；maisoul 的 replyer 生成内联进一条 user message（Planner 循环已用 contexts 参数跨轮累积）。信息等价，结构不同。
2. **黑话高频词提示未移植**：依赖 MaiBot 独立高频词学习器（属已取舍的学习器家族）。
3. **引用回复（部分接通）**：reply 工具 set_quote（默认 true）与独立模式 enable_reply_quote 已接——首段 MessageChain 挂 `Reply(id=目标消息id)`。**错字纠正 quote_previous 未接**：引用对象是自己刚发的消息，而 context.send_message 只返回 bool 拿不到 message_id（坑 21），维持普通文本发送。
4. **focus/注意力漂移/情景分析子代理**：未移植（见 §4）。
5. **maisoul 独有扩展（MaiBot 之外的加项）**：预设对话（preset_dialogues 注入【预设对话】块，人格可覆盖）、多人格、管家桥（call_maid 桥+单轮回填）、independent/native 模式、逃生舱（escape_at_wake）、总开关、native 三件套注入、情绪 VA 模型+频率窗口反馈（见 ARCHITECTURE.md §5/§6）。
6. **用户明示同意的取舍**：A_memorix→livingmemory、偷表情→stealer、行为/高频词学习、聊天回想、世界书/好感度（见 §3 表）。
7. **工程差异（行为一致）**：错字引擎 jieba 词典进程内缓存；学习器为发言后异步任务（受 max_expression_learner 信号量约束）而非逐消息队列。
