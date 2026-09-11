# AGENTS.md — astrbot_plugin_maisoul 开发准则

> 本文档面向所有开发者（人与 AI），只写规则与命令。架构、配置全表、对标规格、坑库见 §7 文档索引，按需查阅。
> 本机部署实值（容器名/端口/路径）在 AGENTS.local.md，不入库；文档示例一律用占位符。

## 1. 项目概述

把 MaiBot（麦麦）的 QQ 群聊发言流水线完整移植为 AstrBot 插件，**完全取代 AstrBot 原生聊天回复路径**，同时保留 AstrBot 插件/agent 框架作逃生舱。核心目标：**复刻保真**——行为、配置、提示词、默认值、实现写法、WebUI 外观全部对标 MaiBot 官方仓库（github.com/Mai-with-u/MaiBot）及其部署实例。聊天全权由本插件接管；复杂 agent 操作经 `call_maid` 转管家插件；显式指令放行原生。

## 2. 环境与构建命令

```bash
# 容器内跑测试
sudo docker exec <astrbot容器> python3 <容器内插件路径>/tests/test_core.py

# pytest 入口（与上共用同一套用例）
python -m pytest tests/ -q

# 离线测试（不动运行容器；black --check 验格式门）
docker run --rm -v <仓库父目录>:/src -w /src/astrbot_plugin_maisoul soulter/astrbot:latest \
  sh -c "pip install -q pypinyin jieba pytest black==26.5.1; python -m pytest tests/ -q; python3 tests/test_core.py"

# 重载插件（dashboard API，body {"name":"astrbot_plugin_maisoul"}）
curl -X POST -H "Authorization: Bearer <JWT>" http://<dashboard地址>:<端口>/api/plugin/reload
```

- **改 `pipeline/*` 后必须 `docker restart <astrbot容器>`**——插件 reload 不级联子包（坑 59）；core/页面/配置 reload 即可。
- 插件 WebAPI 前缀：`/api/v1/plugins/extensions/astrbot_plugin_maisoul/<子路径>`（旧式无 v1 会 404）；`/api/plugin/reload`、`/api/chat/send` 无 v1 前缀。
- 不接 QQ 测整条管线：WebUI 聊天页直发（webchat=私聊事件）；或聊天里 `/maisoul sim <文本>` 强制走私聊全管线并输出门控明细；或 `POST /api/chat/send`（body 含 `session_id`/`message`/`stream:false`，webchat 会话 id 形如 `webchat!<用户名>!<uuid>`，可 `docker logs <astrbot容器> --since 2h | grep -o "webchat![^ ,)\"']*"` 抓取）。
- 切换发言模式：`/maisoul planner|independent|native`。
- 排查为何不说话：`docker logs <astrbot容器> | grep maisoul`（门控明细 + planner 决策日志）。
- 改 pages/ 后重载插件 + 浏览器 Ctrl+F5（内容接口有缓存）。
- 铸 dashboard JWT（600~900s 过期，过期重铸）：

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

## 3. 测试指令

1. **改任何 core/ 逻辑必须先补测试再改实现**；纯配置改动（schema + 页面 render/collect + 文档映射表）不建测试文件。
2. tests/test_core.py 双入口：自执行模式聚合计数；pytest 模式（检测 `PYTEST_CURRENT_TEST`）失败即抛逐用例红绿。
3. `PlannerState` 加字段后跑 webchat 端到端（缺字段 AttributeError 走静默死亡路径，坑 46）。
4. 改会话 UI 必须造两个会话验证（单会话测不出过滤 bug，坑 18）。
5. 提交前测试全绿；沙盒测试要验真——工具缺失=空验证，失败路径必须真的失败过一次。

## 4. 代码风格与规范

1. 代码格式 = **black 26.5.1**（CI `format-check` 锁版本）；升 black 大版本前先全库重排单独成提交。
2. import 顺序：`from X import Y` 在前、`import X` 在后，两组各按字母序；标准库/第三方在前、本地在后，块间空行。core/ 内相对导入；astrbot 框架对象统一 `from astrbot...` 绝对导入。
3. core/ 是纯函数/纯数据类，**不 import astrbot 运行时对象**（bridge.py 是唯一运行时边界）；main.py 只做接线，业务逻辑一律进 core/ 或 pipeline/。
4. 注释、日志、WebUI 文案一律简体中文；长/复杂逻辑块必须写注释；重构时原注释可修正不可删。
5. 类型注解：重构保留原注解；复杂/多参数函数补注解；泛型用 typing 模块（`list[int]`）。
6. 类型已确定时**禁止 `or` fallback 链**（`cfg["bot_name"] or ""` 属掩盖问题）；少用 getattr/setattr（bridge.py 对 AstrBot 动态属性例外）。
7. **debug 铁律：不许 fallback/兜底掩盖错误**——有错完整暴露（logger.error + 异常栈）。显式降级路径必须留 error 日志，禁止静默吞异常。
8. 一切"对象转文本"边界取 `.text`，**禁止 str() 整个对象**（坑 51）。
9. 会话键全链路统一：群=group_id、私聊=sender_id；业务代码不得自拼会话键或造 fallback hash（坑 23）。
10. 配置迁移保持幂等；已发布迁移路径不可改，不擅自新增迁移步骤。
11. AstrBot 框架 API/schema 动手前先查框架文档：llm_tool 参数类型只认 string/number/object/array/boolean 五类，写 `integer` 拒载整插件（坑 30b）。
12. WebUI（pages/dashboard/）铁律：**零外部网络依赖、无构建链**——禁 CDN/网络字体/外部请求；本地静态文件分离（index.html 壳 + app.css + app.js + 数据文件 mbrc.css.js，相对路径引用由服务端自动改写补 asset_token）；HTML 内联内容禁 `</body>` 闭合标签字面量（服务端 bridge-sdk 注入替换首个 `</body>`，坑 14）；桥必须 `ensureBridge()`；图标 lucide SVG 禁 emoji；JS 里装 CSS/大段数据用 `String.raw` 标签模板（坑 66）。
13. changelog 以 git 提交信息承载，无独立 changelog 文件。

