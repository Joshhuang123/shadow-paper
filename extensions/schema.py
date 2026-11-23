"""论文文档结构（schema 契约）。

论文不是「章节 + 句子」，是「小节 + 块」。块有六种：
    p     段落（可含行内公式）
    eq    独立公式（带 LaTeX 原式）
    fig   图（本地 PNG 路径 + 图注）
    table 表格（行 × 列 + 表注）
    list  列表
    quote 引用块

一个 paper 文档（存进 SQLite 的 data_json）：

{
  "id":        "2611.05417",
  "title":     "Organizing Intelligence",
  "authors":   ["Alex Imas", "Andrew Koh", "Shane Legg"],
  "abstract":  "……",
  "venue":     "arXiv:2611.05417v1 [econ.TH]",
  "date":      "2026-10-31",
  "source":    {"type": "arxiv"|"pdf", "raw": "…原始文件名", "url": "…"},
  "sections":  [ {"id":"s1", "title":"Introduction", "level":1, "blocks":[ … ]} ],
  "toc":       [ {"id":"s1", "title":"Introduction", "level":1} ],
  "refs":      [ {"key":"…", "text":"…"} ],
  "stats":     {"words": 31200, "blocks": 410, "formulas": 1921, "figures": 32},
  "imported_at": "2026-11-23T18:30:00"
}

**块内文本**统一用纯文本 + 行内公式占位：
    行内公式写成 ⟦MATH:⟧ + LaTeX + ⟧⟧
这样「一句话」可以被切句、可以点词查义、可以喂 TTS，而公式原式不丢。

行内公式占位的取舍：
    * 用 ⟦MATH:…⟧ 而不是 $…$：$ 在英文正文里太常见（货币、变量），会误伤切句。
    * 占位符在渲染时换成 <span class="math">…</span>，由 KaTeX/MathJax 渲染。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# 行内公式占位
MATH_OPEN = "⟦MATH:"
MATH_CLOSE = "⟧⟧"
_MATH_RE = re.compile(re.escape(MATH_OPEN) + r"(.*?)" + re.escape(MATH_CLOSE), re.S)

BLOCK_KINDS = ("p", "eq", "fig", "table", "list", "quote")


def wrap_math(latex: str) -> str:
    """把行内公式包成占位符。"""
    return f"{MATH_OPEN}{latex}{MATH_CLOSE}"


def split_math(text: str):
    """把带占位符的文本拆成 [(kind, value), …]，kind ∈ {"text","math"}。"""
    out = []
    pos = 0
    for m in _MATH_RE.finditer(text):
        if m.start() > pos:
            out.append(("text", text[pos:m.start()]))
        out.append(("math", m.group(1)))
        pos = m.end()
    if pos < len(text):
        out.append(("text", text[pos:]))
    return out


def strip_math(text: str) -> str:
    """去掉占位符，只留可读文字（喂 TTS / 算字数时用）。"""
    return _MATH_RE.sub(" 公式 ", text)


# ── 校验 ──────────────────────────────────────────────────────
def validate(doc: Dict[str, Any]) -> List[str]:
    """返回问题列表，空列表 = 合法。"""
    errs: List[str] = []
    if not doc.get("id"):
        errs.append("缺 id")
    if not doc.get("title"):
        errs.append("缺 title")
    sections = doc.get("sections")
    if not isinstance(sections, list) or not sections:
        errs.append("sections 必须是非空列表")
        return errs
    n_blocks = 0
    for i, sec in enumerate(sections):
        if not sec.get("id"):
            errs.append(f"section[{i}] 缺 id")
        if "title" not in sec:
            errs.append(f"section[{i}] 缺 title")
        lvl = sec.get("level", 1)
        if not isinstance(lvl, int) or not (1 <= lvl <= 6):
            errs.append(f"section[{i}] level 非法：{lvl!r}")
        blocks = sec.get("blocks")
        if not isinstance(blocks, list):
            errs.append(f"section[{i}] blocks 必须是列表")
            continue
        for j, b in enumerate(blocks):
            n_blocks += 1
            k = b.get("k")
            if k not in BLOCK_KINDS:
                errs.append(f"section[{i}].block[{j}] 未知块类型 {k!r}")
            if k in ("p", "quote"):
                if not isinstance(b.get("t"), str):
                    errs.append(f"section[{i}].block[{j}] 段落缺文本")
            elif k == "eq":
                if not isinstance(b.get("latex"), str) or not b.get("latex", "").strip():
                    errs.append(f"section[{i}].block[{j}] 公式缺 latex")
            elif k == "fig":
                if not b.get("src"):
                    errs.append(f"section[{i}].block[{j}] 图缺 src")
            elif k == "table":
                if not isinstance(b.get("rows"), list):
                    errs.append(f"section[{i}].block[{j}] 表格缺 rows")
            elif k == "list":
                if not isinstance(b.get("items"), list):
                    errs.append(f"section[{i}].block[{j}] 列表缺 items")
    if n_blocks == 0:
        errs.append("没有任何块")
    return errs


def make_section(sid: str, title: str, level: int = 1) -> Dict[str, Any]:
    return {"id": sid, "title": title, "level": level, "blocks": []}


def make_doc(pid: str, title: str, **kw) -> Dict[str, Any]:
    doc = {
        "id": pid,
        "title": title,
        "authors": kw.get("authors", []),
        "abstract": kw.get("abstract", ""),
        "venue": kw.get("venue", ""),
        "date": kw.get("date", ""),
        "source": kw.get("source", {}),
        "sections": kw.get("sections", []),
        "toc": kw.get("toc", []),
        "refs": kw.get("refs", []),
        "stats": kw.get("stats", {}),
        "imported_at": kw.get("imported_at", ""),
    }
    return doc
