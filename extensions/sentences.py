"""句子切分（从 shadow-learning 的 books.py 搬来，**加了一处论文必需的改动**）。

原版的切句逻辑一字未改：缩写/首字母/小数点/省略号保护 + 英文正则 + 中文分支。
论文版新增：**行内公式占位符 ⟦MATH:…⟧⟧⟧ 在切句期间要藏起来** ——
占位符里是 LaTeX，里面有 . 有 \\begin{aligned} 有小数点，
不藏的话一句公式会被切成好几段，"点词查义" 的粒度就全乱了。
"""
from __future__ import annotations

import re

from .schema import MATH_CLOSE, MATH_OPEN

# 排版连字（fi / fl / ff …）还原成普通字母
_LIGATURES = {
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi",
    "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st", "œ": "oe",
    "Œ": "OE", "Æ": "AE", "æ": "ae", "ß": "ss",
    "Ł": "L", "ł": "l",
}

_SENT_ABBREV = re.compile(r"\b(Mr|Mrs|Ms|Dr|Prof|Sr|Jr|St|Mt|vs|etc|e\.g|i\.e|Fig|Eq|Sec|Tab|App)\.")
_SENT_INITIAL = re.compile(r"\b([A-Z])\.")
_SENT_DECIMAL = re.compile(r"(\d)\.(\d)")
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(⟦])")
_CJK_SENT_SPLIT = re.compile(r"(?<=[。！？…])\s*")
_CJK_CHAR_RE = re.compile(r"[一-鿿]")

# ⚠ 两个哨兵必须**分开**：
#   _PLACEHOLDER(\x00) 是原项目用来保护缩写里那个点的，切句末尾会 `.replace(_PLACEHOLDER, ".")`
#   —— 如果公式占位符也用它，切完就被替换成 ".0."，公式直接被毁（2026-11-23 测试抓到）。
#   所以公式用 STX/STH(\x02/\x03)，不跟切句的替换逻辑打架。
_PLACEHOLDER = "\x00"
_MATH_SENTINEL_A = "\x02"
_MATH_SENTINEL_B = "\x03"
_MATH_PROTECT = re.compile(re.escape(MATH_OPEN) + r".*?" + re.escape(MATH_CLOSE), re.S)


def normalize_ligatures(text: str) -> str:
    if not text:
        return text
    for k, v in _LIGATURES.items():
        if k in text:
            text = text.replace(k, v)
    return text


def _protect_math(text: str):
    """把 ⟦MATH:…⟧⟧⟧ 换成不可见占位，返回 (文本, 还原表)。"""
    table = []

    def repl(m):
        table.append(m.group(0))
        return f"{_MATH_SENTINEL_A}{len(table) - 1}{_MATH_SENTINEL_B}"

    return _MATH_PROTECT.sub(repl, text), table


def _restore_math(text: str, table) -> str:
    def repl(m):
        idx = int(m.group(1))
        return table[idx] if 0 <= idx < len(table) else m.group(0)

    return re.sub(_MATH_SENTINEL_A + r"(\d+)" + _MATH_SENTINEL_B, repl, text)


def split_sentences(text: str) -> list:
    """切句。返回 ≥10 字符且 ≥3 词的句子（与原项目口径一致）。"""
    if not text:
        return []
    text = normalize_ligatures(text)
    protected, table = _protect_math(text)
    if _CJK_CHAR_RE.search(protected):
        out = _split_sentences_cjk(protected)
    else:
        out = _split_sentences_en(protected)
    return [_restore_math(s, table) for s in out]


def _split_sentences_cjk(text: str) -> list:
    sentences = []
    for chunk in _CJK_SENT_SPLIT.split(text):
        chunk = (chunk or "").strip().strip("。！？…")
        if not chunk:
            continue
        en = _split_sentences_en(chunk)
        if en:
            sentences.extend(en)
            continue
        cjk = len(_CJK_CHAR_RE.findall(chunk))
        if len(chunk) > 8 and (cjk >= 5 or len(chunk.split()) >= 3):
            sentences.append(chunk)
    return sentences


def _split_sentences_en(text: str) -> list:
    text = _SENT_ABBREV.sub(r"\1" + _PLACEHOLDER, text)
    text = _SENT_INITIAL.sub(r"\1" + _PLACEHOLDER, text)
    text = _SENT_DECIMAL.sub(r"\1" + _PLACEHOLDER + r"\2", text)
    text = text.replace("...", _PLACEHOLDER * 3)
    parts = _SENT_SPLIT.split(text)
    sentences = []
    for p in parts:
        p = p.strip().replace(_PLACEHOLDER, ".").strip("\"' ")
        if len(p) > 10 and len(p.split()) >= 3:
            sentences.append(p)
    return sentences


# ── 点词查义：把一段文本切成「可点词的 token」 ─────────────────
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\-]{1,}")


def tokenize_words(text: str) -> list:
    """返回 [(start, end, word)]，只含英文词（点词查义的对象）。"""
    return [(m.start(), m.end(), m.group(0)) for m in _WORD_RE.finditer(text or "")]
