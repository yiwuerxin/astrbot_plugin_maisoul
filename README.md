# 麦麦之魂 (astrbot_plugin_maisoul)

> 把 [MaiBot（麦麦）](https://github.com/MaiM-with-u/MaiBot) 的 QQ 群聊发言流水线**源码级复刻**为 AstrBot 插件：触发意愿门控、三件套 Prompt、Planner 决策、拟人分段发送——聊天完全交给麦麦，复杂 agent 操作桥接给管家。

## 简介

麦麦之魂**完全取代 AstrBot 原生的群聊/私聊回复路径**，同时保留 AstrBot 的插件与 agent 框架能力作为"逃生舱"：

| 能力 | 归属 |
|---|---|
| 全部聊天（群聊+私聊：人格/意愿/节奏/发送） | 本插件全权接管 |
| 复杂 agent 操作（查资料/跑任务/工具链） | `astrbot_plugin_maid_agent`（经 `call_maid` 管家桥调用） |
| 显式召唤（`/指令`、其他插件触发） | AstrBot 原生路径（一律放行，原生插件照常可用） |
| @机器人 / 唤醒前缀 | **麦麦管线接管**：作为 at 档显式点名强制触发（`escape_at_wake=true` 可改回放行原生） |

## 功能特性

- **三种发言模式**（聊天里 `/maisoul planner|independent|native` 可随时切换）
  - `planner`（默认）：完整复刻 MaiBot maisaka 决策层——agent 循环、reply/wait/send_emoji/fetch_history 工具、wait 状态机、思考打断、空闲指数退避、上下文折叠、防复读
  - `independent`：麦麦流水线直跑——触发门控 → 三件套 Prompt 生成 → 拟人发送
  - `native`：触发后交 AstrBot 原生 agent，本插件注入三件套
- **触发门控**（对标 MaiBot turn_scheduler/turn_gates）：frequency 频率触发（攒消息+空窗补偿）/ reply_necessity 评分触发（档位+内容分+压力分+存在感惩罚）；talk_value 动态规则（按群/私聊×时段）；@ 必回、提及可配
- **三件套 Prompt**：人格设定/表达风格/行为风格（behavior_style 只进 planner），备用表达风格彩票池，群聊/私聊额外 Prompt 精确匹配
- **拟人发送**：回复后处理管线（括号心声清除、分句+概率合并、条数上限、颜文字保护）+ 拼音字频错字引擎（同音字/声调/整词替换+纠正消息）+ 打字延迟
- **聊天学习**：表达习惯学习、黑话学习（上下文命中注入）、关键词/正则反应规则（支持命名捕获组）、优化上下文
- **多人格管理**：人格库 + 群绑定 + 默认人格，兼容 `astrbot_plugin_persona_switch` 的 `/persona` 切换
- **麦麦观察**：会话/阶段/时间线实时观察页（对齐 MaiBot maisaka monitor 的 9 个事件，SQLite 账本 + 增量重放）
- **任务级模型绑定**：planner/replyer/表情检索/学习器可分别绑定不同模型与选择策略（sequential/random/balance）
- **管家桥**：聊天模型可调 `call_maid` 把复杂任务交给 AstrBot 的完整 agent 能力，结果用麦麦口吻转达
- **生态注入桥**：生成前手动触发 `on_llm_request` 钩子链、发言后触发 `on_llm_response`——心弦好感/记忆/世界书等注入型插件在麦麦管线内同样生效（v6.11.0+，`eco_injection` 可关）
- **WebUI 管理面板**：概览/麦麦设置/发言节奏/人格管理/学习/管家桥/麦麦观察 七页（MaiBot 部署版风格，零外部依赖）

## 前置要求

- AstrBot v4.2x+
- Python 依赖安装见 `requirements.txt`（jieba / pypinyin / sqlmodel）

**推荐的生态插件**（可选，装了自动联动）：

| 插件 | 作用 |
|---|---|
| `astrbot_plugin_maid_agent` | 管家桥对端：`call_maid` 工具的执行者 |
| `astrbot_plugin_stealer` | 表情包系统（MaiBot emoji_system 的移植+增强），经 `chat_tools` 暴露 `send_meme` |
| `astrbot_plugin_persona_switch` | `/persona` 切换人格与本插件多人格库联动 |
| `astrbot_plugin_livingmemory` / `astrbot_plugin_xinxian` / `astrbot_plugin_worldbook` | 记忆/好感度/世界书：**全模式生效**（生态注入桥在麦麦管线内收集注入、发言后触发记忆沉淀；v6.11.0+） |

心弦（v1.31.0+）另有 **§6.6 情绪-关系耦合**（数值面，默认关）：连续同向情绪累积
`pfb(±7)` 经 `star_cls.api` 供心弦查增益表调制好感增量（方向①），心弦好感等级跃迁
反向推送情绪事件（方向②）；只交换数值，不渲染提示词。生效链：方向① =
`emotion_enable`+`emotion_feedback_enable`+心弦 `favor.mood_coupling` 三开；方向② =
心弦 `favor.mood_push`+`emotion_enable` 两开。

## 安装

**方式一：WebUI 插件市场**（上架后可用）

**方式二：手动克隆**

```bash
cd AstrBot/data/plugins
git clone <本仓库地址> astrbot_plugin_maisoul
```

重启 AstrBot（或在 WebUI 重载插件）后，进入 WebUI 的「麦麦之魂」面板完成配置。

## 快速上手

1. **基础配置**：WebUI → 插件面板 → 「麦麦设置」页，填 `bot_name`（默认"麦麦"）、人格设定（`personality`）、表达风格（`reply_style`）、行为风格（`behavior_style`，planner 模式的行动准则）
2. **话痨程度**：`talk_value`（全局 0~1+，越大越爱说话）；分群/分时段用 `talk_value_rules`；单群语气用 `chat_prompts`
3. **触发方式**：`reply_trigger_mode`（`frequency`=频率触发 / `reply_necessity`=评分触发）；`mentioned_bot_reply`（提及必回，默认关）、`inevitable_at_reply`（@必回，默认开）
4. **私聊**：独立频率 `private_talk_value`、独立上下文 `max_private_context_size`、私聊提示词 `private_chat_prompts`——语义完全对标 MaiBot

全部配置项在 WebUI 面板内可直接编辑；字段名/默认值与 MaiBot 官方配置逐条对标（对照表见 [AGENTS.md](AGENTS.md) §4）。

## 排查

```bash
# 看门控明细（频率/模式/阈值/评分）与 planner 决策日志
docker logs <astrbot容器> | grep maisoul

# 不接 QQ 测试整条管线：聊天里发
/maisoul sim <文本>
```

更多排查手段与开发规范见 [AGENTS.md](AGENTS.md)。

## 开发

```bash
# 单元测试（227 用例，在 AstrBot 容器内跑）
python3 tests/test_core.py
```

架构、数据流、配置映射、MaiBot 保真对照与已知取舍清单全部记录在 [AGENTS.md](AGENTS.md)。

## 致谢

- [MaiBot](https://github.com/MaiM-with-u/MaiBot) —— 本插件是麦麦发言流水线的源码级移植，触发门控/评分/错字引擎/后处理/学习器/Planner 决策层/观察事件均以其源码为准
- [AstrBot](https://github.com/AstrBotDevs/AstrBot) —— 插件与 agent 框架底座

## 许可证

[AGPL-3.0](LICENSE) —— 因本插件对 MaiBot（AGPL-3.0）做了源码级移植，随附 AGPL-3.0 许可证分发。
