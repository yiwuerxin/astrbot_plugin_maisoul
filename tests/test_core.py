"""maisoul 核心模块单元测试 —— 容器内直接运行：python3 tests/test_core.py

覆盖：触发门控（双模式/空窗补偿/强制触发/动态频率规则）/ 必要性评分 /
回复后处理（括号心声/分句/条数上限/打字时间）/ 错字引擎冒烟 /
prompt 组装 / 会话状态 / 多人格 / 工具集暴露策略。
"""

import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

# CI 等无 AstrBot 运行时的环境：注入桩模块，识图/引用链用例离线可跑
# （容器内有真实 astrbot 时不生效，仍测真实类。构造语义对齐：
#   ImageURLPart(image_url={"url": ...}) → .image_url.url / .type == "image_url"
#   MessageChain([Reply(id=...)]).message(text) → .chain / Reply.toDict() OneBot 段）
try:
    from astrbot.api.event import MessageChain  # noqa: F401
    from astrbot.api.message_components import Plain, Reply  # noqa: F401
    from astrbot.core.agent.message import ImageURLPart  # noqa: F401
    _HAS_REAL_ASTRBOT = True
except ImportError:
    _HAS_REAL_ASTRBOT = False
    import types as _types

    class _StubImageURL:
        def __init__(self, url=None):
            self.url = url

    class _StubImageURLPart:
        type = "image_url"

        def __init__(self, image_url=None):
            if isinstance(image_url, dict):
                image_url = _StubImageURL(url=image_url.get("url"))
            self.image_url = image_url

    class _StubPlain:
        def __init__(self, text=""):
            self.text = text

    class _StubReply:
        def __init__(self, id=None):
            self.id = id

        def toDict(self):
            return {"type": "reply", "data": {"id": self.id}}

    class _StubMessageChain:
        def __init__(self, chain=None):
            self.chain = list(chain or [])

        def message(self, text):
            self.chain.append(_StubPlain(text))
            return self

    class _StubLogger:
        # core 模块级 from astrbot.api import logger——离线环境吞日志即可
        def __getattr__(self, name):
            return lambda *a, **k: None

    class _StubToolSet:
        # bridge.build_chat_toolset 用：add_tool/empty/tools 三件套
        def __init__(self, tools=None):
            self.tools = list(tools or [])

        def add_tool(self, tool):
            self.tools.append(tool)

        def empty(self):
            return not self.tools

    class _StubFunctionTool:
        # 纯数据记录：list_astrbot_tools 逻辑测试用
        def __init__(self, name="", description="", parameters=None,
                     handler=None, handler_module_path=None):
            self.name = name
            self.description = description
            self.parameters = parameters
            self.handler = handler
            self.handler_module_path = handler_module_path

    class _StubMCPTool:
        # list_astrbot_tools 的 isinstance 分支判别用（测试内无 MCP 实例）
        pass

    _pkg = _types.ModuleType("astrbot")
    _core = _types.ModuleType("astrbot.core")
    _agent = _types.ModuleType("astrbot.core.agent")
    _msg = _types.ModuleType("astrbot.core.agent.message")
    _tool = _types.ModuleType("astrbot.core.agent.tool")
    _mcp = _types.ModuleType("astrbot.core.agent.mcp_client")
    _star = _types.ModuleType("astrbot.core.star")
    _api = _types.ModuleType("astrbot.api")
    _comp = _types.ModuleType("astrbot.api.message_components")
    _evt = _types.ModuleType("astrbot.api.event")
    _msg.ImageURLPart = _StubImageURLPart
    _tool.ToolSet = _StubToolSet
    _tool.FunctionTool = _StubFunctionTool
    _mcp.MCPTool = _StubMCPTool
    _star.star_map = {}
    _comp.Plain = _StubPlain
    _comp.Reply = _StubReply
    _evt.MessageChain = _StubMessageChain
    _api.logger = _StubLogger()
    _pkg.core = _core
    _pkg.api = _api
    _core.agent = _agent
    _core.star = _star
    _agent.message = _msg
    _agent.tool = _tool
    _agent.mcp_client = _mcp
    _api.message_components = _comp
    _api.event = _evt
    for _name, _mod in {"astrbot": _pkg, "astrbot.core": _core,
                        "astrbot.core.agent": _agent,
                        "astrbot.core.agent.message": _msg,
                        "astrbot.core.agent.tool": _tool,
                        "astrbot.core.agent.mcp_client": _mcp,
                        "astrbot.core.star": _star,
                        "astrbot.api": _api,
                        "astrbot.api.message_components": _comp,
                        "astrbot.api.event": _evt}.items():
        sys.modules[_name] = _mod

from astrbot_plugin_maisoul.core import postprocess, prompt, scoring, sender, trigger  # noqa: E402
from astrbot_plugin_maisoul.core.constants import OUTPUT_INSTRUCTION  # noqa: E402
from astrbot_plugin_maisoul.core.states import GroupState, StateManager  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond, info=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {info}")


def make_state(msgs, *, pending=None, self_times=None, intervals=None):
    st = GroupState()
    now = time.time()
    for i, (name, text, at) in enumerate(msgs):
        st.buffer.append({"name": name, "sid": name, "msg_id": f"m{i}", "text": text,
                          "at_bot": at, "reply_bot": False, "ts": now - 60 + i})
    if pending is not None:
        st.pending_since_fire = pending
    for t in (self_times or []):
        st.recent_self.append(t)
    for i in range(len(intervals or [])):
        st.ext_intervals.append(now - 60 + i * 2)
    if intervals:
        st.last_ext_ts = now - 60 + (len(intervals) - 1) * 2
    return st


BASE_CFG = {
    "reply_trigger_mode": "frequency", "talk_value": 1.0,
    "inevitable_at_reply": True, "mentioned_bot_reply": False,
    "enable_talk_value_rules": False, "talk_value_rules": [],
    "bot_name": "麦麦", "aliases": ["小麦"],
}


def test_trigger():
    print("[触发门控]")
    check("阈值: frequency f=0.5→ceil(1/f)=2", trigger.message_trigger_threshold("frequency", 0.5) == 2)
    check("阈值: necessity f=0.5→ceil(1/f²)=4", trigger.message_trigger_threshold("reply_necessity", 0.5) == 4)
    check("阈值: f=1→1", trigger.message_trigger_threshold("frequency", 1.0) == 1)
    check("阈值: f=0→0(静默)", trigger.message_trigger_threshold("frequency", 0.0) == 0)

    # 频率触发：攒够条数
    st = make_state([("u", "哈", False)] * 2, pending=2, intervals=[1, 2])
    fired, detail, _ = trigger.should_trigger(st, BASE_CFG, at_bot=False, mentioned=False,
                                              text="哈", aliases=["小麦"], bot_name="麦麦",
                                              platform="qq", chat_id="g1")
    check("频率触发: f=1 攒 1 条即触发", fired, detail)

    st = make_state([("u", "哈", False)], pending=1, intervals=[1])
    st.last_ext_ts = time.time()  # 刚收到，无空窗
    fired, detail, _ = trigger.should_trigger(st, dict(BASE_CFG, talk_value=0.5), at_bot=False,
                                              mentioned=False, text="哈", aliases=[], bot_name="麦麦",
                                              platform="qq", chat_id="g1")
    check("频率触发: f=0.5 阈值2 条数不足不触发", not fired, detail)

    # 空窗补偿：1 条 + 长空窗 折算补齐
    st = make_state([("u", "哈", False)], pending=1, intervals=[1, 2, 3])
    st.last_ext_ts = time.time() - 120  # 空窗 120s，平均间隔 2s → 折算 59 条，封顶 threshold-1
    fired, detail, _ = trigger.should_trigger(st, dict(BASE_CFG, talk_value=0.5), at_bot=False,
                                              mentioned=False, text="哈", aliases=[], bot_name="麦麦",
                                              platform="qq", chat_id="g1")
    check("频率触发: 空窗补偿补齐触发", fired, detail)

    st.pending_since_fire = 0
    fired, detail, _ = trigger.should_trigger(st, dict(BASE_CFG, talk_value=0.5), at_bot=False,
                                              mentioned=False, text="哈", aliases=[], bot_name="麦麦",
                                              platform="qq", chat_id="g1")
    check("频率触发: 纯沉默不触发", not fired, detail)

    # 静默接收
    st = make_state([("u", "哈", False)], pending=5, intervals=[1])
    fired, detail, _ = trigger.should_trigger(st, dict(BASE_CFG, talk_value=0.0), at_bot=True,
                                              mentioned=True, text="哈", aliases=[], bot_name="麦麦",
                                              platform="qq", chat_id="g1")
    check("静默接收: talk_value=0 连 @ 也不发", not fired, detail)

    # 强制触发
    st = make_state([("u", "哈", False)], pending=0, intervals=[1])
    st.last_ext_ts = time.time()
    fired, detail, _ = trigger.should_trigger(st, BASE_CFG, at_bot=True, mentioned=False,
                                              text="哈", aliases=[], bot_name="麦麦",
                                              platform="qq", chat_id="g1")
    check("强制触发: @ 必回复(默认开)", fired, detail)
    fired, _, _ = trigger.should_trigger(st, BASE_CFG, at_bot=False, mentioned=True,
                                         text="哈", aliases=[], bot_name="麦麦",
                                         platform="qq", chat_id="g1")
    check("提及必回复默认关: 不强制", not fired, detail)
    fired, _, _ = trigger.should_trigger(st, dict(BASE_CFG, mentioned_bot_reply=True),
                                         at_bot=False, mentioned=True, text="哈", aliases=[],
                                         bot_name="麦麦", platform="qq", chat_id="g1")
    check("提及必回复开: 强制触发", fired)

    # 必要性触发模式
    st = make_state([("u", "麦麦，帮我看看这个", False)], pending=1, intervals=[1, 2, 3])
    fired, detail, nec = trigger.should_trigger(st, dict(BASE_CFG, reply_trigger_mode="reply_necessity"),
                                                at_bot=False, mentioned=True,
                                                text="麦麦，帮我看看这个", aliases=["麦麦"],
                                                bot_name="麦麦", platform="qq", chat_id="g1")
    check("必要性触发: 提及+请求过 80 分", fired and nec is not None and nec.score >= 80, detail)

    # 动态频率规则
    rules = dict(BASE_CFG, enable_talk_value_rules=True, talk_value=0.2, talk_value_rules=[
        {"platform": "", "item_id": "", "rule_type": "group", "time": "00:00-23:59", "value": 0.5},
        {"platform": "qq", "item_id": "g1", "rule_type": "group", "time": "*", "value": 1.0},
    ])
    check("动态规则: 精确匹配优先于通配", trigger.effective_talk_value(rules, "qq", "g1") == 1.0)
    check("动态规则: 未命中群回落时段规则", trigger.effective_talk_value(rules, "qq", "gX") == 0.5)
    check("动态规则: 关闭时用基础值", trigger.effective_talk_value(BASE_CFG, "qq", "g1") == 1.0)
    night = dict(rules, talk_value_rules=[
        {"platform": "", "item_id": "", "rule_type": "group", "time": "00:00-08:59", "value": 0.1},
        {"platform": "", "item_id": "", "rule_type": "group", "time": "09:00-23:59", "value": 0.9},
    ])
    import datetime
    noon = datetime.datetime(2026, 8, 29, 12, 0).timestamp()
    check("动态规则: 时段命中取对应值", trigger.effective_talk_value(night, "qq", "g1", now=noon) == 0.9)
    check("动态规则: private 规则在群聊不参与", trigger._rule_target_priority(
        {"platform": "", "item_id": "", "rule_type": "private"}, "qq", "g1", True) is None)
    check("私聊: 基础频率用 private_talk_value",
          trigger.effective_talk_value(dict(BASE_CFG, private_talk_value=0.5), "qq", "u1", is_group=False) == 0.5)
    check("私聊: private 规则参与匹配",
          trigger._rule_target_priority(
              {"platform": "qq", "item_id": "u1", "rule_type": "private"}, "qq", "u1", False) == 5)
    prules = dict(BASE_CFG, is_group_cfg=False, private_talk_value=0.2, enable_talk_value_rules=True,
                  talk_value_rules=[{"platform": "qq", "item_id": "u1", "rule_type": "private",
                                     "time": "*", "value": 1.0}])
    check("私聊: 动态规则覆盖私聊基础值",
          trigger.effective_talk_value(prules, "qq", "u1", is_group=False) == 1.0)


