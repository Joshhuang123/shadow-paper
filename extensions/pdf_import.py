"""本地 PDF 导入 → 论文文档结构。

和 arXiv 路径的根本差别（**别混淆**）：
    arXiv 路径拿的是 LaTeX **源码**，公式是原式，结构是 \\section/\\begin{figure} —— 什么都不用猜。
    PDF 路径只有**排版后的文字层**：
      · 公式只能拿到压平后的碎片（"P(Q) =0.3 / r / 1− Q200N"），**救不回来**
      · 双栏要靠几何猜
      · 图表里的文字拿不到（不做 OCR）

两个实测出来的坑（都写在这里，免得重踩）：
  1. **这篇论文的 PDF 文字层里没有空格字符** —— 词间距是排版定位给的视觉间隙。
     所以 pdftotext 和 pdfplumber 抽出来都会**粘词**
     （"howintelligence"、"competence,which capturesdomain-specificcapabilities"）。
     这是 PDF 文字层的天花板，不是工具的问题。
  2. 正文里到处是**公式碎片**（"H N 1 2 ≈"、"I ASI"），
     正则一匹配就变成假小节标题 → 所以标题判定要过**「像散文」**这一关。
     另外这篇论文的**小节标题与正文同字号**，靠字号认标题不成立 —— 只当参考。

定位：这条路径只保证「**能读全文、能点词查义、能收生词本**」。
      公式密集的论文请走 arXiv 路径。
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import schema

# 小节标题：1 / 1.2 / 1.2.3 / A / A.1 / 附录 A
_HEADING_RE = re.compile(
    r"^\s*((?:\d+(?:\.\d+)*)|[A-Z](?:\.\d+)*)\s+([A-Za-z][^.!?]{2,70})\s*$"
)
_APPENDIX_RE = re.compile(r"^\s*(Appendix|附录)\s+([A-Z])\b", re.I)

# ── 中文标题 ────────────────────────────────────────────────
# 中文论文的小节标题常见三种写法：
#   ① 第 X 章 / 第 X 节 + 标题        「第三章 模型」「第 2 节 均衡」
#   ② 中文数字 + 顿号 + 标题          「一、三句话结论」「二、论文对我们路线图的三个具体修正」
#   ③ 阿拉伯数字 + 空格 + 中文标题     「1 引言」「4 均衡与效率」
_CN_CHAPTER_RE = re.compile(r"^\s*第\s*([一二三四五六七八九十百千\d]+)\s*[章节節]\s*(.{1,40}?)\s*$")
_CN_NUM_RE = re.compile(r"^\s*([一二三四五六七八九十]+)\s*[、.．]\s*(.{1,40}?)\s*$")
_CN_ARABIC_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)\s*[、.．]?\s+([一-鿿][^\d]{1,40}?)\s*$")
# 目录条目：「1 引言 7」「2.3 均衡 12」—— 结尾那个裸数字是页码，不是标题的一部分
_CN_TOC_LINE_RE = re.compile(r"^\s*\d+(?:\.\d+)*\s+.+?\s+\d{1,4}\s*$")
# 中文数字开头的目录条目：「一、三句话结论 48」
_CN_TOC_LINE_CN_RE = re.compile(r"^\s*[一二三四五六七八九十]+\s*[、.．]\s+.+?\s+\d{1,4}\s*$")
# 目录页的标记行
_TOC_MARKER_RE = re.compile(r"^\s*(目\s*录|目录|Table\s*of\s*Contents|Contents)\s*$", re.I)

# 标题的正文部分必须**像散文**：只允许字母数字和常见标点，不许出现数学符号。
_TITLE_PROSE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 ,'\-–—&()/:]{2,68}$")
# 中文标题：2–40 个字，以中文/数字开头，不以句末标点收尾
_CN_TITLE_RE = re.compile(r"^[一-鿿\d][一-鿿\d A-Za-z0-9，、：:（）()·\-—""''《》%]{1,40}$")
_CN_STOP = "。！？；：，、,.;:!?"


def _looks_like_cn_title(t: str) -> bool:
    """中文标题判定：像标题、且不是句子/图注。"""
    t = (t or "").strip()
    if not t or len(t) < 2 or len(t) > 40:
        return False
    if not _CN_TITLE_RE.match(t):
        return False
    # 句末标点收尾 → 是句子不是标题
    if t[-1] in _CN_STOP:
        return False
    # 夹着数字的（图注常常有 "表 3"）→ 不算标题
    if re.search(r"[（(]\s*\d+\s*[）)]", t):
        return False
    return True


def _looks_like_title(t: str) -> bool:
    """判断一段文字像不像**章节标题**（而不是公式碎片或乱码）。"""
    t = (t or "").strip()
    if not t:
        return False
    # 中文标题走另一套判定
    if re.search(r"[一-鿿]", t):
        return _looks_like_cn_title(t)
    if not _TITLE_PROSE_RE.match(t):
        return False
    words = re.findall(r"[A-Za-z][A-Za-z\-']{2,}", t)
    if not words:
        return False
    # 至少一个 ≥4 字母的词（挡掉 "ASI" 这种缩写碎片）
    if not any(len(w) >= 4 for w in words):
        return False
    # 单词标题要够长（"firm" 是图注不是标题；"References"/"Conclusion" 够长）
    if len(words) == 1 and len(words[0]) < 5:
        return False
    # 标题里不该有**独立的数字**（"firms 1 firm" 是图注不是标题）
    if re.search(r"\b\d+(?:\.\d+)?\b", t):
        return False
    letters = sum(1 for c in t if c.isalpha())
    if letters / max(1, len(t)) < 0.75:
        return False
    if len(words) > 14:
        return False
    return True


def _plausible_number(num: str) -> bool:
    """小节编号得像个编号：纯整数 < 100，或点分（每段 < 100）。

    挡掉图注里的 "1000 Scale threshold"（1000 不是节号）。
    """
    parts = num.split(".")
    for p in parts:
        if not p.isdigit() or int(p) >= 100:
            return False
    return True


_CN_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
              "八": 8, "九": 9, "十": 10}


def _cn_number_level(num: str) -> int:
    """中文数字编号 → 层级。「一、」是一级（=第 X 章），「（一）」是二级。"""
    if not num:
        return 1
    if num in _CN_DIGITS:
        return 1
    if num.startswith("十"):          # 十一、十二…
        return 1
    return 1


def _heading_of(text: str):
    """一行文字 → (编号, 标题, 层级) 或 None。

    中英文都给。中文要额外排掉**目录条目**（结尾带页码的那种）。
    """
    s = (text or "").strip()
    if not s or len(s) > 80:
        return None

    # 目录条目：「1 引言 7」—— 结尾的裸数字是页码
    if _CN_TOC_LINE_RE.match(s) or _CN_TOC_LINE_CN_RE.match(s):
        return None

    # ① 第 X 章/节
    m = _CN_CHAPTER_RE.match(s)
    if m:
        title = m.group(2)
        if _looks_like_cn_title(title):
            return (m.group(1), title, 1)

    # ② 一、二、…
    m = _CN_NUM_RE.match(s)
    if m:
        title = m.group(2)
        if _looks_like_cn_title(title):
            return (m.group(1), title, _cn_number_level(m.group(1)))

    # ③ 1 引言
    m = _CN_ARABIC_RE.match(s)
    if m and _looks_like_cn_title(m.group(2)):
        return (m.group(1), m.group(2), m.group(1).count(".") + 1)

    # 英文：1 Introduction / A.1 Title
    m = _HEADING_RE.match(s)
    if m:
        return (m.group(1), m.group(2).strip(), m.group(1).count(".") + 1)

    return None


class ImportError_(Exception):
    pass


# ── pdfplumber：一次拿到「行文本 + 字号 + 位置」 ───────────────
def _page_lines(page) -> List[Dict]:
    """一页 → [{text, size, x0, x1, top}]。"""
    out = []
    try:
        # ⚠️ x_tolerance 必须收紧（2026-11-24 实测）：
        #    pdfplumber 默认 3，LaTeX 排版的词间距约 3pt 会被它当成「同一个词」，
        #    于是整段文字**粘连**——「COCONUT as a pseudo-reasoning mechanism」
        #    变成「CONUTasapseudo-reasoningmechanism」。
        #    后果有三：读者看到的是黏字、检索匹配不到、切句把词切坏。
        #    2 是实测的临界值（≤2 正常、3 开始粘）。
        try:
            lines = page.extract_text_lines(layout=False, strip=True, x_tolerance=2)
        except TypeError:
            lines = page.extract_text_lines(layout=False, strip=True)
    except Exception:
        return out
    for ln in lines:
        txt = (ln.get("text") or "").strip()
        if not txt:
            continue
        sizes = [c.get("size", 0) for c in (ln.get("chars") or [])]
        out.append({
            "text": txt,
            "size": max(sizes) if sizes else 0.0,
            "x0": float(ln.get("x0", 0) or 0),
            "x1": float(ln.get("x1", 0) or 0),
            "top": float(ln.get("top", 0) or 0),
        })
    return out


def _detect_columns(page) -> Tuple[float, float]:
    """用词的几何分布猜分栏，返回 (中缝左, 中缝右)；单栏返回 (0,0)。"""
    try:
        words = page.extract_words()
    except Exception:
        return (0.0, 0.0)
    if len(words) < 80:
        return (0.0, 0.0)
    width = float(page.width or 0)
    if not width:
        return (0.0, 0.0)
    mid = width / 2.0
    xs = [w["x0"] for w in words if w["x1"] - w["x0"] > 1]
    if not xs:
        return (0.0, 0.0)
    in_mid = sum(1 for x in xs if mid - width * 0.04 <= x <= mid + width * 0.04)
    if in_mid > len(xs) * 0.12:
        return (0.0, 0.0)
    left = [w for w in words if w["x1"] <= mid + width * 0.03]
    right = [w for w in words if w["x0"] >= mid - width * 0.03]
    if len(left) < 40 or len(right) < 40:
        return (0.0, 0.0)
    return (mid, mid)


def _order_lines(lines: List[Dict], cut_l: float, cut_r: float) -> List[Dict]:
    """双栏按「先左后右、各自从上到下」排序；单栏按 y 排。"""
    if not cut_l:
        return sorted(lines, key=lambda d: d["top"])
    left = [d for d in lines if d["x1"] <= cut_r]
    right = [d for d in lines if d["x0"] >= cut_l]
    mid = [d for d in lines if d not in left and d not in right]
    return (sorted(left, key=lambda d: d["top"]) + sorted(mid, key=lambda d: d["top"])
            + sorted(right, key=lambda d: d["top"]))


def _fallback_text(pdf: Path) -> List[List[str]]:
    """没有 pdfplumber 时退回 pdftotext -layout（只拿文本，不拿字号）。"""
    if not shutil.which("pdftotext"):
        raise ImportError_("这台机器需要 pdfplumber 或 pdftotext(poppler) 才能读 PDF")
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "p.txt"
        r = subprocess.run(["pdftotext", "-layout", str(pdf), str(out)],
                           capture_output=True, timeout=300)
        if r.returncode != 0 or not out.exists():
            raise ImportError_("这个 PDF 打不开（可能已损坏或被加密）")
        pages = out.read_text(encoding="utf-8", errors="replace").split("\f")
    return [[{"text": l.strip(), "size": 0.0, "x0": 0.0, "x1": 0.0, "top": i}
             for i, l in enumerate(p.split("\n")) if l.strip()] for p in pages if p.strip()]


def _read_pages(pdf: Path) -> List[List[Dict]]:
    """逐页取行（含字号）。返回 [[{text,size,...}]]。"""
    try:
        import pdfplumber
    except ImportError:
        return _fallback_text(pdf)
    pages: List[List[Dict]] = []
    with pdfplumber.open(str(pdf)) as doc:
        for page in doc.pages:
            lines = _page_lines(page)
            if not lines:
                continue
            cut_l, cut_r = _detect_columns(page)
            pages.append(_order_lines(lines, cut_l, cut_r))
    if not pages:
        return _fallback_text(pdf)
    return pages


def _paper_id(stem: str) -> str:
    """文件名 → 论文 id。

    非 ASCII（中文）文件名不能直接当 id（会变成空串，
    所有中文 PDF 都会撞成同一个 `paper`）→ 走 md5 前 8 位，
    和 shadow-ebook 的 `_sanitize_book_id` 同一个思路。
    """
    raw = (stem or "").strip()
    ascii_id = re.sub(r"[^A-Za-z0-9_.-]", "", raw)[:40]
    if ascii_id and len(ascii_id) >= 3:
        return ascii_id
    import hashlib
    return "paper_" + hashlib.md5(raw.encode("utf-8")).hexdigest()[:8]


def import_pdf(raw: bytes, filename: str, asset_dir: Path) -> dict:
    """PDF 字节 → 论文文档。"""
    with tempfile.TemporaryDirectory(prefix="pdf_") as td:
        pdf = Path(td) / (Path(filename).stem + ".pdf")
        pdf.write_bytes(raw)
        pages = _read_pages(pdf)
        if not pages:
            raise ImportError_("这个 PDF 里提取不到文字（多半是扫描版，需要先 OCR）")

        sections: List[Dict] = []
        toc: List[Dict] = []
        cur = schema.make_section("s1", "正文", 1)
        sections.append(cur)
        toc.append({"id": "s1", "title": "正文", "level": 1})
        seq = 1

        for pi, lines in enumerate(pages):
            bsize = Counter(round(d["size"], 1) for d in lines if d["size"]).most_common(1)
            bsize = bsize[0][0] if bsize else 0.0
            # 目录页：整页都不认标题（目录里的条目长得跟正文标题一模一样，
            # 认了会生成一堆空章节）。标记行所在页 + 紧接着的一页都算目录。
            in_toc = any(_TOC_MARKER_RE.match((d["text"] or "").strip()) for d in lines)
            if not in_toc and pi > 0:
                prev = pages[pi - 1]
                in_toc = any(_TOC_MARKER_RE.match((d["text"] or "").strip()) for d in prev)
            for d in lines:
                s = (d["text"] or "").strip()
                if not s:
                    continue
                m = None if in_toc else _APPENDIX_RE.match(s)
                if m:
                    seq += 1
                    title = f"附录 {m.group(2)}"
                    cur = schema.make_section(f"s{seq}", title, 1)
                    sections.append(cur)
                    toc.append({"id": cur["id"], "title": title, "level": 1})
                    continue
                hm = None if in_toc else _heading_of(s)
                if hm and len(s) < 80:
                    num, title, level = hm
                    # ① 标题部分像标题（挡掉 "N 1 2 ≈"、"ASI" 这类公式碎片）
                    # ② 字号不小于正文（这篇论文标题与正文同号，所以这条只是兜底，
                    #    真正起作用的是 ①）
                    big_enough = (not bsize) or d["size"] >= bsize - 0.8
                    ok_num = True
                    if re.fullmatch(r"[\d.]+", num or ""):
                        ok_num = _plausible_number(num)
                    if _looks_like_title(title) and big_enough and ok_num:
                        seq += 1
                        cur = schema.make_section(f"s{seq}", f"{num} {title}", min(level, 6))
                        sections.append(cur)
                        toc.append({"id": cur["id"], "title": cur["title"], "level": cur["level"]})
                        continue
                cur["blocks"].append({"k": "p", "t": s})

        pid = _paper_id(Path(filename).stem)
        doc = schema.make_doc(
            pid,
            Path(filename).stem,
            authors=[],
            abstract="",
            source={"type": "pdf", "raw": filename},
            sections=sections,
            toc=toc,
            stats={},
        )
        n_words = sum(len(b.get("t", "").split()) for s in sections for b in s["blocks"])
        doc["stats"] = {"words": n_words,
                        "blocks": sum(len(s["blocks"]) for s in sections),
                        "figures": 0}
        return doc
