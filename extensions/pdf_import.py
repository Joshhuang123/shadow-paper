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

# 标题的正文部分必须**像散文**：只允许字母数字和常见标点，不许出现数学符号。
_TITLE_PROSE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 ,'\-–—&()/:]{2,68}$")


def _looks_like_title(t: str) -> bool:
    """判断一段文字像不像**章节标题**（而不是公式碎片或乱码）。"""
    t = (t or "").strip()
    if not t or not _TITLE_PROSE_RE.match(t):
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


class ImportError_(Exception):
    pass


# ── pdfplumber：一次拿到「行文本 + 字号 + 位置」 ───────────────
def _page_lines(page) -> List[Dict]:
    """一页 → [{text, size, x0, x1, top}]。"""
    out = []
    try:
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

        for lines in pages:
            bsize = Counter(round(d["size"], 1) for d in lines if d["size"]).most_common(1)
            bsize = bsize[0][0] if bsize else 0.0
            for d in lines:
                s = (d["text"] or "").strip()
                if not s:
                    continue
                m = _APPENDIX_RE.match(s)
                if m:
                    seq += 1
                    title = f"附录 {m.group(2)}"
                    cur = schema.make_section(f"s{seq}", title, 1)
                    sections.append(cur)
                    toc.append({"id": cur["id"], "title": title, "level": 1})
                    continue
                hm = _HEADING_RE.match(s)
                if hm and len(s) < 80:
                    num, title = hm.group(1), hm.group(2).strip()
                    # ① 标题部分像散文（挡掉 "N 1 2 ≈"、"ASI" 这类公式碎片）
                    # ② 字号不小于正文（这篇论文标题与正文同号，所以这条只是兜底，
                    #    真正起作用的是 ①）
                    big_enough = (not bsize) or d["size"] >= bsize - 0.8
                    if (_looks_like_title(title) and big_enough
                            and _plausible_number(num)):
                        level = num.count(".") + 1
                        seq += 1
                        cur = schema.make_section(f"s{seq}", f"{num} {title}", min(level, 6))
                        sections.append(cur)
                        toc.append({"id": cur["id"], "title": cur["title"], "level": cur["level"]})
                        continue
                cur["blocks"].append({"k": "p", "t": s})

        pid = re.sub(r"[^A-Za-z0-9_.-]", "", Path(filename).stem)[:40] or "paper"
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