def test_scoring():
    print("[必要性评分]")

    def ev(text, *, at_bot=False, aliases=("麦麦",), bot_name="麦麦", frequency=0.5, st=None):
        st = st or make_state([("u", text, at_bot)], pending=1, intervals=[1, 2, 3])
        return scoring.evaluate(st, at_bot=at_bot, text=text, aliases=list(aliases),
                                bot_name=bot_name, frequency=frequency)

    r = ev("@麦麦 帮我看看这个报错", at_bot=True)
    check("被@直通且长回复", r.score >= 80 and r.style == "长回复", f"{r.score} {r.style}")

    r = ev("麦麦，帮我看看这个", at_bot=False)
    check("提及=80档+请求触发", r.score >= 80, str(r.score))

    r = ev("今天午饭吃什么", at_bot=False)
    check("单条闲聊不触发", r.score < 80, str(r.score))

    r = ev("DeepSeek，帮我写个脚本", at_bot=False)
    check("叫别的AI被抑制", r.score < 20, str(r.score))

    st = make_state([(f"q{i}", "今天天气不错啊大家", False) for i in range(25)], pending=25,
                    intervals=list(range(8)))
    r = scoring.evaluate(st, at_bot=False, text="今天天气不错啊大家",
                         aliases=["麦麦"], bot_name="麦麦", frequency=0.5)
    check("积压25条满压插话(简短表达)", r.score == 75 and r.style == "简短表达", f"{r.score} {r.style}")

    st = make_state([("u", "麦麦 你觉得呢", False)], pending=1, intervals=[1, 2, 3])
    for _ in range(8):
        st.recent_self.append(time.time() - 30)
    for i in range(16):
        st.buffer.append({"name": "x", "sid": "y", "msg_id": "", "text": "m",
                          "at_bot": False, "reply_bot": False, "ts": time.time() - 60})
    noisy = scoring.evaluate(st, at_bot=False, text="麦麦 你觉得呢", aliases=["麦麦"],
                             bot_name="麦麦", frequency=0.5).score
    st.recent_self.clear()
    quiet = scoring.evaluate(st, at_bot=False, text="麦麦 你觉得呢", aliases=["麦麦"],
                             bot_name="麦麦", frequency=0.5).score
    check("存在感惩罚生效", noisy < quiet, f"{noisy} < {quiet}")

    check("频率0.1倍率0.55", abs(scoring.freq_factor(0.1) - 0.55) < 1e-9)
    check("压力: 4/4无闲置=50", scoring.pressure_score(4, 4, False) == 50)
    check("压力: 超阈值对数封顶", scoring.pressure_score(400, 4, False) == 100)


def test_text_rules():
    print("[文本规则]")
    check("噪声清洗: 引用剥离", scoring.strip_noise("[CQ:reply,id=1] 你好呀") == "你好呀")
    check("噪声清洗: @剥离", scoring.strip_noise("@麦麦 怎么弄") == "怎么弄")
    check("噪声清洗: 图片占位为空", scoring.strip_noise("[图片：xx.jpg]") == "")
    check("问句: 吗结尾", scoring.is_question("这个能行吗"))
    check("问句: ？结尾", scoring.is_question("你要去哪里？"))
    check("非问句: 纯感叹短词", not scoring.is_question("！！好"))
    from astrbot_plugin_maisoul.core.constants import SHORT_REACTIONS
    check("短反应: 哈哈", "哈哈" in SHORT_REACTIONS)


def test_postprocess():
    print("[回复后处理]")
    ct = postprocess.calculate_typing_time
    check("打字: 单汉字3倍+0.3", abs(ct("好") - 1.2) < 1e-9)
    check("打字: 9汉字=2.7(无额外回车)", abs(ct("这句话有点长啊朋友") - 2.7) < 1e-9)
    check("打字: typing_speed=2 翻倍", abs(ct("你好呀", typing_speed=2) - 0.9 * 2) < 1e-9)
    check("打字: typing_speed=0 不等待", ct("你好呀", typing_speed=0) == 0)

    off = {"enable_response_post_process": False}
    segs = postprocess.process_response_segments("你好（心想：好累）呀", off)
    check("总开关关: 原文直出", len(segs) == 1 and segs[0].text == "你好（心想：好累）呀")

    cfg = {"enable_response_post_process": True, "splitter_enable": False,
           "typo_enable": False, "bot_name": "麦麦",
           "splitter_max_length": 512, "splitter_max_sentence_num": 8, "splitter_max_split_num": 3}
    segs = postprocess.process_response_segments("你好（心声）呀", cfg)
    check("括号心声被清除", segs[0].text == "你好呀", segs[0].text)
    segs = postprocess.process_response_segments("（只有心声）", cfg)
    check("全为心声→呃呃", segs[0].text == "呃呃")
    segs = postprocess.process_response_segments("好" * 2000, cfg)
    check("全中文超长→默认回复池", any(k in segs[0].text for k in ("不知道", "不晓得", "懒得说", "()")))

    scfg = dict(cfg, splitter_enable=True)
    segs = postprocess.process_response_segments("一句话", scfg)
    check("分句: 短句原样", segs[0].text == "一句话")
    import random as _r
    _r.seed(7)
    segs = postprocess.process_response_segments("今天天气不错。明天也很好。后天更好。", scfg)
    check("分句: 按句分割为多条", len(segs) >= 2, str([s.text for s in segs]))

    mcfg = dict(scfg, splitter_max_sentence_num=1, splitter_max_split_num=3,
                splitter_enable_overflow_return_all=True)
    segs = postprocess.process_response_segments("今天天气不错。明天也很好。后天更好。", mcfg)
    check("超限保留全文", len(segs) == 1)

    async def run():
        sent = []
        async def send(t):
            sent.append(t)
        return await sender.send_humanlike(send, "一\n二", dict(cfg, typing_speed=0))
    check("发送: 段顺序且 typing_speed=0 立即发", asyncio.run(run()) == ["一\n二"])

    # 首段零延迟（对齐 MaiBot typing=index>0）：事件序应为 send,sleep,send,...
    async def run_order():
        from unittest.mock import patch
        events = []
        async def send(t):
            events.append(("send", t))
        real_sleep = asyncio.sleep
        async def fake_sleep(d):
            events.append(("sleep", d))
            await real_sleep(0)
        with patch("astrbot_plugin_maisoul.core.sender.asyncio.sleep", fake_sleep):
            await sender.send_humanlike(send, "一\n二\n三", dict(cfg, typing_speed=1))
        return events
    events = asyncio.run(run_order())
    kinds = [e[0] for e in events]
    check("发送: 首段零延迟（首事件为 send）", kinds[0] == "send", str(events))
    check("发送: 第 2 段起逐段打字延迟",
          kinds.count("sleep") == kinds.count("send") - 1, str(events))

    # 空窗补偿到点重查延迟（对齐 FrequencyThresholdTurnGate delay 分支）
    from collections import deque as _deque
    st2 = GroupState()
    now = time.time()
    st2.ext_intervals = _deque([now - 120, now - 60, now], maxlen=40)
    st2.last_ext_ts = now
    st2.pending_since_fire = 1
    d = trigger.frequency_recheck_delay(st2, 1, 2)
    check("空窗重查: delay=(阈值-积压)×平均间隔-空窗", abs(d - 60.0) < 0.01, str(d))
    st2.last_ext_ts = now - 60  # 空窗已等满 → 到点即查
    check("空窗重查: 空窗已满则 delay=0",
          abs(trigger.frequency_recheck_delay(st2, 1, 2)) < 1e-6)
    check("空窗重查: pending=0 不排期（纯沉默不触发）",
          trigger.frequency_recheck_delay(st2, 0, 2) is None)
    st3 = GroupState()  # 无间隔样本
    st3.pending_since_fire = 1
    check("空窗重查: 平均间隔不可用不排期",
          trigger.frequency_recheck_delay(st3, 1, 2) is None)
    class _FakeTask:
        cancelled = False
        def cancel(self):
            self.cancelled = True
    st4 = GroupState()
    fake = _FakeTask()
    st4.defer_task = fake
    st4.cancel_defer()
    check("状态: cancel_defer 清空重查任务",
          st4.defer_task is None and fake.cancelled)


def test_typo():
    print("[错字引擎]")
    from astrbot_plugin_maisoul.core.typo import ChineseTypoGenerator
    gen = ChineseTypoGenerator(error_rate=0.0, min_freq=9, tone_error_rate=0.0, word_replace_rate=0.0)
    out, fix = gen.create_typo_sentence("今天天气不错")
    check("全零概率: 原样且无纠正", out == "今天天气不错" and fix is None)
    hot = ChineseTypoGenerator(error_rate=1.0, min_freq=0, tone_error_rate=0.0, word_replace_rate=0.0)
    outs = {hot.create_typo_sentence("的")[0] for _ in range(5)}
    check("满概率单字: 引擎可运行", isinstance(outs, set))
    outs = {hot.create_typo_sentence("今天天气真的很好啊")[0] for _ in range(20)}
    check("满概率: 大概率产生错字", len(outs) > 1, str(outs))
    check("字频表已加载", len(gen.char_frequency) > 5000, str(len(gen.char_frequency)))
    # 拼音字典进程级缓存（v6.15.4）：与参数无关的全字符索引只建一次，
    # 生成器重建（调参）复用同一对象；.get 读取不往共享 defaultdict 塞空键
    from astrbot_plugin_maisoul.core import typo as typo_mod
    shared1 = typo_mod._shared_pinyin_dict()
    gen2 = ChineseTypoGenerator(error_rate=0.5, min_freq=9,
                                tone_error_rate=0.1, word_replace_rate=0.0)
    check("拼音缓存: 生成器重建复用同一字典", gen2.pinyin_dict is shared1)
    check("拼音缓存: 索引规模完整", len(shared1) > 300, str(len(shared1)))
    shared1.get("__不存在的音节__", None)
    check("拼音缓存: .get 读取不污染缓存",
          "__不存在的音节__" not in typo_mod._shared_pinyin_dict())


