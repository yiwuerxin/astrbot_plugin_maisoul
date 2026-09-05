"""评分词典与常量 —— 逐条对齐 MaiBot src/maisaka/reply_necessity.py"""

import re

TRIGGER_SCORE = 80
PRESSURE_STANDARD, PRESSURE_MAX, PRESSURE_FULL_RATIO = 50, 100, 5.0
IDLE_PRESSURE_BONUS = 15
SELF_RATIO_FREE, SELF_RATIO_FULL, SELF_PENALTY_MAX = 0.25, 0.60, 25

DIRECT_REQUEST_TERMS = ("帮我", "帮忙", "能不能", "可以吗", "要不要")
WEAK_REQUEST_TERMS = ("需要", "求", "看看", "试试")
QUESTION_TERMS = ("怎么", "如何", "为什么", "有没有")
OPINION_TERMS = ("你觉得", "你认为", "咋看", "有什么建议")
SHORT_REACTIONS = {"哈哈", "哈哈哈", "草", "笑死", "好", "嗯", "啊", "哦", "6", "666", "？", "?"}
MEDIA_PLACEHOLDER_PREFIXES = ("[CQ:image", "[图片：", "[表情包:", "[文件]", "[语音:", "[卡片:")
IGNORED_TEXT_PREFIXES = ("【合并转发消息:", *MEDIA_PLACEHOLDER_PREFIXES, "本群发言榜")
OTHER_ASSISTANT_PATTERN = re.compile(r"^(?:DeepSeek|ChatGPT|Grok|豆包|千问|元宝|通义|Kimi|Claude)[，,、\s]")

# MaiBot maisaka_generator_base._build_replyer_output_instruction() 中文原文
OUTPUT_INSTRUCTION = "请注意不要输出多余内容(包括不必要的前后缀，冒号，括号，表情包，@等 )，只输出发言内容就好。"

# 调度时机 —— 对齐 maisaka/runtime.py
MESSAGE_DEBOUNCE_SECONDS = 1.0          # _message_debounce_seconds：开轮前等消息静默窗
EXTERNAL_SAMPLE_WINDOW_SECONDS = 1800.0  # 外部消息间隔样本窗（30 分钟）
EXTERNAL_BURST_INTERVAL_SECONDS = 5.0    # 连发抖动：间隔 <5s 不采样
EXTERNAL_MIN_AVERAGE_INTERVAL_SECONDS = 30.0  # 平均间隔下限（空窗补偿不被连发拉低）
