"""中文错别字生成器 —— 源码级移植 MaiBot src/chat/utils/typo_generator.py

基于拼音（pypinyin TONE3）与字频的错字生成：
- 单字替换：同音（含声调错误）候选按字频差指数衰减加权
- 整词替换：jieba 分词后取同音词（词频+字频综合评分）
- 50% 概率给出一条纠正建议（正确字/词）

依赖：jieba、pypinyin（astrbot 镜像内置）；
字频表 data_char_frequency.json（从 MaiBot depends-data/char_frequency.json 拷贝）。
与 MaiBot 的差异仅一处：jieba 词典在进程内缓存一次（MaiBot 每次重读），行为一致。
"""

import itertools
import json
import math
import os
import random
from collections import defaultdict
from pathlib import Path

import jieba
from astrbot.api import logger
from pypinyin import Style, pinyin

_DATA_DIR = Path(__file__).resolve().parent.parent
_FREQ_FILE = _DATA_DIR / "data_char_frequency.json"


def _is_chinese_char(char: str) -> bool:
    return "\u4e00" <= char <= "\u9fff"


_PINYIN_DICT_CACHE: dict | None = None


def _shared_pinyin_dict() -> dict:
    """全字符拼音索引（0x4E00-0x9FFF 逐字 pinyin，与生成器参数无关）。

    进程级缓存：两万字的逐字扫描是秒级开销，此前挂在生成器 __init__ 里，
    每次调参重建生成器（get_typo_generator 参数元组变化）都会重跑一遍，
    调参后的第一条回复明显卡顿。"""
    global _PINYIN_DICT_CACHE
    if _PINYIN_DICT_CACHE is None:
        pinyin_dict = defaultdict(list)
        for code in range(0x4E00, 0x9FFF):
            char = chr(code)
            try:
                py = pinyin(char, style=Style.TONE3)[0][0]
                pinyin_dict[py].append(char)
            except Exception:
                # 降级：个别生僻字无拼音读数，跳过（不参与错字替换）
                continue
        _PINYIN_DICT_CACHE = pinyin_dict
    return _PINYIN_DICT_CACHE