def test_prompt():
    print("[Prompt 组装]")
    cfg = {"bot_name": "麦麦", "aliases": ["小麦"], "personality": "测试人格",
           "reply_style": "简短口语", "behavior_style": "大二学生",
           "group_chat_prompt": "群里要简短", "multiple_reply_style": [], "multiple_probability": 0,
           "chat_prompts": [{"platform": "qq", "item_id": "12345", "rule_type": "group",
                             "prompt": "这个群聊游戏"}]}
    sp = prompt.build_system_prompt(cfg, chat_id="12345", platform="qq")
    check("identity 行", sp.startswith("你的名字是麦麦，也有人叫你小麦。\n测试人格"))
    check("reply_style 注入", "简短口语" in sp)
    check("通用注意事项", "通用注意事项：\n群里要简短" in sp)
    check("每群额外注意事项(精确匹配)", "当前聊天额外注意事项：\n这个群聊游戏" in sp)
    check("输出指令原文", OUTPUT_INSTRUCTION in sp)
    check("behavior_style 不进 replyer 提示词（对齐 MaiBot 分工：只进 planner）",
          "大二学生" not in sp and "行动准则" not in sp)
    sp2 = prompt.build_system_prompt(cfg, chat_id="99999", platform="qq")
    check("其他群不命中额外注意事项", "这个群聊游戏" not in sp2)
    cfg2 = dict(cfg, multiple_reply_style=["文言文"], multiple_probability=100)
    check("风格彩票必中", "本次临时风格" in prompt.select_reply_style(cfg2))
    # 预设对话（maisoul 扩展）：空配置不注入；条目渲染；缺边条目跳过；人格覆盖生效
    from astrbot_plugin_maisoul.core import personas as _personas_mod

    check("预设对话: 默认不注入", prompt.build_preset_dialogues_block(cfg) == "")
    cfg_pd = dict(cfg, preset_dialogues=[
        {"user": "在吗", "reply": "咋了"},
        {"user": "只有对方没回复", "reply": ""},
        {"user": "", "reply": "孤儿回复"},
        "不是字典的脏条目",
    ])
    blk = prompt.build_preset_dialogues_block(cfg_pd)
    check("预设对话: 有效条目渲染", "【预设对话】" in blk and "用户：在吗\n你：咋了" in blk, blk)
    check("预设对话: 缺边/脏条目跳过", "孤儿回复" not in blk and "只有对方" not in blk)
    check("预设对话: 进系统提示词", "【预设对话】" in prompt.build_system_prompt(cfg_pd))
    ov_pd = _personas_mod.overlay(cfg_pd, {"name": "傲娇", "preset_dialogues": [{"user": "哈喽", "reply": "干嘛"}]})
    check("预设对话: 人格覆盖生效",
          prompt.build_preset_dialogues_block(ov_pd).count("用户：") == 1 and "哈喽" in prompt.build_preset_dialogues_block(ov_pd))
    cfgp = dict(cfg, private_chat_prompts="私聊要温柔")
    spp = prompt.build_system_prompt(cfgp, chat_id="u1", platform="qq", is_group=False)
    check("私聊: 注意事项用私聊提示词", "通用注意事项：\n私聊要温柔" in spp and "群里要简短" not in spp)
    fmp = prompt.build_final_user_message(make_state([("u", "hi", False)], pending=1),
                                          dict(cfgp, max_private_context_size=60), "原因", "",
                                          is_group=False)
    check("私聊: 上下文用私聊条数", "当前时间：" in fmp)

    st = make_state([("u", "大家好", False)], pending=1)
    fm = prompt.build_final_user_message(st, dict(cfg, max_context_size=40), "测试触发原因", "简短表达")
    check("final: 当前时间+记录+思考+篇幅", all(s in fm for s in
          ("当前时间：", "【最近群聊记录】", "当前思考：\n测试触发原因")))
    check("final: MaiBot 结尾指令原文", fm.endswith(prompt.REPLY_INSTRUCTION))
    check("final: 无自造防复读块", "你最近说过" not in fm)

    # v6.9.8：reply_style 篇幅指令 = MaiBot 三档原文（且 reference_override 非空也注入）
    fm_short = prompt.build_final_user_message(st, cfg, "原因", "简短表达",
                                               reference_override="当前思考：\nX")
    check("篇幅指令: 简短表达原文",
          "请简短的回复，允许句子残缺，奇怪表达，倒装，省略，符合口语习惯，符合省力随意回复习惯" in fm_short)
    fm_long = prompt.build_final_user_message(st, cfg, "原因", "长回复")
    check("篇幅指令: 长回复原文", "可以针对问题做出较为详细的评论和说明" in fm_long)
    fm_normal = prompt.build_final_user_message(st, cfg, "原因", "正常回复")
    check("篇幅指令: 正常回复不注入", "篇幅" not in fm_normal and "残缺" not in fm_normal)

    # v6.9.9：识图上下文（默认关；开时取最近 N 张，旧→新，去重）
    st_img = GroupState()
    st_img.buffer.append({"name": "u", "sid": "1", "msg_id": "m1", "text": "图1",
                          "ts": 1.0, "images": ["http://x/1.jpg"]})
    st_img.buffer.append({"name": "u", "sid": "1", "msg_id": "m2", "text": "图2图3",
                          "ts": 2.0, "images": ["http://x/2.jpg", "http://x/3.jpg"]})
    st_img.buffer.append({"name": "u", "sid": "1", "msg_id": "m3", "text": "重复图1",
                          "ts": 3.0, "images": ["http://x/1.jpg"]})
    check("识图: 默认关不附加", prompt.image_context_parts(st_img, {}) == [])
    cfg_img = {"enable_image_context": True, "image_context_max_num": 3}
    parts = prompt.image_context_parts(st_img, cfg_img)
    urls = [p.image_url.url for p in parts]
    # 去重按最新出现计：图1 在 m3 重发 → 排最后（最旧出现被最新出现取代）
    check("识图: 最近 3 张旧→新去重",
          urls == ["http://x/2.jpg", "http://x/3.jpg", "http://x/1.jpg"], str(urls))
    parts2 = prompt.image_context_parts(st_img, dict(cfg_img, image_context_max_num=1))
    check("识图: 上限 1 取最新出现",
          [p.image_url.url for p in parts2] == ["http://x/1.jpg"])
    check("识图: 部件类型为 image_url", all(p.type == "image_url" for p in parts))

    st_dup = make_state([("u", "在吗", False)], pending=1)
    st_dup.record_self_reply("m0", ["第一句"], "麦麦")
    st_dup.buffer.append({"name": "u", "sid": "u", "msg_id": "m0", "text": "在吗",
                          "at_bot": False, "reply_bot": False, "ts": time.time()})
    fm_dup = prompt.build_final_user_message(st_dup, dict(cfg, max_context_size=40), "原因")
    check("final: 同目标防重复= MaiBot 模板原文",
          fm_dup.startswith("当前时间：")
          and "你刚刚已经回复过这条消息，你刚刚的发言是：“第一句”" in fm_dup
          and "注意请不要和之前你的发言重复" in fm_dup)


def test_states():
    print("[会话状态]")

    # M2 收敛：会话键单一真相——群=群号，私聊=发送者，均空回退 umo
    from astrbot_plugin_maisoul.core.states import session_key

    class _Ev:
        def __init__(self, g, s, u):
            self._g, self._s, self._u = g, s, u
        def get_group_id(self): return self._g
        def get_sender_id(self): return self._s
        @property
        def unified_msg_origin(self): return self._u
    check("M2 会话键: 群聊=群号", session_key(_Ev("103", "42", "umo:g")) == "103")
    check("M2 会话键: 私聊=发送者", session_key(_Ev("", "42", "umo:p:42")) == "42")
    check("M2 会话键: 双空回退 umo", session_key(_Ev("", "", "umo:p:x")) == "umo:p:x")

    # v6.9.13：任务级模型绑定（对齐 model_task_config 的多模型+策略）
    import random as _rnd
    from astrbot_plugin_maisoul.core import modelbind
    cfgm = {"task_models": [{"task": "planner",
        "models": [{"provider": "p1", "model": "m1"},
                   {"provider": "p2", "model": "m2"},
                   {"provider": "", "model": "m3"},   # 无效：缺 provider
                   "junk"],                            # 无效：非 dict
        "strategy": "sequential"}]}
    cands = modelbind.task_model_candidates(cfgm, "planner")
    check("绑定: 无效候选过滤", len(cands) == 2
          and cands[0] == {"provider": "p1", "model": "m1"})
    check("绑定: 未配置任务为空", modelbind.task_model_candidates(cfgm, "replyer") == [])
    check("绑定: 默认策略 sequential", modelbind.task_model_strategy(cfgm, "planner") == "sequential")
    check("绑定: 非法策略回退 sequential",
          modelbind.task_model_strategy({"task_models": [{"task": "planner",
                                                           "strategy": "xxx"}]},
                                        "planner") == "sequential")
    norm = modelbind.normalize_task_models([{"task": "planner", "models": ["junk"],
                                             "strategy": "random"}])
    check("绑定: normalize 补齐五任务且清洗无效项",
          len(norm) == 5 and norm[0]["models"] == [] and norm[0]["strategy"] == "random"
          and norm[1]["task"] == "replyer", str(norm[:2]))
    chain = modelbind.build_model_chain(cands, "sequential", {}, "planner")
    check("策略: sequential 链按列表顺序",
          [c["model"] for c in chain] == ["m1", "m2"])
    rr = {}
    chains = [modelbind.build_model_chain(cands, "balance", rr, "planner")[0]["model"]
              for _ in range(4)]
    check("策略: balance 轮转", chains == ["m1", "m2", "m1", "m2"], str(chains))
    _rnd.seed(7)
    chain_r = modelbind.build_model_chain(cands, "random", {}, "planner")
    check("策略: random 主候选在列表内且降级链含全部",
          len(chain_r) == 2 and {c["model"] for c in chain_r} == {"m1", "m2"})
    pick = modelbind.pick_model(cands, "sequential", {}, "planner")
    check("pick: 主候选", pick["model"] == "m1")
    check("pick: 空候选返回 None", modelbind.pick_model([], "random", {}, "x") is None)

    sm = StateManager()
    st = sm.get("g1")
    st.record_external({"name": "u", "sid": "1", "msg_id": "m1", "text": "hi",
                        "at_bot": False, "reply_bot": False, "ts": time.time()})
    check("记录后积压=1", st.pending_since_fire == 1)
    st.mark_fire("m1")
    check("触发后积压清零+防重复", st.pending_since_fire == 0 and st.recently_replied("m1"))
    check("防重复: 其他消息不命中", not st.recently_replied("mX"))
    st.record_self_reply("m2", ["段落一", "段落二"], "麦麦")
    check("自发回写: 缓冲存全文与防复读",
          st.buffer[-1]["text"] == "段落一\n段落二" and "段落二" in st.last_replies)
    st.record_self_reply("m3", ["引用回复"], "麦麦", quote="m1")
    check("自发回写: quote 目标进记录", st.buffer[-1].get("quote") == "m1")
    check("状态输出字段", set(sm.status_all()["g1"]) == {"buffer", "pending", "recent_self", "last_fire_ago", "persona"})

    # 间隔样本统计四规则（对齐 runtime：30min 窗 / <5s 连发不采样 / 均值下限 30s / 回退 30s）
    from collections import deque as _deq
    now = time.time()

    def mk_intervals(ts_list, last_ts=None):
        s = GroupState()
        s.ext_intervals = _deq(ts_list, maxlen=360)
        s.last_ext_ts = last_ts if last_ts is not None else (ts_list[-1] if ts_list else 0.0)
        return s

    s = mk_intervals([now - 120, now - 60, now])
    check("间隔: 正常 60s 间隔 → 均值 60", abs(s.avg_external_interval() - 60.0) < 0.01,
          str(s.avg_external_interval()))
    s = mk_intervals([now - 8, now - 4, now])
    check("间隔: <5s 连发不采样 → 回退 30s", abs(s.avg_external_interval() - 30.0) < 0.01,
          str(s.avg_external_interval()))
    s = mk_intervals([now - 80, now - 70, now - 60])
    check("间隔: 10s 间隔 → 均值取下限 30s", abs(s.avg_external_interval() - 30.0) < 0.01,
          str(s.avg_external_interval()))
    s = mk_intervals([now - 2400, now - 100, now - 40])
    check("间隔: 30min 窗外样本剔除（只算窗内 60s 间隔）",
          abs(s.avg_external_interval() - 60.0) < 0.01, str(s.avg_external_interval()))
    s = GroupState()
    check("间隔: 从未见过外部消息 → None", s.avg_external_interval() is None)
    s = mk_intervals([])
    s.last_ext_ts = now - 60
    check("间隔: 见过消息但无样本 → 回退 30s",
          abs(s.avg_external_interval() - 30.0) < 0.01)

    # 引用回复链构造（v6.9.5：首段 Reply(目标消息id) + Plain 正文）
    from astrbot.api.message_components import Plain as _Plain, Reply as _Reply
    from astrbot.api.event import MessageChain as _MC
    mc = _MC([_Reply(id="m9")]).message("你好")
    check("引用链: 首组件为 Reply 且带目标 id",
          isinstance(mc.chain[0], _Reply) and str(mc.chain[0].id) == "m9")
    check("引用链: 正文为第二组件",
          isinstance(mc.chain[1], _Plain) and mc.chain[1].text == "你好")
    check("引用链: toDict 为 OneBot reply 段",
          mc.chain[0].toDict() == {"type": "reply", "data": {"id": "m9"}})

    # 防重复提醒字典随 replied_targets(deque 30)对齐裁剪（v6.15.4，防无界增长）
    st2 = GroupState()
    for i in range(40):
        st2.record_self_reply(f"t{i}", [f"回复{i}"], "麦麦")
    alive = {mid for mid, _ in st2.replied_targets if mid}
    check("防重复字典: 随 deque 裁剪",
          set(st2.reply_by_target) == alive and len(alive) <= 30,
          f"dict={len(st2.reply_by_target)} deque={len(alive)}")
    check("防重复字典: 最近条目保留", "t39" in st2.reply_by_target)