## 5. 协作边界与禁令

**必须做**：

1. 复刻保真：凡涉及"对齐 MaiBot"，动手前先对照官方源码与部署实例产物，不凭记忆直觉；对标验收物是请求 dump / 构建产物逐项 diff（规格见 docs/MAIBOT_FIDELITY.md）。
2. 加配置项流程：_conf_schema.json 加字段（键名/默认值/描述先查 MaiBot official_configs.py）→ core 读 `cfg.get` → 页面 render/collect → 测试断言 → docs/CONFIG.md 映射表同步。
3. 版本号**八处**同步（坑 12 全清单，按字面量逐条打勾）：metadata.yaml、main.py `@register`、模块 docstring、已加载日志、已卸载日志、PAGE_VERSION（pages/dashboard/app.js）、status API、pipeline/admin.py 状态行。
4. 提交前跑 AGENTS.local.md 的红线 grep。

**先问再做**：

5. 偏离 MaiBot 的写法（即便自认更优）——须有机制依据，并在 docs/MAIBOT_FIDELITY.md 非对标清单标注差异与理由。
6. 新增配置迁移步骤 / 改 legacy 迁移。

**禁止**：

7. 禁止为 MaiBot 已有功能重复造轮子；已由生态覆盖的不复刻：表情→stealer、长期记忆→livingmemory、世界书/好感度→生态注入桥、聊天回想→不移植（取舍清单见 FIDELITY §3）。
8. 禁止提交任何真实环境数据与运行时数据：容器名/端口/路径、QQ 号/群号/会话 ID、真实人设、密钥、学习库/观察账本——示例一律中性占位符（`<astrbot容器>`、`<端口>`）；schema 默认值必须中性。
9. 新装纯净红线：插件安装后状态必须等于干净新插件——交付包 ≡ `git archive` 输出，无运行时数据、无本机信息；运行时数据存 `data/plugin_data/<插件名>/`（坑 50）。
10. 禁止自造前端事件/卡片：对标协议以 MaiBot 后端 emit 点为准，一个不多一个不少（坑 45）。
11. 禁止无边界格式化/导入整理/大面积整理 diff——确需整理单独成提交并在正文说明范围，不与功能改动混在一起。
12. 实验目录、临时脚本、比对用私有产物不经确认不进共享历史。

## 6. Git 与 PR 规范

1. Conventional Commits（<https://www.conventionalcommits.org>）：`<type>(<scope>): 祈使句摘要`——一行、动词开头、≤72 字符硬线；正文写**为什么**；不列文件清单（文件移动/全局配置/对外 API 变更三种情况例外）。
2. 版本号联动：`fix`→patch、`feat`→minor、`BREAKING CHANGE`→major（联动八处同步）。
3. PR 四段：① 改动简述（用户可感知）② 为什么改 ③ 核心改动（1-2 个关键文件或风险点）④ 测试情况。reviewer 不点开 Files changed 就能懂 = 合格。
4. 流程：开分支 → 提交 → `gh pr create` → 页面审阅合并。审阅 AI（Sourcery 等）的评论不回复，直接修复；修复提交落在引入该问题的分支。
5. 自审纪律：任何改动写完先逐行自审 + 本地实测，全绿才 commit/push。

## 7. 关键文档索引

| 触发场景 | 文档 |
|---|---|
| 改架构/数据流/目录结构/模式分工/情绪耦合 | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| 改配置字段/查 MaiBot 映射 | [docs/CONFIG.md](docs/CONFIG.md) |
| 对标 MaiBot 行为/提示词/观察页/推理页/WebUI 像素/取舍清单 | [docs/MAIBOT_FIDELITY.md](docs/MAIBOT_FIDELITY.md) |
| 排查问题/动 AstrBot 框架交互/前端 | [docs/PITFALLS.md](docs/PITFALLS.md)（65 条坑，**编号稳定，代码注释与测试按「坑 N」引用**） |
| 本机环境实值/隐私红线名单/推送节奏 | AGENTS.local.md（已列 .gitignore，永不入库） |

外部参考：MaiBot 官方仓库 <https://github.com/Mai-with-u/MaiBot>（复刻保真蓝本，含其 AGENTS.md 代码规范）；AstrBot 框架 <https://github.com/AstrBotDevs/AstrBot>（插件宿主）。