class ChineseTypoGenerator:
    """参数与 MaiBot chinese_typo 配置一一对应。"""

    def __init__(
        self,
        error_rate=0.01,
        min_freq=9,
        tone_error_rate=0.1,
        word_replace_rate=0.006,
        max_freq_diff=200,
    ):
        self.error_rate = error_rate
        self.min_freq = min_freq
        self.tone_error_rate = tone_error_rate
        self.word_replace_rate = word_replace_rate
        self.max_freq_diff = max_freq_diff
        # 共享缓存（defaultdict）：只读使用，缺失键经 .get 访问不污染缓存
        self.pinyin_dict = _shared_pinyin_dict()
        self.char_frequency = self._load_char_frequency()
        self._jieba_dict: dict | None = None

    # ------------------------------------------------------------------ #
    # 数据加载
    # ------------------------------------------------------------------ #
    def _load_char_frequency(self) -> dict:
        if _FREQ_FILE.exists():
            try:
                with open(_FREQ_FILE, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                # 形状校验：json.load 对 null/列表/标量/非数值不抛异常，直接
                # 带出去会在查频时 AttributeError——形状不对一律按损坏自愈
                if (
                    not isinstance(loaded, dict)
                    or not loaded
                    or not all(isinstance(v, (int, float)) for v in loaded.values())
                ):
                    raise ValueError("invalid frequency cache shape")
                return loaded
            except (OSError, ValueError):
                # 坏缓存自愈（对齐 learning 库的 .corrupt 处理）：备份原文件后
                # 重建，禁止让错字引擎整体不可用
                logger.warning(
                    "maisoul: 字频缓存损坏，已备份为 .corrupt 并按 jieba 重建",
                    exc_info=True,
                )
                try:
                    _FREQ_FILE.replace(
                        _FREQ_FILE.parent / (_FREQ_FILE.name + ".corrupt")
                    )
                except OSError:
                    logger.debug(
                        "maisoul: 字频缓存损坏备份失败（继续重建）", exc_info=True
                    )
        # 无缓存时按 MaiBot 逻辑从 jieba 词典生成并落盘
        char_freq = defaultdict(int)
        dict_path = os.path.join(os.path.dirname(jieba.__file__), "dict.txt")
        with open(dict_path, "r", encoding="utf-8") as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) < 2:
                    continue
                word, freq = parts[0], parts[1]
                for char in word:
                    if _is_chinese_char(char):
                        char_freq[char] += int(freq)
        max_freq = max(char_freq.values())
        normalized = {c: v / max_freq * 1000 for c, v in char_freq.items()}
        try:
            # 原子落盘：直接 open("w") 写中途崩溃会留半截 JSON，下次启动
            # 走上面的损坏分支（tmp+replace，对齐 learning.save）
            tmp = _FREQ_FILE.parent / (_FREQ_FILE.name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(normalized, f, ensure_ascii=False, indent=2)
            tmp.replace(_FREQ_FILE)
        except OSError:
            logger.debug("maisoul: 字频缓存落盘失败（本次用内存态）", exc_info=True)
        return normalized

    def _get_jieba_dict(self) -> dict:
        if self._jieba_dict is None:
            valid_words = {}
            dict_path = os.path.join(os.path.dirname(jieba.__file__), "dict.txt")
            with open(dict_path, "r", encoding="utf-8") as f:
                for line in f:
                    parts = line.strip().split()
                    if len(parts) >= 2:
                        try:
                            valid_words[parts[0]] = float(parts[1])
                        except ValueError:
                            continue
            self._jieba_dict = valid_words
        return self._jieba_dict

    # ------------------------------------------------------------------ #
    # 拼音与候选
    # ------------------------------------------------------------------ #
    @staticmethod
    def _get_similar_tone_pinyin(py: str) -> str:
        if not py:
            return py
        if not py[-1].isdigit():
            return f"{py}1"
        base, tone = py[:-1], int(py[-1])
        if tone not in (1, 2, 3, 4):
            return base + str(random.choice([1, 2, 3, 4]))
        tones = [1, 2, 3, 4]
        tones.remove(tone)
        return base + str(random.choice(tones))

    def _calculate_replacement_probability(self, orig_freq, target_freq) -> float:
        if target_freq > orig_freq:
            return 1.0
        freq_diff = orig_freq - target_freq
        if freq_diff > self.max_freq_diff:
            return 0.0
        return math.exp(-3 * freq_diff / self.max_freq_diff)

    def _get_similar_frequency_chars(self, char, py, num_candidates=5):
        homophones = []
        if random.random() < self.tone_error_rate:
            homophones.extend(
                self.pinyin_dict.get(self._get_similar_tone_pinyin(py), [])
            )
        homophones.extend(self.pinyin_dict.get(py, []))
        if not homophones:
            return None
        orig_freq = self.char_frequency.get(char, 0)
        freq_pairs = [
            (h, self.char_frequency.get(h, 0))
            for h in homophones
            if h != char and self.char_frequency.get(h, 0) >= self.min_freq
        ]
        if not freq_pairs:
            return None
        candidates = []
        for h, freq in freq_pairs:
            prob = self._calculate_replacement_probability(orig_freq, freq)
            if prob > 0:
                candidates.append((h, prob))
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[1], reverse=True)
        return [c for c, _ in candidates[:num_candidates]]

    def _get_word_homophones(self, word):
        if len(word) == 1:
            return []
        word_pinyin = [py[0] for py in pinyin(word, style=Style.TONE3)]
        candidates = []
        for py in word_pinyin:
            chars = self.pinyin_dict.get(py, [])
            if not chars:
                return []
            candidates.append(chars)
        valid_words = self._get_jieba_dict()
        original_freq = valid_words.get(word, 0)
        min_word_freq = original_freq * 0.1
        homophones = []
        for combo in itertools.product(*candidates):
            new_word = "".join(combo)
            if new_word != word and new_word in valid_words:
                new_freq = valid_words[new_word]
                if new_freq >= min_word_freq:
                    char_avg = sum(
                        self.char_frequency.get(c, 0) for c in new_word
                    ) / len(new_word)
                    score = new_freq * 0.7 + char_avg * 0.3
                    if score >= self.min_freq:
                        homophones.append((new_word, score))
        homophones.sort(key=lambda x: x[1], reverse=True)
        return [w for w, _ in homophones[:5]]

    # ------------------------------------------------------------------ #
    # 主入口
    # ------------------------------------------------------------------ #
    def create_typo_sentence(self, sentence: str) -> tuple[str, str | None]:
        """返回 (含错字的句子, 纠正建议或 None)。纠正建议本身有 50% 概率给出。"""
        result = []
        word_typos = []
        char_typos = []
        words = list(jieba.cut(sentence))

        for word in words:
            if all(not _is_chinese_char(c) for c in word):
                result.append(word)
                continue
            word_pinyin = [py[0] for py in pinyin(word, style=Style.TONE3)]

            # 整词替换
            if len(word) > 1 and random.random() < self.word_replace_rate:
                homophones = self._get_word_homophones(word)
                if homophones:
                    typo_word = random.choice(homophones)
                    result.append(typo_word)
                    word_typos.append((typo_word, word))
                    continue

            # 单字替换
            if len(word) == 1:
                char, py = word, word_pinyin[0]
                if random.random() < self.error_rate:
                    similar = self._get_similar_frequency_chars(char, py)
                    if similar:
                        typo_char = random.choice(similar)
                        replace_prob = self._calculate_replacement_probability(
                            self.char_frequency.get(char, 0),
                            self.char_frequency.get(typo_char, 0),
                        )
                        if random.random() < replace_prob:
                            result.append(typo_char)
                            char_typos.append((typo_char, char))
                            continue
                result.append(char)
            else:
                # 词内单字：替换概率随词长衰减 0.7^(len-1)
                word_error_rate = self.error_rate * (0.7 ** (len(word) - 1))
                word_result = []
                for char, py in zip(word, word_pinyin):
                    if random.random() < word_error_rate:
                        similar = self._get_similar_frequency_chars(char, py)
                        if similar:
                            typo_char = random.choice(similar)
                            replace_prob = self._calculate_replacement_probability(
                                self.char_frequency.get(char, 0),
                                self.char_frequency.get(typo_char, 0),
                            )
                            if random.random() < replace_prob:
                                word_result.append(typo_char)
                                char_typos.append((typo_char, char))
                                continue
                    word_result.append(char)
                result.append("".join(word_result))

        correction = None
        if random.random() < 0.5:
            if word_typos:
                correction = random.choice(word_typos)[1]
            elif char_typos:
                correction = random.choice(char_typos)[1]
        return "".join(result), correction


_generator: ChineseTypoGenerator | None = None


def get_typo_generator(cfg) -> ChineseTypoGenerator:
    """按 chinese_typo 配置返回进程级单例（参数变化时重建）。"""
    global _generator
    params = (
        float(cfg.get("typo_error_rate", 0.01)),
        int(cfg.get("typo_min_freq", 9)),
        float(cfg.get("typo_tone_error_rate", 0.1)),
        float(cfg.get("typo_word_replace_rate", 0.006)),
    )
    if (
        _generator is None
        or tuple(
            (
                _generator.error_rate,
                _generator.min_freq,
                _generator.tone_error_rate,
                _generator.word_replace_rate,
            )
        )
        != params
    ):
        _generator = ChineseTypoGenerator(
            error_rate=params[0],
            min_freq=params[1],
            tone_error_rate=params[2],
            word_replace_rate=params[3],
        )
    return _generator