class _FakeTool:
    def __init__(self, name):
        self.name = name


class _FakeMgr:
    def __init__(self, names):
        self._tools = {n: _FakeTool(n) for n in names}

    def get_func(self, name):
        return self._tools.get(name)


def test_bridge_toolset():
    print("[工具暴露策略]")
    if _HAS_REAL_ASTRBOT:  # 框架模块导入契约只在真实环境检查（CI 离线跳过）
        from astrbot.core.agent.tool import ToolSet  # noqa: F401  确认可导入
    from astrbot_plugin_maisoul.core import bridge

    class _Ctx:
        def get_llm_tool_manager(self):
            return _FakeMgr(["send_meme", "search_meme", "steal_meme", "query_favor", "call_maid"])

    cfg = {"chat_tools": ["send_meme"], "maid_bridge": True}
    ts = bridge.build_chat_toolset(_Ctx(), cfg)
    names = sorted(t.name for t in ts.tools)
    check("等价物(含前置依赖)+call_maid 暴露，其余隐藏",
          names == ["call_maid", "search_meme", "send_meme"], str(names))
    check("两步制依赖补全：send_meme 自动带上 search_meme",
          "search_meme" in names, str(names))

    cfg = {"chat_tools": [], "maid_bridge": False}
    check("全关时无工具集", bridge.build_chat_toolset(_Ctx(), cfg) is None)

    cfg = {"chat_tools": ["send_meme", "query_favor"], "maid_bridge": True}
    ts = bridge.build_chat_toolset(_Ctx(), cfg)
    names = sorted(t.name for t in ts.tools)
    check("显式加入的等价物生效", "query_favor" in names and "send_meme" in names, str(names))


def test_deferred_pool_dependencies():
    print("[deferred 工具池：内置等价物不入池 + 依赖补全]")
    from astrbot_plugin_maisoul.core import bridge

    class _Ctx:
        def get_llm_tool_manager(self):
            return _FakeMgr(["send_meme", "search_meme", "steal_meme", "call_maid"])

    # planner deferred 池：send_meme/search_meme 由内置 send_emoji 覆盖，不入池（防一轮双发）
    cfg = {"chat_tools": ["send_meme"], "maid_bridge": True}
    names = [t["name"] for t in bridge.list_deferred_tools(_Ctx(), cfg)]
    check("planner 池排除内置等价物（send_meme/search_meme）",
          names == ["call_maid"], str(names))

    # 独立模式 chat_toolset：两步制对仍然完整暴露（依赖补全生效）
    ts = bridge.build_chat_toolset(_Ctx(), cfg)
    tnames = sorted(t.name for t in ts.tools)
    check("独立模式仍暴露 search+send 对",
          tnames == ["call_maid", "search_meme", "send_meme"], str(tnames))

    cfg = {"chat_tools": ["call_maid"], "maid_bridge": False}
    names = [t["name"] for t in bridge.list_deferred_tools(_Ctx(), cfg)]
    check("无依赖的工具不受影响", names == ["call_maid"], str(names))

def test_tool_skill_registry():
    print("[AstrBot 工具/技能注册表]")
    from pathlib import Path
    from types import SimpleNamespace

    from astrbot.core.agent.tool import FunctionTool
    from astrbot.core.star import star_map
    from astrbot_plugin_maisoul.core import bridge

    async def _noop():
        return ""

    t1 = FunctionTool(name="call_maid", description="管家", parameters={"type": "object", "properties": {}}, handler=_noop, handler_module_path="x.y")
    t2 = FunctionTool(name="send_meme", description="表情包", parameters={"type": "object", "properties": {}}, handler=_noop)

    class _Mgr:
        func_list = [t1, t2]

        def iter_builtin_tools(self):
            return []

        def is_builtin_tool(self, name):
            return False

    class _Ctx:
        def get_llm_tool_manager(self):
            return _Mgr()

    tools = bridge.list_astrbot_tools(_Ctx())
    by_name = {t["name"]: t for t in tools}
    check("工具: func_list 全量列出", set(by_name) == {"call_maid", "send_meme"}, str(sorted(by_name)))
    check("工具: 序列化字段对齐官方 get_tool_list",
          all(set(t) == {"name", "description", "active", "origin", "origin_name"} for t in tools))

    star_map["x.y"] = SimpleNamespace(name="maid_agent")
    try:
        tools = bridge.list_astrbot_tools(_Ctx())
        by_name = {t["name"]: t for t in tools}
        check("工具: handler_module_path 命中 star_map → plugin 来源",
              by_name["call_maid"]["origin"] == "plugin" and by_name["call_maid"]["origin_name"] == "maid_agent",
              str(by_name.get("call_maid")))
    finally:
        star_map.pop("x.y", None)

    bt = FunctionTool(name="send_meme", description="核心", parameters={"type": "object", "properties": {}}, handler=_noop)  # 与插件工具同名的 builtin

    class _MgrBuiltin(_Mgr):
        def iter_builtin_tools(self):
            return [bt]

        def is_builtin_tool(self, name):
            return name == "send_meme"

    class _CtxBuiltin:
        def get_llm_tool_manager(self):
            return _MgrBuiltin()

    tools = bridge.list_astrbot_tools(_CtxBuiltin())
    dup = [t for t in tools if t["name"] == "send_meme"]
    check("工具: builtin 同名去重（对齐官方逻辑）", len(dup) == 1 and dup[0]["origin"] == "builtin", str(dup))

    # 技能：真实 SkillManager round-trip（在 AstrBot 技能根目录临时建一个技能再清理）
    # ——框架集成测试，仅真实环境运行（CI 离线跳过）
    if _HAS_REAL_ASTRBOT:
        from astrbot.core.skills.skill_manager import SkillManager
        from astrbot.core.utils.astrbot_path import get_astrbot_skills_path

        sdir = Path(get_astrbot_skills_path()) / "maisoul_test_skill"
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / "SKILL.md").write_text(
            "---\nname: maisoul_test_skill\ndescription: 测试技能\n---\n# 测试\n",
            encoding="utf-8")
        try:
            skills = {s["name"]: s for s in bridge.list_astrbot_skills()}
            check("技能: 读取 SKILL.md frontmatter 描述",
                  skills.get("maisoul_test_skill", {}).get("description") == "测试技能")
            blk = bridge.build_skills_block({"chat_skills": ["maisoul_test_skill"]})
            check("技能块: 原生 build_skills_prompt 注入",
                  blk.startswith("\n## Skills") and "maisoul_test_skill" in blk and "SKILL.md" in blk)
            check("技能块: 未选/选了不存在 → 空",
                  bridge.build_skills_block({}) == ""
                  and bridge.build_skills_block({"chat_skills": ["不存在的技能"]}) == "")
        finally:
            SkillManager().delete_skill("maisoul_test_skill")


def test_tool_exec_official_path():
    # 官方 FunctionToolExecutor 执行路径（坑 10 回归）——框架集成测试，
    # 仅真实环境运行（CI 离线跳过）
    if not _HAS_REAL_ASTRBOT:
        print("[工具执行官方路径] 跳过（无 AstrBot 运行时）")
        return
    print("[工具执行官方路径]")
    import asyncio as _aio

    from astrbot.core.agent.tool import FunctionTool
    from astrbot_plugin_maisoul.core import bridge

    async def _handler(event, x=""):
        return f"ok:{x}:{event.unified_msg_origin}"

    async def _gen_handler(event, ys=None):
        for y in (ys or []):
            yield y

    tool = FunctionTool(name="demo_tool", description="演示",
                        parameters={"type": "object", "properties": {}},
                        handler=_handler)
    ev = bridge.SyntheticEvent("webchat!u!1")
    out = _aio.run(bridge.call_llm_tool(None, ev, tool, {"x": "1"}))
    check("call_llm_tool: 装饰器 handler 路径（原生 agent 同路）",
          out == "ok:1:webchat!u!1", out)

    gen_tool = FunctionTool(name="demo_gen", description="生成器演示",
                            parameters={"type": "object", "properties": {}},
                            handler=_gen_handler)
    out = _aio.run(bridge.call_llm_tool(None, ev, gen_tool, {"ys": ["a", "b"]}))
    check("call_llm_tool: 异步生成器 handler 多段结果", out == "a\nb", out)
    check("SyntheticEvent: get_extra 默认空", ev.get_extra("k") is None)


