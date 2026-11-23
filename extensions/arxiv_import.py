"""arXiv 导入：arXiv ID → LaTeX 源码 → pandoc AST → 论文文档结构。

为什么走源码而不是 PDF：
    PDF→文本这条路**对堆叠公式没有解**（分子分母被压平成 "P(Q) =0.3 / r / 1− Q200N"），
    而且双栏要靠猜、图表里的字拿不到。
    LaTeX 源码是**排版之前**的东西，公式是 `$...$` 原式，结构是 \\section/\\begin{figure}。

为什么不用 arxiv.org/html 或 ar5iv：
    arxiv.org/html 只覆盖一部分新论文（实测这篇 404）；ar5iv 要等它异步渲染，
    实测这篇只回摘要页（正文没渲染出来）。
    自己做 pandoc 转换可控、且能拿到完整附录。

一条实测有效的清洗链（2026-11-23，见 README「踩坑」）：
    1. 剥离带可选参数的调用（\\label[appendix]{x}）—— pandoc 直接报错
    2. 抽取并**就地展开**自定义宏 —— pandoc 遇到不认识的宏会一路吃到文件尾，
       报 "unexpected end of input"
    3. \\input / \\include 内联 —— pandoc 不会自己找子文件
    4. 迭代收敛：pandoc 报哪个宏不认就展开哪个，直到通过（实测 5 个宏收敛）
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import schema

# ── 网络 ──────────────────────────────────────────────────────
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/157.0 Safari/537.36"
API = "https://export.arxiv.org/api/query?id_list={pid}"
EPRINT = "https://arxiv.org/e-print/{pid}"


class ImportError_(Exception):
    """导入失败（名字带下划线，避免和内建 ImportError 混淆）。"""


def _get(url: str, timeout: int = 90) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ── arXiv 元数据 ──────────────────────────────────────────────
def fetch_meta(pid: str) -> Dict[str, Any]:
    """arXiv API → {title, authors, abstract, date, venue}。"""
    try:
        raw = _get(API.format(pid=pid), timeout=40).decode("utf-8", "replace")
    except Exception as e:
        raise ImportError_(f"arXiv 元数据拉取失败：{e}") from e
    m = re.search(r"<entry>(.*?)</entry>", raw, re.S)
    if not m:
        raise ImportError_(f"arXiv 上没有这篇：{pid}")
    e = m.group(1)

    def tag(t):
        mm = re.search(rf"<{t}[^>]*>(.*?)</{t}>", e, re.S)
        return re.sub(r"\s+", " ", mm.group(1)).strip() if mm else ""

    authors = re.findall(r"<name>(.*?)</name>", e)
    pub = tag("published")
    return {
        "title": tag("title"),
        "authors": [re.sub(r"\s+", " ", a).strip() for a in authors],
        "abstract": tag("summary"),
        "date": pub[:10],
        "venue": tag("arxiv_announce_type") or "",
        "comment": tag("arxiv_comment"),
        "categories": re.findall(r'term="([^"]+)"', e),
    }


# ── 源码下载 ──────────────────────────────────────────────────
def download_source(pid: str, workdir: Path) -> Path:
    """下载 e-print 源码包并解包，返回主 .tex 的目录。"""
    tarball = workdir / "src.tar.gz"
    try:
        tarball.write_bytes(_get(EPRINT.format(pid=pid), timeout=180))
    except Exception as e:
        raise ImportError_(f"arXiv 源码下载失败（{pid}）：{e}") from e

    ex = workdir / "src"
    ex.mkdir(parents=True, exist_ok=True)
    # arXiv 的包有三种形态：.tar.gz / .gz(单文件) / 裸 PDF
    if tarfile.is_tarfile(tarball):
        with tarfile.open(tarball) as tf:
            try:
                # Python 3.12+ 有 filter（防路径穿越）；3.9 没有 —— 退一步即可
                tf.extractall(ex, filter="data")
            except TypeError:
                tf.extractall(ex)
    else:
        try:
            import gzip
            (ex / "main.tex.gz").write_bytes(tarball.read_bytes())
            with gzip.open(ex / "main.tex.gz") as g:
                (ex / "main.tex").write_bytes(g.read())
        except OSError:
            (ex / "main.tex").write_bytes(tarball.read_bytes())

    texs = sorted(ex.rglob("*.tex"), key=lambda p: -p.stat().st_size)
    if not texs:
        raise ImportError_(f"源码包里没有 .tex：{pid}")
    return texs[0].parent


# ── LaTeX 清洗 + pandoc 收敛 ─────────────────────────────────
def _clean_and_convert(tex_path: Path, workdir: Path, base_dir: Path,
                        unwrap_cache: Optional[Path] = None) -> Dict[str, Any]:
    """清洗 tex → 迭代 pandoc → 返回 pandoc AST（dict）。"""
    from . import latex_clean

    unwrap: List[str] = []
    if unwrap_cache and unwrap_cache.exists():
        try:
            unwrap = json.loads(unwrap_cache.read_text(encoding="utf-8"))
        except Exception:
            unwrap = []

    clean_path = workdir / "clean.tex"
    last_err = ""
    # 参考文献：跟主 .tex 同目录的 .bib（名字不固定，得自己找）
    bib = None
    for cand in sorted(base_dir.glob("*.bib")):
        bib = cand
        break
    for _ in range(30):
        latex_clean.clean_tex(tex_path, clean_path)
        s = clean_path.read_text(encoding="utf-8")
        for mac in unwrap:
            s = latex_clean.expand_named_macro(s, mac)
        clean_path.write_text(s, encoding="utf-8")

        cmd = ["pandoc", str(clean_path), "-f", "latex", "-t", "json", "--citeproc"]
        if bib:
            cmd.append(f"--bibliography={bib.resolve()}")
        p = subprocess.run(cmd, cwd=base_dir, capture_output=True, text=True, timeout=900)
        if p.returncode == 0:
            if unwrap_cache:
                unwrap_cache.parent.mkdir(parents=True, exist_ok=True)
                unwrap_cache.write_text(json.dumps(unwrap, ensure_ascii=False), encoding="utf-8")
            return json.loads(p.stdout)
        last_err = p.stderr.strip().split("\n")[0]
        m = re.search(r"\(line (\d+), column (\d+)\)", last_err)
        if not m:
            raise ImportError_(f"pandoc 失败：{last_err[:180]}")
        mac = latex_clean.macro_at(s, int(m.group(1)), int(m.group(2)))
        if not mac or mac in unwrap:
            raise ImportError_(f"pandoc 失败且无法定位新宏：{last_err[:180]}")
        unwrap.append(mac)

    raise ImportError_(f"pandoc 收敛失败（30 轮）：{last_err[:180]}")


# ── AST → 论文结构 ───────────────────────────────────────────

def _figure_url(target) -> str:
    """从 Figure 的 target（inline 节点列表）里挖出图片 url。

    target 里通常是 Plain → Image → [attr, inlines, [url, title]]，
    但也可能直接是 [url, title] 或裸字符串 —— 三种都要认。
    """
    if isinstance(target, str):
        return target
    if isinstance(target, list) and target and isinstance(target[0], str):
        return target[0]
    stack = list(target) if isinstance(target, list) else [target]
    while stack:
        x = stack.pop(0)
        if isinstance(x, dict):
            if x.get("t") == "Image":
                c = x.get("c") or []
                if len(c) > 2 and isinstance(c[2], list) and c[2] and isinstance(c[2][0], str):
                    return c[2][0]
            for v in x.values():
                if isinstance(v, (list, dict)):
                    stack.append(v)
        elif isinstance(x, list):
            stack.extend(x)
    return ""


def _inline_text(inlines) -> str:
    """pandoc inlines → 带 ⟦MATH:…⟧⟧⟧ 占位的纯文本。"""
    out: List[str] = []

    def go(x):
        if isinstance(x, list):
            for it in x:
                go(it)
            return
        if not isinstance(x, dict):
            return
        t = x.get("t")
        if t == "Str":
            out.append(x.get("c", ""))
        elif t in ("Space", "SoftBreak", "LineBreak"):
            out.append(" ")
        elif t == "Math":
            out.append(schema.wrap_math(x.get("c", [None, ""])[1]))
        elif t == "Code":
            out.append(x.get("c", ["", ""])[1])
        elif t == "RawInline":
            out.append("")
        elif t == "Link":
            go(x.get("c", [[], [], []])[2])
        elif t == "Image":
            out.append("")
        elif t == "Cite":
            for it in x.get("c", []):
                if isinstance(it, dict) and it.get("t") == "Str":
                    out.append(it.get("c", ""))
        elif t == "Note":
            pass
        else:
            go(x.get("c", []))

    go(inlines)
    return re.sub(r"[ \t]{2,}", " ", "".join(out)).strip()


def _blocks_from_ast(blocks, base_dir: Path, asset_dir: Path, paper_id: str) -> List[Dict[str, Any]]:
    """pandoc 顶层块 → 论文块列表（图同时落成 PNG）。"""
    out: List[Dict[str, Any]] = []
    for b in blocks:
        t = b.get("t")
        if t == "Para":
            txt = _inline_text(b.get("c", []))
            if txt:
                out.append({"k": "p", "t": txt})
        elif t == "Header":
            pass  # 章节标题由调用方单独处理
        elif t == "BlockQuote":
            txt = _inline_text(b.get("c", []))
            if txt:
                out.append({"k": "quote", "t": txt})
        elif t in ("BulletList", "OrderedList"):
            items = []
            for item in b.get("c", []):
                s = _inline_text(item)
                if s:
                    items.append(s)
            if items:
                out.append({"k": "list", "ordered": t == "OrderedList", "items": items})
        elif t == "Figure":
            # Figure c = [attr, caption, target]
            #   caption = [plain, inlines]
            #   target  = **inline 节点列表**（图被包在 Plain→Image 里，
            #             不是 [url, title] 这种简写 —— 踩过，图全被丢掉）
            c = b.get("c") or []
            caption = c[1] if len(c) > 1 and isinstance(c[1], list) else []
            target = c[2] if len(c) > 2 and isinstance(c[2], list) else []
            src = _figure_url(target)
            ctext = _inline_text(caption[1]) if len(caption) > 1 else ""
            png = _materialize_figure(src, base_dir, asset_dir, paper_id)
            if png:
                out.append({"k": "fig", "src": png, "caption": ctext, "orig": src})
        elif t == "Table":
            rows = _table_rows(b)
            cap = _inline_text(b.get("c", [])[3]) if len(b.get("c", [])) > 3 else ""
            if rows:
                out.append({"k": "table", "rows": rows, "caption": cap})
        elif t == "Div":
            # ⚠ pandoc 的 Div c = [attr, blocks]（**只有 2 段**，不是 3 段！）
            #   之前按 3 段读 → Div 里的正文全被丢掉（定理、表格、图都在 Div 里）。
            inner = b.get("c") or []
            attr = inner[0] if len(inner) > 0 and isinstance(inner[0], list) else []
            cls = " ".join(attr[1] or []) if len(attr) > 1 else ""
            sub = _blocks_from_ast(inner[1] if len(inner) > 1 and isinstance(inner[1], list) else [],
                                   base_dir, asset_dir, paper_id)
            if "theorem" in cls or "lemma" in cls or "definition" in cls or "proposition" in cls:
                for sb in sub:
                    if sb["k"] == "p":
                        out.append({"k": "quote", "t": sb["t"], "kind": cls})
            else:
                out.extend(sub)
    return out


def _table_rows(b: Dict[str, Any]) -> List[List[str]]:
    """pandoc Table → 行×列的字符串矩阵。

    ⚠ pandoc 的 Table c 是 **6 段**：
        [attr, caption, colspecs, head, bodies, foot]
      head  = [attr, rows]，每个 body = [attr, rowhead, intermediate, rows]，
      每个 row = [attr, cells]，每个 cell = [attr, align, colspan, rowspan, content]。
      （之前按 2 段读 → 表全成了空行，2026-11-23 踩过。）
    """
    c = b.get("c") or []
    if len(c) < 5:
        return []
    rows: List[List[str]] = []

    def cell_text(cell):
        # cell = [attr, align, colspan, rowspan, content]
        if isinstance(cell, list) and len(cell) >= 5:
            return _inline_text(cell[4])
        return ""

    def add_rows(rowlist):
        for row in rowlist or []:
            cells = row[1] if isinstance(row, list) and len(row) > 1 else row
            if isinstance(cells, list):
                rows.append([cell_text(c) for c in cells])

    head = c[3]
    if isinstance(head, list) and len(head) > 1:
        add_rows(head[1])
    for body in (c[4] or []):
        if isinstance(body, list) and len(body) > 3:
            add_rows(body[3])
    return rows


def _materialize_figure(src: str, base_dir: Path, asset_dir: Path, paper_id: str) -> str:
    """图在源码里可能是 .pdf（浏览器渲染不了）→ 转 PNG 落到 asset_dir。"""
    if not src:
        return ""
    p = (base_dir / src).resolve()
    if not p.exists():
        return ""
    asset_dir.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^A-Za-z0-9_.-]", "_", Path(src).stem)[:60]
    if p.suffix.lower() == ".pdf":
        out = asset_dir / f"{paper_id}_{stem}.png"
        r = subprocess.run(["pdftoppm", "-png", "-r", "160", "-singlefile", str(p), str(out.with_suffix(""))],
                           capture_output=True, timeout=180)
        if r.returncode == 0 and out.exists():
            return f"assets/{out.name}"
        return ""
    out = asset_dir / f"{paper_id}_{stem}{p.suffix}"
    shutil.copy2(p, out)
    return f"assets/{out.name}"


def _sections_from_ast(blocks, base_dir: Path, asset_dir: Path, paper_id: str):
    """顶层块 → (sections, toc)。Header 开新节，其余进当前节。"""
    sections: List[Dict[str, Any]] = []
    toc: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    seq = 0
    for b in blocks:
        if b.get("t") == "Header":
            lvl, attr, inl = b["c"]
            title = _inline_text(inl) or attr[0]
            seq += 1
            sid = f"s{seq}"
            cur = schema.make_section(sid, title, max(1, min(6, int(lvl))))
            sections.append(cur)
            toc.append({"id": sid, "title": title, "level": cur["level"]})
            continue
        if cur is None:
            # 正文第一个块之前没有标题 → 建一个「正文」节
            seq += 1
            cur = schema.make_section(f"s{seq}", "正文", 1)
            sections.append(cur)
            toc.append({"id": cur["id"], "title": cur["title"], "level": 1})
        cur["blocks"].extend(_blocks_from_ast([b], base_dir, asset_dir, paper_id))
    return sections, toc


# ── 对外主入口 ────────────────────────────────────────────────
def import_arxiv(pid: str, asset_dir: Path, unwrap_cache: Optional[Path] = None) -> Dict[str, Any]:
    """arXiv ID → 论文文档（dict）。"""
    pid = pid.strip()
    m = re.search(r"(\d{4}\.\d{4,5})", pid) or re.search(r"([a-z-]+(?:\.[A-Z]{2})?/\d{7})", pid)
    if not m:
        raise ImportError_(f"认不出 arXiv 编号：{pid}")
    pid = m.group(1)

    meta = fetch_meta(pid)
    with tempfile.TemporaryDirectory(prefix="arx_") as td:
        work = Path(td)
        base = download_source(pid, work)
        main_tex = max(base.rglob("*.tex"), key=lambda p: p.stat().st_size)
        ast = _clean_and_convert(main_tex, work, base, unwrap_cache)
        blocks = ast.get("blocks", [])
        sections, toc = _sections_from_ast(blocks, base, asset_dir, pid)

        refs = []
        for b in blocks:
            if b.get("t") != "Div":
                continue
            c = b.get("c") or []
            attr = c[0] if len(c) > 0 and isinstance(c[0], list) else []
            if not attr or attr[0] != "refs":
                continue
            # ⚠ Div c = [attr, blocks] —— blocks 在 c[1]（不是 c[2]）
            inner = c[1] if len(c) > 1 and isinstance(c[1], list) else []

            def _entry_text(node):
                # csl-entry 是 Div 套 Div 套 Para —— 直接找顶层 Para 会一条都找不到
                if not isinstance(node, dict):
                    return ""
                if node.get("t") == "Para":
                    return _inline_text(node.get("c") or [])
                if node.get("t") == "Div":
                    c2 = node.get("c") or []
                    parts = c2[1] if len(c2) > 1 and isinstance(c2[1], list) else []
                    return " ".join(t for t in (_entry_text(x) for x in parts) if t)
                return ""

            for it in inner:
                txt = _entry_text(it)
                if txt:
                    refs.append({"text": txt})

    words = sum(len(schema.strip_math(b.get("t", "")).split())
                for s in sections for b in s["blocks"] if b["k"] == "p")
    n_formula = sum(1 for s in sections for b in s["blocks"] if b["k"] == "eq") + \
        sum(schema.wrap_math("").count("") + 1 for s in sections for b in s["blocks"]
            if b["k"] == "p" for _ in [0] for _ in [0])
    n_fig = sum(1 for s in sections for b in s["blocks"] if b["k"] == "fig")

    doc = schema.make_doc(
        pid,
        meta.get("title") or pid,
        authors=meta.get("authors", []),
        abstract=meta.get("abstract", ""),
        venue=meta.get("venue", ""),
        date=meta.get("date", ""),
        source={"type": "arxiv", "id": pid, "url": f"https://arxiv.org/abs/{pid}"},
        sections=sections,
        toc=toc,
        refs=refs,
        stats={"words": words, "blocks": sum(len(s["blocks"]) for s in sections),
               "figures": n_fig},
    )
    return doc
