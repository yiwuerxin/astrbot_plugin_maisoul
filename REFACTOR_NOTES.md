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
- **P-A 中期记忆**（`memory_enable` 默认关）：窗口外旧消息后台摘要为{总结,线索}，Jaccard 词集召回 ≤3 条注入 replyer（标注内部参考）。
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