def test_monitor():
    print("[麦麦观察]")
    import pathlib
    import tempfile

    from astrbot_plugin_maisoul.core import monitor as M

    tmp = pathlib.Path(tempfile.mkdtemp()) / "m.db"
    store = M.MonitorStore(path=tmp)
    mon = M.Monitor(store)
    check("常量: 保留策略对齐 MaiBot event_store",
          (M.MAX_MONITOR_EVENT_RECORDS, M.MAX_MONITOR_EVENT_AGE_HOURS,
           M.DEFAULT_REPLAY_LIMIT, M.CLEANUP_CHECK_INTERVAL_RECORDS)
          == (10000, 72, 1000, 200))
    check("常量: 非持久化事件集合",
          M.NON_PERSISTED_EVENTS == {"stage.status", "stage.removed", "stage.snapshot"})

    mon.emit_session_start("g1", "群 g1", is_group_chat=True, group_id="g1",
                           user_id=None, platform="qq")
    mon.emit_message_ingested("g1", "小明", "早上好", "m1", 111.0,
                              platform="qq", user_id="u1", group_id="g1")
    mon.emit_stage_status(session_id="g1", session_name="群 g1", stage=M.STAGE_PLANNER,
                          detail="组织上下文并请求模型", round_text="第 1 轮",
                          agent_state="running")
    mon.emit_message_sent("g1", "麦麦", "早呀", "m1", 112.0, "reply",
                          platform="qq", group_id="g1")
    mon.emit_planner_finalized(
        session_id="g1", cycle_id=1,
        planner_request_messages=[{"role": "user", "content": "历史"}],
        planner_selected_history_count=3, planner_tool_count=4,
        planner_content="思考过程", planner_tool_calls=[
            {"id": "1-0-0", "name": "reply", "arguments": {"msg_id": "m1"}}],
        planner_prompt_tokens=1200, planner_completion_tokens=350,
        planner_total_tokens=1550,
        planner_duration_ms=1234.5,
        tools=[{"tool_call_id": "1-0-0", "tool_name": "reply", "tool_args": {},
                "tool_call_source": "planner", "tool_call_source_label": "",
                "success": True, "duration_ms": 500.0, "summary": "已发送 2 段"}],
        agent_state="idle", end_reason="reply", end_detail="已发送 2 段",
        eco_injection="【好感度档案】测试注入块")

    events = store.replay(limit=50)
    fin = next(e for e in events if e["event"] == "planner.finalized")
    check("finalized: final_state.eco_injection 带入生态注入全文",
          (fin["data"].get("final_state") or {}).get("eco_injection") == "【好感度档案】测试注入块",
          str((fin["data"].get("final_state") or {}).get("eco_injection"))[:60])
    kinds = [e["event"] for e in events]
    check("事件名: 全部对齐 MaiBot（stage.* 不落账本）",
          kinds == ["session.start", "message.ingested", "message.sent", "planner.finalized"],
          str(kinds))
    ing = events[1]["data"]
    check("ingested: 字段对齐 emit_message_ingested",
          ing["speaker_name"] == "小明" and ing["platform"] == "qq"
          and ing["reply_to"] is None and ing["media"] == [] and "event_id" in ing)
    fin = events[3]["data"]
    check("finalized: 嵌套结构对齐（request/planner/tools/final_state）",
          fin["request"]["selected_history_count"] == 3
          and fin["planner"]["tool_calls"][0]["name"] == "reply"
          and fin["tools"][0]["success"] is True
          and fin["final_state"]["end_reason"] == "reply"
          and fin["interrupted"] is False, str(fin.keys()))
    check("finalized: token 字段对齐 MaiBot（planner.prompt/completion/total_tokens）",
          fin["planner"]["prompt_tokens"] == 1200
          and fin["planner"]["completion_tokens"] == 350
          and fin["planner"]["total_tokens"] == 1550, str(fin["planner"].keys()))
    check("finalized: native_tool_calls 无信号不编造",
          "native_tool_calls" not in fin["planner"])

    since = events[1]["data"]["event_id"]
    tail = store.replay(since_event_id=since, limit=10)
    check("replay: since_event_id 增量重放有序",
          [e["data"]["event_id"] for e in tail] == [since + 1, since + 2], str(tail[:1]))

    mon.emit_message_ingested("g1", "小明", "带图", "m2", 113.0,
                              media=[{"kind": "image", "hash": "h1", "data_url": "data:image/png;base64,AAAA"}])
    raw = json.dumps(store.replay(limit=1)[0]["data"], ensure_ascii=False)
    check("sanitize: data_url 剔除不入账本（对齐 MaiBot）", "data_url" not in raw and "AAAA" not in raw)

    # SQL 账本（写法照抄 MaiBot event_store.py）：持久化重开、超限裁剪
    big_path = pathlib.Path(tempfile.mkdtemp()) / "b.db"
    big = M.MonitorStore(path=big_path)
    with big._session_factory() as sess:
        for i in range(50):
            sess.add(M.MaisakaMonitorEventRecord(
                event_type="message.ingested", session_id="g", timestamp=1.0,
                schema_version=1, payload_json="{}"))
        sess.commit()
    origin_cap = M.MAX_MONITOR_EVENT_RECORDS
    M.MAX_MONITOR_EVENT_RECORDS = 10  # 临时调低上限验证裁剪逻辑
    try:
        removed = big.cleanup()
    finally:
        M.MAX_MONITOR_EVENT_RECORDS = origin_cap
    left = big.replay(limit=100)
    check("cleanup: SQL 超限裁剪最旧（对齐两条 DELETE 语句）",
          removed == 40 and len(left) == 10
          and left[0]["data"]["event_id"] > 40,
          f"removed={removed}")
    reopened = M.MonitorStore(path=big_path)
    check("SQL: 重开库数据仍在（持久化）", len(reopened.replay(limit=100)) == 10)
    check("SQL: 表名/列对齐 maisaka_monitor_events",
          M.MaisakaMonitorEventRecord.__tablename__ == "maisaka_monitor_events"
          and {c for c in M.MaisakaMonitorEventRecord.__fields__
               if c in {"event_id", "event_type", "session_id", "timestamp",
                        "schema_version", "payload_json", "created_at"}}
          == {"event_id", "event_type", "session_id", "timestamp",
              "schema_version", "payload_json", "created_at"})

    # 事件集对齐 MaiBot events.py 的 9 个 emit：timing_gate.result / planner.response /
    # replier.response 是部署前端的遗留渲染类型，后端不发，maisoul 也不发（禁止编造）
    check("事件集: 不存在三个前端遗留类型的发射方法",
          not any(hasattr(mon, m) for m in
                  ("emit_timing_gate", "emit_planner_response", "emit_replier_response")))

    # 推送：订阅队列收到广播（SSE 端点的数据源）
    q = mon.bus.subscribe()
    mon.emit_message_ingested("g1", "小明", "推我", "m3", 114.0)
    import asyncio as _a
    item = _a.run(q.get())
    check("bus: 订阅者收到实时事件", item["event"] == "message.ingested"
          and item["data"]["content"] == "推我")
    mon.bus.unsubscribe(q)


def test_learning():
    print("[学习子系统]")
    import pathlib
    import tempfile
    import random
    from astrbot_plugin_maisoul.core import learning

    tmp = pathlib.Path(tempfile.mkdtemp()) / "t.json"
    store = learning.LearningStore(path=tmp)
    check("表达: 新增", store.add_expression("global", "惊叹", "使用 我嘞个", True))
    check("表达: 重复计数不重复入库",
          not store.add_expression("global", "惊叹", "使用 我嘞个", True)
          and store.expressions("global")[0]["count"] == 2)
    check("表达块: 库<10 不注入", learning.expression_habits_block(store, "global", False) == "")
    for i in range(12):
        store.add_expression("global", f"情境{i}", f"风格{i}", i % 2 == 0)
    random.seed(3)
    blk = learning.expression_habits_block(store, "global", False)
    check("表达块: 满库注入格式", blk.startswith("【表达习惯参考，请视情况自然的使用】")
          and "时，可以用" in blk and "来表达。" in blk, blk[:60])
    check("表达块: 条数≤5", blk.count("\n") <= 5)

    tmp2 = pathlib.Path(tempfile.mkdtemp()) / "u.json"
    store2 = learning.LearningStore(path=tmp2)
    for i in range(12):
        store2.add_expression("global", f"情境{i}", f"风格{i}", False)
    check("表达块: 精选过滤后为空", learning.expression_habits_block(store2, "global", True) == "")

    store.add_jargon("global", "yyds", "永远的神")
    blk = learning.jargon_reference_block(store, "global", ["这波太yyds了", "哈哈"])
    check("黑话块: 命中注入", blk.startswith("以下黑话来自当前上下文") and "1. yyds：永远的神" in blk, blk)
    check("黑话块: 未命中为空", learning.jargon_reference_block(store, "global", ["普通消息"]) == "")

    # 黑话去重钩子（planner 轮间，对齐 jargon_context_matcher 历史去重）
    got: list[str] = []
    blk2 = learning.jargon_reference_block(store, "global", ["这波太yyds了"],
                                           matched_out=got)
    check("黑话钩子: matched_out 回填命中词条", got == ["yyds"] and "yyds" in blk2)
    check("黑话钩子: exclude 跳过已注入词条",
          learning.jargon_reference_block(store, "global", ["这波太yyds了"],
                                          exclude={"yyds"}) == "")

    # v6.9.8：黑话排序近似（count 降序 + 首现位置优先）
    tmp3 = pathlib.Path(tempfile.mkdtemp()) / "j.json"
    store3 = learning.LearningStore(path=tmp3)
    store3.add_jargon("g", "低频词", "含义")           # count=1，首现位置 0
    store3.add_jargon("g", "高频词", "含义")
    store3.add_jargon("g", "高频词", "含义")           # count=2
    store3.add_jargon("g", "同高频", "含义")
    store3.add_jargon("g", "同高频", "含义")           # count=2，首现位置更晚
    blk3 = learning.jargon_reference_block(store3, "g", ["高频词 同高频 低频词"])
    order = [line.split(". ", 1)[1].split("：")[0] for line in blk3.splitlines()[1:]]
    check("黑话排序: count 降序 + 首现优先",
          order == ["高频词", "同高频", "低频词"], str(order))

    # 防复读与提示词清理（v6.9.6）
    from astrbot_plugin_maisoul.core import planner as _pl
    from difflib import SequenceMatcher as _SM
    check("防复读: 相同文本相似度>0.9", _SM(None, "分析A", "分析A").ratio() > 0.9)
    check("防复读: 不同文本相似度<0.9",
          _SM(None, "群友在聊新出的游戏", "今天天气不错适合睡觉").ratio() < 0.9)
    check("防复读: 反思文本常量存在",
          _pl.PLANNER_REFLECT_ON_REPEAT.startswith("我应该根据我上面思考的内容进行反思"))
    sys_prompt = _pl.build_planner_system(
        {"bot_name": "麦麦", "behavior_style": "测试", "personality": ""}, "")
    check("提示词: tool_search 指引已恢复（v6.9.7 工具已实装）", "tool_search" in sys_prompt)
    check("提示词: view_forward_message 仍不提及（无此工具）",
          "view_forward_message" not in sys_prompt)
    check("提示词: deferred tools 提示已恢复", "deferred tools" in sys_prompt)
    check("提示词: reply/wait 指引保留",
          "调用reply" in sys_prompt and "wait()" in sys_prompt)
    check("PlannerState: last_analysis 字段存在", hasattr(_pl.PlannerState(), "last_analysis"))

    cfg = {"keyword_rules": [{"keywords": ["早上好"], "reaction": "热情打招呼"}],
           "regex_rules": [{"regex": [r"(?P<food>吃\w+)"], "reaction": "聊聊 [food]"}]}
    blk = learning.keyword_reaction_block(cfg, "大家早上好呀")
    check("关键词反应: 命中格式", blk.startswith("【关键词反应】") and "- 热情打招呼" in blk)
    blk = learning.keyword_reaction_block(cfg, "今天吃火锅吗")
    check("正则反应: 命名捕获组替换", "聊聊 吃火锅" in blk, blk)
    check("关键词反应: 未命中为空", learning.keyword_reaction_block(cfg, "普通消息") == "")

    cfgf = {"expression_learning_list": [
        {"platform": "", "item_id": "", "type": "group", "use": True, "learn": True},
        {"platform": "qq", "item_id": "g1", "type": "group", "use": False, "learn": False}]}
    check("学习配置: 精确命中关闭",
          learning.learning_flags(cfgf, "expression_learning_list", "qq", "g1") == (False, False))
    check("学习配置: 其他群回落默认",
          learning.learning_flags(cfgf, "expression_learning_list", "qq", "g2") == (True, True))
    cfgp = {"expression_learning_list": [
        {"platform": "", "item_id": "", "type": "group", "use": True, "learn": True},
        {"platform": "qq", "item_id": "u1", "type": "private", "use": False, "learn": True}]}
    check("学习配置: 私聊规则按类型命中",
          learning.learning_flags(cfgp, "expression_learning_list", "qq", "u1", False) == (False, True))
    check("学习配置: 私聊回落 group 默认规则",
          learning.learning_flags(cfgp, "expression_learning_list", "qq", "u2", False) == (True, True))
    cfgg = {"expression_groups": [{"targets": [
        {"platform": "qq", "item_id": "g1"}, {"platform": "qq", "item_id": "g2"}]}]}
    k1 = learning.share_key(cfgg, "expression_groups", "qq", "g1")
    k2 = learning.share_key(cfgg, "expression_groups", "qq", "g2")
    check("共享组: 组内同键且非 global", k1 == k2 and k1 != "global")
    check("共享组: 组外 global", learning.share_key(cfgg, "expression_groups", "qq", "g9") == "global")

    check("JSON 修复: 前后杂讯", learning._repair_json_array('好的：\n[{"a":1}] 完成') == [{"a": 1}])
    check("JSON 修复: 无数组", learning._repair_json_array("没有内容") == [])

    # M1 回归：自检 suitable 判定必须按结构提取——旧实现是
    # '"suitable": true' in raw.replace(" ","")（针含空格、干草堆已去空格，
    # 恒 False）+ 紧凑字符串兜底，模型常规输出 {"suitable": true} 时
    # checked=False，默认 expression_checked_only=true 下学到的表达全部不可用
    check("M1 suitable: 常规带空格 JSON 判真",
          learning._suitable_from_review('{"suitable": true, "reason": "自然口语"}') is True)
    check("M1 suitable: 紧凑 JSON 判真", learning._suitable_from_review('{"suitable":true}') is True)
    check("M1 suitable: false 判假", learning._suitable_from_review('{"suitable": false}') is False)
    check("M1 suitable: 前后杂讯容忍",
          learning._suitable_from_review('评估结果：\n{"suitable": true}\n以上。') is True)
    check("M1 suitable: 无法解析保守为假", learning._suitable_from_review('这条表达没问题') is False)
    # 端到端口径：checked=True 的条目在 expression_checked_only=True 时可用
    # （注入需过滤后池 ≥10 条，故放 12 条过检 + 6 条未过检）
    tmpm = pathlib.Path(tempfile.mkdtemp()) / "m.json"
    storem = learning.LearningStore(path=tmpm)
    for i in range(12):
        storem.add_expression("global", f"可用品{i}", f"风格{i}", True)
    for i in range(6):
        storem.add_expression("global", f"废品{i}", f"风格x{i}", False)
    random.seed(7)
    blkm = learning.expression_habits_block(storem, "global", True)
    check("M1 端到端: 自检通过的表达可用", "可用品" in blkm and "废品" not in blkm, blkm[:60])

    buf = [{"name": "麦麦", "sid": "b", "msg_id": f"s{i}", "text": f"自言{i}",
            "at_bot": False, "reply_bot": False, "ts": i} for i in range(5)]
    buf.append({"name": "u", "sid": "u", "msg_id": "m", "text": "用户",
                "at_bot": False, "reply_bot": False, "ts": 9})
    opt = prompt._optimize_transcript(buf, "麦麦", 3)
    check("优化上下文: 自发言保留最近3条",
          sum(1 for m in opt if m["name"] == "麦麦") == 3 and opt[-1]["text"] == "用户")

    st = make_state([("u", "大家好", False)], pending=1)
    fm = prompt.build_final_user_message(
        st, {"max_context_size": 40, "bot_name": "麦麦"}, "原因", "",
        expression_habits="【表达习惯参考，请视情况自然的使用】\n- 当\"X\"时，可以用\"Y\"来表达。",
        jargon_reference="以下黑话来自当前上下文……\n1. yyds：永远的神",
        keyword_reaction="【关键词反应】\n最新消息命中了预设反应规则，请在回复时优先参考以下要求：\n- 热情打招呼\n")
    idx_rec = fm.find("【最近群聊记录】")
    idx_expr = fm.find("【表达习惯参考")
    idx_jar = fm.find("以下黑话来自")
    idx_ref = fm.find("【回复信息参考】")
    idx_kwr = fm.find("【关键词反应】")
    idx_end = fm.find(prompt.REPLY_INSTRUCTION)
    check("final: 块顺序对齐 MaiBot",
          0 < idx_rec < idx_expr < idx_jar < idx_ref < idx_kwr < idx_end, fm)

    # 学习库损坏防护（v6.15.4）：坏 JSON / 非 dict 结构 → 备份 .corrupt 后空库启动
    bad = pathlib.Path(tempfile.mkdtemp()) / "bad.json"
    bad.write_text('{"global": {"expressions": [', encoding="utf-8")  # 半截 JSON
    store_bad = learning.LearningStore(path=bad)
    check("损坏防护: 半截 JSON → 空库启动", store_bad.data == {})
    check("损坏防护: 原文件备份为 .corrupt",
          (bad.parent / "bad.json.corrupt").exists() and not bad.exists())
    bad2 = pathlib.Path(tempfile.mkdtemp()) / "bad2.json"
    bad2.write_text('["不是对象"]', encoding="utf-8")
    check("损坏防护: 非 dict 结构 → 空库启动",
          learning.LearningStore(path=bad2).data == {})
    check("损坏防护: 结构非法同样备份",
          (bad2.parent / "bad2.json.corrupt").exists())
    # 嵌套错型（合法 JSON + 顶层 dict 但分库非对象）同样备份+空库
    # （Sourcery 审查：_bucket() 会返回 list，.get 抛 AttributeError）
    bad3 = pathlib.Path(tempfile.mkdtemp()) / "bad3.json"
    bad3.write_text('{"global": []}', encoding="utf-8")
    check("损坏防护: 分库非对象 → 空库启动",
          learning.LearningStore(path=bad3).data == {})
    check("损坏防护: 嵌套错型同样备份",
          (bad3.parent / "bad3.json.corrupt").exists())

    # 原子写：保存后无 .tmp 残留、落盘内容可回读
    store_bad.add_expression("global", "情境", "风格", True)
    check("原子写: 无 .tmp 残留", not (bad.parent / "bad.json.tmp").exists())
    check("原子写: 落盘可回读",
          len(learning.LearningStore(path=bad).expressions("global")) == 1)

    # WebUI 写接口校验（core/apivalid.py，v6.15.4）
    from astrbot_plugin_maisoul.core import apivalid
    schema = {"talk_value": {"type": "float"}, "enable": {"type": "bool"},
              "aliases": {"type": "list"}, "bot_name": {"type": "string"}}
    cur = {"talk_value": 1.0, "enable": True, "aliases": [], "bot_name": "麦麦"}
    ok, err = apivalid.validate_config_payload(
        schema, {"talk_value": 0.5, "new_key": 1}, cur)
    check("config校验: 合法值通过且新键被白名单挡住",
          ok == {"talk_value": 0.5} and err is None, f"{ok} {err}")
    _, err = apivalid.validate_config_payload(schema, {"talk_value": "abc"}, cur)
    check("config校验: 类型不符拒绝", err is not None and "talk_value" in err, str(err))
    _, err = apivalid.validate_config_payload(schema, {"enable": 1}, cur)
    check("config校验: int 不冒充 bool", err is not None)
    _, err = apivalid.validate_config_payload(schema, {"aliases": "x,y"}, cur)
    check("config校验: list 不收字符串", err is not None)
    _, err = apivalid.validate_config_payload(None, {"talk_value": "abc"}, cur)
    check("config校验: 无 schema 元数据时保持宽松", err is None)
    check("学习库校验: 合法结构通过", apivalid.validate_learning_payload(
        {"global": {"expressions": [{"situation": "s"}], "jargons": []}}) is None)
    check("学习库校验: 顶层非 dict 拒绝",
          apivalid.validate_learning_payload([1]) is not None)
    check("学习库校验: 分库非对象拒绝",
          apivalid.validate_learning_payload({"global": ["x"]}) is not None)
    check("学习库校验: 字段错型拒绝",
          apivalid.validate_learning_payload(
              {"global": {"expressions": "x"}}) is not None)
    big = {"global": {"expressions": [{"situation": "x" * 100}] * 100000}}
    check("学习库校验: 体积超限拒绝",
          apivalid.validate_learning_payload(big) is not None)


def test_taskregistry():
    print("[任务注册表 M6]")
    import asyncio as _aio
    from astrbot_plugin_maisoul.core.taskregistry import TaskRegistry
    from astrbot_plugin_maisoul.core.monitor import MonitorStore, Monitor

    reg = TaskRegistry()

    async def _ok():
        await _aio.sleep(0.01)
        return 7

    async def _hang():
        await _aio.sleep(30)

    async def _scenario():
        # spawn 持强引用 + 完成自动清理
        t_ok = reg.spawn(_ok(), name="ok")
        assert await t_ok == 7
        await _aio.sleep(0)
        check("M6 registry: 完成任务自动移除", reg.size == 0)
        # adopt（defer_task/running_task 句柄另存场景）
        t_hang = reg.adopt(_aio.create_task(_hang()), name="hang")
        check("M6 registry: adopt 登记", reg.size == 1)
        # cancel_and_wait_all：挂起任务被取消且在超时内返回（幂等）
        await reg.cancel_and_wait_all(timeout=2.0)
        check("M6 registry: 取消并等待", t_hang.cancelled() and reg.size == 0)
        await reg.cancel_and_wait_all(timeout=1.0)  # 幂等：再次调用不抛
        return True

    check("M6 registry: 场景", _aio.run(_scenario()))

    # MonitorStore/Monitor.close：释放连接池且幂等
    import pathlib, tempfile
    p = pathlib.Path(tempfile.mkdtemp()) / "m6.db"
    store = MonitorStore(p)
    mon = Monitor(store)
    mon.close()
    mon.close()  # 幂等
    check("M6 monitor: close 幂等释放", True)


def test_planner():
    print("[Planner 决策层]")
    from astrbot_plugin_maisoul.core import planner as P

    # M3 回归：代际号守卫——打断（cancel 旧循环 + 新循环 running）后，旧循环
    # 在 CancelledError/退出路径回写 idle 会清掉新循环状态，后续消息误判
    # idle 再开循环 → 双循环并发。旧代退出不得回写，仅当前代可以。
    plg = P.PlannerState()
    plg.agent_state = "running"
    g1 = plg.begin_cycle()
    plg.set_idle_if_current(g1)
    check("M3 代际: 当前代退出置 idle", plg.agent_state == "idle")
    g2 = plg.begin_cycle()          # 新循环开启（打断场景）
    plg.agent_state = "running"
    plg.set_idle_if_current(g1)     # 被取消的旧循环稍后醒来退出
    check("M3 代际: 旧代退出不得清状态", plg.agent_state == "running")
    plg.set_idle_if_current(g2)
    check("M3 代际: 新代自身退出仍生效", plg.agent_state == "idle")

    # fetch_history 已移除（v6.13.5）：MaiBot focus 模式专属工具，部署版
    # focus_mode=false 不暴露——工具集与请求结构均不得出现
    st = GroupState()
    for i in range(6):
        st.record_external({"name": f"u{i}", "sid": str(i), "msg_id": f"m{i}",
                            "text": f"消息{i}", "at_bot": False, "reply_bot": False,
                            "ts": 100.0 + i})
    class _PluginStub:
        async def _planner_send_emoji(self, deps): return ""
    deps = P.PlannerDeps(_PluginStub(), st, {}, None, "qq", "g1", True)
    pl = st.planner_state()
    if _HAS_REAL_ASTRBOT:  # ToolSet 构造依赖框架（CI 离线跳过）
        tool_names = sorted(t.name for t in P.build_planner_toolset(deps).tools)
        check("工具集: fetch_history 不再暴露（focus 专属）",
              "fetch_history" not in tool_names
              and tool_names == ["reply", "send_emoji", "tool_search", "wait"],
              str(tool_names))
    check("PlannerState: context_msg_ids 已随 fetch 移除",
          not hasattr(pl, "context_msg_ids"))

    sysp = P.build_planner_system(
        {"bot_name": "麦麦", "behavior_style": "大二学生"},
        "在该聊天中的注意事项：\n通用注意事项：\n群里要简短\n")
    check("planner 系统提示词原文",
          sysp.startswith("你的任务是分析聊天和聊天中的互动情况")
          and "麦麦的行为风格：大二学生" in sysp
          and "在该聊天中的注意事项" in sysp
          and "注意，你无法不调用reply工具直接回复，必须通过reply工具来发送回复。" in sysp)
    check("reply 工具枚举=MaiBot",
          P.REPLY_TOOL_SPEC["properties"]["reply_style"]["enum"] == ["简短表达", "正常回复", "长回复"]
          and P.REPLY_TOOL_SPEC["required"] == ["msg_id"])
    check("wait 工具声明=MaiBot", P.WAIT_TOOL_SPEC["required"] == ["seconds"])
    check("MAX_INTERNAL_ROUNDS=10", P.MAX_INTERNAL_ROUNDS == 10)

    # v6.9.7：tool_search + deferred 池（打分表/提醒模板/发现流）
    pool = [
        {"name": "call_maid", "description": "让管家代理执行任务：查资料 搜索 计算", "tool": object()},
        {"name": "search_meme", "description": "按当前语气搜索表情包", "tool": object()},
        {"name": "send_meme", "description": "发送选中的表情包", "tool": object()},
    ]
    hits = P.search_deferred_tools(pool, "call_maid", 5)
    check("tool_search: 精确命中", [h["name"] for h in hits][:1] == ["call_maid"])
    hits = P.search_deferred_tools(pool, "meme", 5)
    check("tool_search: 子串命中多工具且按名排序",
          [h["name"] for h in hits] == ["search_meme", "send_meme"], str([h["name"] for h in hits]))
    hits = P.search_deferred_tools(pool, "管家 查资料", 5)
    check("tool_search: 描述分词命中", [h["name"] for h in hits] == ["call_maid"])
    check("tool_search: 无命中返回空", P.search_deferred_tools(pool, "不存在的东西", 5) == [])
    hits = P.search_deferred_tools(pool, "m", 1)
    check("tool_search: limit 截断", len(hits) == 1)

    reminder = P.build_deferred_reminder(pool, set())
    check("reminder: 模板原文与编号",
          reminder.startswith("<system-reminder>")
          and "以下工具当前未直接暴露给你" in reminder
          and "1. call_maid: 让管家代理执行任务：查资料 搜索 计算" in reminder
          and "tool_search 只负责发现工具，不直接执行。" in reminder
          and reminder.endswith("</system-reminder>"), reminder[:120])
    reminder2 = P.build_deferred_reminder(pool, {"call_maid"})
    check("reminder: 已发现的不列出", "call_maid" not in reminder2
          and "search_meme" in reminder2)
    check("reminder: 全部发现后为空", P.build_deferred_reminder(
        pool, {"call_maid", "search_meme", "send_meme"}) == "")

    deps2 = P.PlannerDeps(_PluginStub(), st, {}, None, "qq", "g1", True)
    deps2.deferred_pool = pool
    r = deps2.on_tool_search({"query": "meme", "limit": 5})
    check("tool_search 流: 命中文本含新发现标记并记入状态",
          "已找到 2 个 deferred tools" in r and "search_meme（本次新发现）" in r
          and "send_meme（本次新发现）" in r
          and st.planner_state().discovered_tools >= {"search_meme", "send_meme"}, r)
    r2 = deps2.on_tool_search({"query": "meme", "limit": 5})
    check("tool_search 流: 二次调用标此前已发现",
          "search_meme（此前已发现）" in r2, r2)
    r3 = deps2.on_tool_search({"query": "zzz", "limit": 5})
    check("tool_search 流: 无命中文案原文",
          r3 == P.TOOL_SEARCH_NO_HIT and "未找到匹配的 deferred tools" in r3)
    check("PlannerState: discovered_tools 字段存在",
          hasattr(P.PlannerState(), "discovered_tools"))

    # v6.9.8：wait 完成回执两版原文
    r_new = P.build_wait_completed_message(10.0, 8.0, True)
    check("wait回执: 有新消息版原文",
          r_new == "等待已结束，实际等待 10.0 秒，原计划等待 8.0 秒，期间收到了新的用户输入。请结合这些新消息继续下一轮思考。", r_new)
    r_timeout = P.build_wait_completed_message(12.5, None, False)
    check("wait回执: 超时版原文且省略原计划段",
          r_timeout == "等待已超时，实际等待 12.5 秒，期间没有收到新的用户输入。请基于现有上下文继续下一轮思考。", r_timeout)

    # v6.9.8：本轮上下文折叠
    ctx = [{"role": "user", "content": f"历史{i}"} for i in range(3)]
    start = len(ctx)
    for r in range(6):  # 6 轮 user/assistant
        ctx.append({"role": "user", "content": f"第{r}轮输入 " + "x" * 100})
        ctx.append({"role": "assistant", "content": f"第{r}轮分析"})
    P.fold_old_turns(ctx, start)
    turn_part = ctx[start:]
    check("折叠: 超过 3 组触发折叠", len(turn_part) <= 7, str(len(turn_part)))
    check("折叠: 前缀原文与摘要行",
          turn_part[0]["content"].startswith("[已折叠的历史工具调用]\n- user: ")
          and "- assistant: 第0轮分析" in turn_part[0]["content"], turn_part[0]["content"][:100])
    check("折叠: 最近 3 组完整保留", turn_part[-1]["content"] == "第5轮分析")
    ctx2 = [{"role": "user", "content": "只有一组"}]
    P.fold_old_turns(ctx2, 0)
    check("折叠: 未超限不动", ctx2 == [{"role": "user", "content": "只有一组"}])

    # v6.13.4/5：历史分析跨轮回灌 + 部署版消息格式（对齐 MaiBot 会话历史——
    # planner 输出写入 _chat_history 后续作为 assistant 轮回灌；聊天消息含自发
    # 消息全部进 user 轮 <message> 前缀，planner_messages.build_planner_prefix 原文）
    _day1 = 1788000000.0  # 固定基准时间戳（同一天内）
    import datetime as _dtm
    _t1 = _dtm.datetime.fromtimestamp(_day1).strftime("%H:%M:%S")
    _t2 = _dtm.datetime.fromtimestamp(_day1 + 10).strftime("%H:%M:%S")
    chat_hist = [
        {"name": "张三", "sid": "u1", "msg_id": "m1", "text": "早", "ts": _day1},
        {"name": "麦麦", "sid": "self", "msg_id": "", "text": "早啊", "ts": _day1 + 10},
    ]
    ana_log = [{"ts": _day1 + 20,
                "text": "当前状态：对方刚打招呼。\n分析：友好回应即可。"}]
    chat_new = [{"name": "张三", "sid": "u1", "msg_id": "m2", "text": "在吗", "ts": _day1 + 30}]
    ctxs, inc = P.build_history_contexts(chat_hist + chat_new, ana_log, 10)
    check("回灌: 交错顺序 user(消息)→user(自发)→assistant(分析)→user",
          [c["role"] for c in ctxs] == ["user", "user", "assistant", "user"],
          str([c["role"] for c in ctxs]))
    check("回灌: 消息前缀 = build_planner_prefix 原文格式",
          ctxs[0]["content"] == f'<message msg_id="m1" time="{_t1}" user="张三" group_card="张三">\n早',
          ctxs[0]["content"])
    check("回灌: 自发消息 user 轮 + is_self_message、无 group_card",
          ctxs[1]["content"] == f'<message msg_id="" time="{_t2}" user="麦麦" is_self_message="true">\n早啊',
          ctxs[1]["content"])
    check("回灌: 分析文本原样进 assistant 轮",
          ctxs[2]["content"] == "当前状态：对方刚打招呼。\n分析：友好回应即可。")
    check("回灌: included_chat 为进入窗口的聊天消息",
          [m.get("msg_id") for m in inc] == ["m1", "", "m2"])
    ctxs_w, inc_w = P.build_history_contexts(chat_hist + chat_new, ana_log, 2)
    check("回灌: 窗口在合并流上截取（聊天+分析一起数）",
          [c["role"] for c in ctxs_w] == ["assistant", "user"]
          and ctxs_w[0]["content"].startswith("当前状态")
          and [m.get("msg_id") for m in inc_w] == ["m2"],
          str([c["role"] for c in ctxs_w]))
    _day2 = _day1 + 86400  # 次日
    ctxs_d, _ = P.build_history_contexts(
        chat_hist, [{"ts": _day2, "text": "新一天的分析"}], 10)
    check("回灌: 跨日插时间行（分析跨日同样触发）",
          any(c["role"] == "user" and c["content"].startswith("时间：")
              for c in ctxs_d) and ctxs_d[-1]["content"] == "新一天的分析",
          str(ctxs_d))
    ctxs_e, inc_e = P.build_history_contexts(
        chat_hist, [{"ts": _day1, "text": ""}, {"ts": _day1, "text": "  "}], 10)
    check("回灌: 空文本分析过滤、同 ts 聊天在前",
          [c["role"] for c in ctxs_e] == ["user", "user"]
          and [m.get("msg_id") for m in inc_e] == ["m1", ""])
    ctxs_none, inc_none = P.build_history_contexts(chat_hist, [], 10)
    check("回灌: 无分析时退化为纯聊天历史",
          [c["role"] for c in ctxs_none] == ["user", "user"]
          and [m.get("msg_id") for m in inc_none] == ["m1", ""])
    # 私聊无 group_card；quote 属性与转义（对齐 build_planner_prefix）
    ctxs_p, _ = P.build_history_contexts(
        [{"name": "张三", "sid": "u1", "msg_id": "m9", "text": "hi",
          "ts": _day1, "quote": "m8"}], [], 10, is_group=False)
    check("回灌: 私聊无 group_card、quote 属性渲染",
          ctxs_p[0]["content"] == f'<message msg_id="m9" quote="m8" time="{_t1}" user="张三">\nhi',
          ctxs_p[0]["content"])
    esc = P.render_planner_message(
        {"name": '张"三&', "sid": "u1", "msg_id": "<m>", "text": "内容",
         "ts": _day1, "quote": "a,b"}, True)
    check("回灌: 属性值 XML 转义与 quote 去重拼接",
          esc.startswith('<message msg_id="&lt;m&gt;" quote="a,b" time="')
          and 'user="张&quot;三&amp;"' in esc and 'group_card="张&quot;三&amp;"' in esc, esc)
    ps_log = P.PlannerState()
    check("PlannerState: analysis_log 默认有界",
          hasattr(ps_log, "analysis_log") and ps_log.analysis_log.maxlen == 200)

    # v6.9.8：过滤词（对齐 check_ban_words/check_ban_regex）
    check("过滤: 子串命中", trigger.hit_ban_filter("这个广告真烦", ["广告"], []))
    check("过滤: 正则命中", trigger.hit_ban_filter("领红包加微信123", [], [r"微信\d+"]))
    check("过滤: 无效正则跳过不炸", not trigger.hit_ban_filter("正常消息", [], ["("]))
    check("过滤: 未命中放行", not trigger.hit_ban_filter("正常消息", ["广告"], [r"微信\d+"]))

    cfg = {"max_consecutive_wait_count": 3}
    ps = P.PlannerState()
    ok, current, maximum = ps.try_enter_wait(cfg, 30)
    check("wait: 进入等待", ok and current == 1 and ps.agent_state == "wait" and ps.in_wait())
    ps.try_enter_wait(cfg, 30)
    ps.try_enter_wait(cfg, 30)
    ok, current, maximum = ps.try_enter_wait(cfg, 30)
    check("wait: 连续上限拒绝", not ok and current == 3 and maximum == 3
          and "休息" in P.WAIT_LIMIT_RESULT.format(maximum=maximum))
    check("wait: 主动触发恢复", ps.resume_from_wait() and ps.agent_state == "idle")
    ps2 = P.PlannerState()
    check("wait: 非等待状态恢复无效", ps2.resume_from_wait() is False)

    bcfg = {"no_action_backoff_base_seconds": 15, "no_action_backoff_cap_seconds": 300,
            "no_action_backoff_start_count": 2, "no_action_backoff_bypass_pending_count": 6}
    ps3 = P.PlannerState()
    ps3.record_idle_cycle(bcfg)
    check("退避: 起点前不延迟", not ps3.should_delay(bcfg, 1))
    ps3.record_idle_cycle(bcfg)
    check("退避: 达到起点开始延迟", ps3.should_delay(bcfg, 1))
    check("退避: 积压条数绕过", not ps3.should_delay(bcfg, 6))
    ps3.reset_backoff()
    check("退避: 非空闲重置", not ps3.should_delay(bcfg, 1))

    check("末尾提醒原文", P.PLANNER_FINAL_USER_REMINDER.format(bot_name="麦麦")
          == "你需要输出对麦麦发言的分析，视情况输出文本内容的分析，思考是否进行工具调用")

    # v6.13.5：注意事项拆分（通用进系统提示词，chat_prompts 命中进尾部消息）
    from astrbot_plugin_maisoul.core import prompt as _pp
    acfg = {"group_chat_prompt": "群里要简短",
            "chat_prompts": [{"platform": "qq", "item_id": "g1",
                              "rule_type": "group", "prompt": "这个群爱聊游戏"}]}
    sys_blk = _pp.build_attention_block(acfg, "g1", "qq", True, include_chat_prompt=False)
    check("注意事项: planner 系统提示词只含通用项",
          sys_blk == "在该聊天中的注意事项：\n通用注意事项：\n群里要简短\n", sys_blk)
    tail = _pp.chat_attention_tail(acfg, "g1", "qq", True)
    check("注意事项: chat_prompts 命中 → 尾部消息原文格式",
          tail == "当前聊天额外注意事项：\n这个群爱聊游戏", tail)
    check("注意事项: 未命中尾部为空",
          _pp.chat_attention_tail(acfg, "gX", "qq", True) == "")
    merged_blk = _pp.build_attention_block(acfg, "g1", "qq", True)
    check("注意事项: replyer 默认合并形态不变",
          "通用注意事项：\n群里要简短" in merged_blk
          and "当前聊天额外注意事项：\n这个群爱聊游戏" in merged_blk)

    # 表达 LLM 选择（expression_select 路径）
    import pathlib
    import tempfile
    from astrbot_plugin_maisoul.core import learning

    class _Prov:
        async def text_chat(self, prompt, session_id=None, **kw):
            class R:
                completion_text = '{"selected_situations": [1]}'
            return R()

    store = learning.LearningStore(path=pathlib.Path(tempfile.mkdtemp()) / "p.json")
    for i in range(12):
        store.add_expression("global", f"情境{i}", f"风格{i}", True)
    blk = asyncio.run(learning.select_expression_habits_block(
        _Prov(), store, "global", False, "- 12:00:00 u: hi", "麦麦"))
    check("表达 LLM 选择: 选中注入", blk.startswith("【表达习惯参考") and blk.count("\n") == 1, blk)

    class _BadProv:
        async def text_chat(self, prompt, session_id=None, **kw):
            raise RuntimeError("boom")

    blk2 = asyncio.run(learning.select_expression_habits_block(
        _BadProv(), store, "global", False, "- 12:00:00 u: hi", "麦麦"))
    check("表达选择失败回落直注入", blk2.startswith("【表达习惯参考") and blk2.count("\n") >= 1)

    check("chat_info 行格式", "- " in learning.build_chat_info(
        [{"name": "u", "text": "hi", "ts": time.time()}]))

    # v6.14.0：vector_intent 表达召回（对齐 _build_expression_candidate_pool 契约）
    check("query 文本: reply_reference 优先（原文格式）",
          learning.build_expression_query_text("推理A", "参考B")
          == "回复信息参考：\n参考B"
          and learning.build_expression_query_text("推理A")
          == "Planner 推理：\n推理A"
          and learning.build_expression_query_text() == "")
    check("embedding 文本: 情景/风格两行原文",
          learning.expression_embedding_text(" 安慰人 ", " 温柔拍拍 ")
          == "情景：安慰人\n风格：温柔拍拍")
    check("余弦: 同向=1 正交=0 维度不符=-1",
          abs(learning._cosine([1, 0], [2, 0]) - 1.0) < 1e-9
          and abs(learning._cosine([1, 0], [0, 1])) < 1e-9
          and learning._cosine([1, 0], [1]) == -1.0)

    class _Emb:
        """假嵌入：'安慰' 类文本 → [1,0]，'编程' 类 → [0,1]，其余 → [1,1]。"""
        def __init__(self):
            self.calls = []
            self.provider_config = {"id": "fake-emb"}

        async def get_embeddings(self, texts):
            self.calls.extend(texts)
            out = []
            for t in texts:
                if "安慰" in t:
                    out.append([1.0, 0.0])
                elif "编程" in t:
                    out.append([0.0, 1.0])
                else:
                    out.append([1.0, 1.0])
            return out

    vstore = learning.LearningStore(path=pathlib.Path(tempfile.mkdtemp()) / "v.json")
    vstore.add_expression("global", "安慰情绪低落的人", "温柔拍拍", True)
    vstore.add_expression("global", "聊到写代码", "吐槽编程", True)
    for i in range(10):
        vstore.add_expression("global", f"日常闲聊{i}", f"日常风格{i}", True)
    emb = _Emb()
    # query 与"安慰"同向 → 召回池应以安慰条目打头；LLM 选择选中第 1 条
    blk3 = asyncio.run(learning.select_expression_habits_block(
        _Prov(), vstore, "global", False, "- 12:00 u: 心情好差", "麦麦",
        mode="vector_intent", embedding=emb, embedding_model="fake-emb",
        query_text="回复信息参考：\n安慰一下对方",
        pool_size=5))
    check("vector 召回: 相似条目进精选并注入",
          blk3.startswith("【表达习惯参考") and "安慰情绪低落的人" in blk3, blk3)
    check("vector 召回: 候选向量缓存在学习库（二次调用不重嵌）",
          isinstance(vstore.data["global"]["expressions"][0].get("emb"), list)
          and vstore.data["global"]["expressions"][0].get("emb_model") == "fake-emb")
    calls_before = len(emb.calls)
    asyncio.run(learning.select_expression_habits_block(
        _Prov(), vstore, "global", False, "- 12:00 u: hi", "麦麦",
        mode="vector_intent", embedding=emb, embedding_model="fake-emb",
        query_text="回复信息参考：\n安慰", pool_size=5))
    check("vector 召回: 缓存命中（仅重嵌 query）",
          len(emb.calls) == calls_before + 1, f"{calls_before} -> {len(emb.calls)}")

    # 回落三态：未配嵌入 / query 空 / 召回异常（维度不一致上抛后吞掉）
    blk4 = asyncio.run(learning.select_expression_habits_block(
        _Prov(), vstore, "global", False, "- 12:00 u: hi", "麦麦",
        mode="vector_intent", embedding=None, query_text="x", pool_size=5))
    check("vector 回落: 未配嵌入模型走随手抽样", blk4.startswith("【表达习惯参考"))
    blk5 = asyncio.run(learning.select_expression_habits_block(
        _Prov(), vstore, "global", False, "- 12:00 u: hi", "麦麦",
        mode="vector_intent", embedding=emb, embedding_model="fake-emb",
        query_text="", pool_size=5))
    check("vector 回落: query 为空走随手抽样", blk5.startswith("【表达习惯参考"))

    class _DimEmb:
        async def get_embeddings(self, texts):
            return [[0.5, 0.5, 0.5] for _ in texts]  # 与缓存候选维度不符

    vstore.data["global"]["expressions"][0]["emb"] = [1.0, 0.0]
    blk6 = asyncio.run(learning.select_expression_habits_block(
        _Prov(), vstore, "global", False, "- 12:00 u: hi", "麦麦",
        mode="vector_intent", embedding=_DimEmb(), embedding_model="fake-emb",
        query_text="回复信息参考：\n测试", pool_size=5))
    check("vector 回落: 维度异常吞掉后走随手抽样",
          blk6.startswith("【表达习惯参考"))


def test_personas():
    print("[多人格]")
    from astrbot_plugin_maisoul.core import personas

    cfg = {"personas": [
        {"name": "傲娇", "bot_name": "小麦黑", "personality": "傲娇人格", "reply_style": "语气冲"},
        {"name": "温柔", "personality": "温柔人格"},
    ], "default_persona": "温柔", "group_persona": [{"chat": "12345", "name": "傲娇"}],
       "follow_persona_switch": True, "bot_name": "麦麦", "personality": "主人格"}

    check("查找命中", (personas.find_persona(cfg, "傲娇") or {}).get("bot_name") == "小麦黑")
    check("查找未命中", personas.find_persona(cfg, "不存在") is None)
    check("名单列举", personas.list_persona_names(cfg) == ["傲娇", "温柔"])
    ov = personas.overlay(cfg, personas.find_persona(cfg, "傲娇"))
    check("覆盖: 人格字段生效", ov["personality"] == "傲娇人格" and ov["bot_name"] == "小麦黑")
    check("覆盖: 未设字段保留主配置", ov.get("behavior_style") is None and ov.get("group_chat_prompt") is None)
    check("群号匹配: 后缀", personas.chat_id_match("98712345", "12345") and personas.chat_id_match("x", "*"))
    check("群号匹配: 不匹配", not personas.chat_id_match("999", "123"))

    class _NoConv:
        async def get_curr_conversation_id(self, umo): return None
    class _Ctx:
        conversation_manager = _NoConv()
    _, name = asyncio.run(personas.resolve_active(_Ctx(), cfg, "98712345", "u"))
    check("解析: 群绑定优先于默认", name == "傲娇")
    _, name = asyncio.run(personas.resolve_active(_Ctx(), cfg, "999", "u"))
    check("解析: 无绑定落默认人格", name == "温柔")

    class _Conv:
        persona_id = "傲娇"
    class _ConvMgr:
        async def get_curr_conversation_id(self, umo): return "c1"
        async def get_conversation(self, umo, cid): return _Conv()
    class _CtxLive:
        conversation_manager = _ConvMgr()
    _, name = asyncio.run(personas.resolve_active(_CtxLive(), cfg, "999", "u"))
    check("解析: persona_switch 会话人格最高优先", name == "傲娇")
    cfg_off = dict(cfg, follow_persona_switch=False)
    _, name = asyncio.run(personas.resolve_active(_CtxLive(), cfg_off, "999", "u"))
    check("解析: 关闭兼容后回退默认", name == "温柔")
    cfg_none = dict(cfg, default_persona="", group_persona=[])
    _, name = asyncio.run(personas.resolve_active(_Ctx(), cfg_none, "999", "u"))
    check("解析: 全空回退主配置", name == "主配置·麦麦")

    # 主配置人格：人格名即机器人昵称（如"麦麦"），/persona 麦麦 可切换
    mp = personas.find_persona(cfg, "麦麦")
    check("主配置人格: 名字即机器人昵称", (mp or {}).get("name") == "麦麦" and mp.get("personality") == "主人格")
    check("主配置人格: '主配置' 关键字也命中", (personas.find_persona(cfg, "主配置") or {}).get("name") == "麦麦")
    cfg_dup = dict(cfg, personas=[{"name": "麦麦", "personality": "库内麦麦"}])
    check("库内同名人格优先于主配置", (personas.find_persona(cfg_dup, "麦麦") or {}).get("personality") == "库内麦麦")
    ov_main = personas.overlay(cfg, mp)
    check("主配置人格 overlay = 主配置本值", ov_main["personality"] == "主人格" and ov_main.get("active_persona") == "麦麦")

    # 别名单一列表（对应 MaiBot alias_names）：人格 aliases 覆盖主配置
    cfg_alias = dict(cfg, aliases=["小麦"], default_persona="", group_persona=[])
    ov_alias = personas.overlay(cfg_alias, {"name": "傲娇", "aliases": ["黑千"]})
    check("别名覆盖: 人格 aliases 生效", ov_alias.get("aliases") == ["黑千"])


if __name__ == "__main__":
    test_trigger()
    test_scoring()
    test_text_rules()
    test_postprocess()
    test_typo()
    test_prompt()
    test_states()
    test_learning()
    test_planner()
    test_monitor()
    test_bridge_toolset()
    test_deferred_pool_dependencies()
    test_tool_skill_registry()
    test_tool_exec_official_path()
    test_personas()
    test_taskregistry()
    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    # check 失败必须非零退出，否则 CI 步骤假绿（Sourcery PR 审查指出）
    sys.exit(1 if FAIL else 0)
