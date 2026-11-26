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
import time
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


def _get_range(url: str, timeout: int = 120, chunk: int = 512 * 1024) -> bytes:
    """按 HTTP Range 分块下载并**校验完整性**。

    为什么需要它（2026-11-24 实测）：
      arxiv.org 的 e-print（源码包）和 PDF 在弱网下**整包连接会被截断**，
      一次 urlopen().read() 拿到的是半截数据 —— 而 tarfile.is_tarfile()
      只看文件头、照样 True，于是静默解出一半、导进来一篇只有附录的「论文」。
      整包重试十次都可能断；按 512KB 一段 Range 拉、拼起来对长度，一次就成。

    流程：
      ① 先 GET 拿真实 URL（arxiv 的 /e-print 会 302）和 Content-Length
      ② 服务端支持 Range（Accept-Ranges: bytes）就分块拉，逐块校验长度
      ③ 不支持 Range 就退回整包 GET，但仍**比对长度**，对不上就报错
    """
    # ① 解析真实 URL 与大小
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        real = r.geturl()
        try:
            total = int(r.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            total = 0
        accepts = (r.headers.get("Accept-Ranges") or "").lower()
        if not total:
            data = r.read()
            return data
    if "bytes" not in accepts:
        # 不支持 Range：整包拉，但要对长度
        data = _get(real, timeout=timeout)
        if total and len(data) != total:
            raise ImportError_(
                f"下载不完整（{len(data)}/{total} 字节）——重试即可")
        return data

    # ② 分块拉
    out = bytearray()
    pos = 0
    while pos < total:
        end = min(pos + chunk, total) - 1
        last = None
        for attempt in range(5):
            try:
                rq = urllib.request.Request(
                    real, headers={**{"User-Agent": UA}, "Range": f"bytes={pos}-{end}"})
                with urllib.request.urlopen(rq, timeout=timeout) as rr:
                    data = rr.read()
                if len(data) != (end - pos + 1):
                    raise IOError(f"块长度 {len(data)} != {end - pos + 1}")
                out += data
                pos = end + 1
                last = None
                break
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(1.5 * (attempt + 1))
        if last is not None:
            raise ImportError_(f"分块下载失败（{pos}/{total}）：{last!r}"[:180])
    if len(out) != total:
        raise ImportError_(f"下载不完整（{len(out)}/{total} 字节）——重试即可")
    return bytes(out)


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
        raw = _get_range(EPRINT.format(pid=pid), timeout=180)
    except Exception as e:
        raise ImportError_(f"arXiv 源码下载失败（{pid}）：{e}") from e
    # ⚠️ 下载完整性必须校验（2026-11-24 实测踩中）：
    #    arxiv.org 的 e-print 在弱网下会被**截断**，而 tarfile.is_tarfile()
    #    只看文件头、照样返回 True —— 于是 extractall 静默解出一半，
    #    剩下的 .tex 缺失，最后「最大的 .tex」挑中了附录，
    #    **整篇论文被当成只有附录的论文导进去，而且报 success**。
    #    现在：先按 Content-Length 对长度，再让 tar 自己走完整个归档。
    try:
        tarball.write_bytes(raw)
    except Exception as e:
        raise ImportError_(f"arXiv 源码落盘失败（{pid}）：{e}") from e

    ex = workdir / "src"
    ex.mkdir(parents=True, exist_ok=True)
    # arXiv 的包有三种形态：.tar.gz / .gz(单文件) / 裸 PDF
    if tarfile.is_tarfile(tarball):
        try:
            with tarfile.open(tarball) as tf:
                members = tf.getmembers()          # ← 走完整个归档，截断会抛
                try:
                    # Python 3.12+ 有 filter（防路径穿越）；3.9 没有 —— 退一步即可
                    tf.extractall(ex, filter="data")
                except TypeError:
                    tf.extractall(ex)
        except (tarfile.TarError, EOFError, OSError) as e:
            raise ImportError_(
                f"arXiv 源码包不完整（{pid}，已解出 {len(members) if 'members' in dir() else 0} 项）：{e}；"
                "通常是下载被截断，重试即可") from e
    else:
        try:
            import gzip
            (ex / "main.tex.gz").write_bytes(tarball.read_bytes())
            with gzip.open(ex / "main.tex.gz") as g:
                (ex / "main.tex").write_bytes(g.read())
        except OSError:
            (ex / "main.tex").write_bytes(tarball.read_bytes())

    if not list(ex.rglob("*.tex")):
        raise ImportError_(f"源码包里没有 .tex：{pid}")
    # ⚠️ 返回**解包根目录**，不是「最大的 .tex 所在的目录」（2026-11-24 实测）：
    #    多文件论文里最大的 .tex 常在附录子目录里（SELR 的 sec/7_suppl.tex 最大），
    #    以前返回它自己的目录 → 后面 find_main_tex 只在那个子目录里找，
    #    于是「带 \documentclass 的主文件」根本不在搜索范围内 → 整篇被当成只有附录；
    #    同时图片在根目录、相对路径解析不到 → 图数 0。
    return ex


def find_main_tex(base: Path) -> Path:
    """挑**真正的主文件**：带 \documentclass 的那个。

    为什么不能只挑最大的 .tex（2026-11-24 实测）：
    多文件论文里附录常常比正文还长（SELR 的 sec/7_suppl 就是），
    挑错主文件 → 整篇论文被当成只有附录。
    找不到带 documentclass 的，退回「最大的那个」保持旧行为。
    """
    texs = sorted(base.rglob("*.tex"), key=lambda p: -p.stat().st_size)
    if not texs:
        raise ImportError_("源码包里没有 .tex")
    for t in texs:
        try:
            head = t.read_text(encoding="utf-8", errors="replace")[:4000]
        except OSError:
            continue
        if "\\documentclass" in head:
            return t
    return texs[0]


# ── LaTeX 清洗 + pandoc 收敛 ─────────────────────────────────
def _neutralize_bibliography(tex: str) -> str:
    """把 tex 里的 \\bibliography{...} / \\addbibresource{...} 中和掉。

    pandoc 的 LaTeX reader 会去解析 \\bibliography 写的那�� bib：
    文件不存在 → "not found in resource path"；语法读不了 → 直接报错。
    参考文献由 --bibliography 显式提供，tex 里那条命令留着只会添乱。
    """
    tex = re.sub(r"\\\\bibliography\s*\{[^}]*\}", "% bibliography neutralized", tex)
    tex = re.sub(r"\\\\addbibresource\s*\{[^}]*\}", "% addbibresource neutralized", tex)
    tex = re.sub(r"\\\\printbibliography", "% printbibliography neutralized", tex)
    return tex


def _is_bib_error(stderr: str) -> bool:
    low = (stderr or "").lower()
    return ("bibliography" in low or "citeproc" in low or "citation" in low
            or ".bib" in low)


def _run_pandoc(clean_path: Path, base_dir: Path, bib, with_citeproc: bool):
    cmd = ["pandoc", str(clean_path), "-f", "latex", "-t", "json"]
    if with_citeproc:
        cmd.append("--citeproc")
        if bib:
            cmd.append(f"--bibliography={bib.resolve()}")
    return subprocess.run(cmd, cwd=base_dir, capture_output=True, text=True, timeout=900)


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
    # ── 参考文献要「可失败」──（2026-11-24 实测）
    # ① tex 里的 \bibliography{xxx} 会让 pandoc **去找它写的那个 bib**：
    #    找不到就报 "File 'xxx.bib' not found in resource path"（2512.21711 就是这样，
    #    它包里根本没有那个 bib），bib 本身语法 pandoc 读不了也直接报解析错（2608.13570）。
    #    → 先把这条命令**中和掉**，bib 由 --bibliography 显式给。
    # ② bib 读不了时不能整篇导入失败：正文才是本体，参考文献可以没有。
    #    → 退回「不带 --citeproc」再跑一遍，正文照常进，refs 为空。
    for _ in range(30):
        # base_dir 要传**解包根目录**：本地 .sty/.cls 往往在根上而不是主 tex 旁边，
        # 不传就找不到 → pandoc 会自己去啃这些 .sty → 题名块被搅乱
        # （正文开头多出「maketitle thanks aketitle」，2026-11-26 实测）。
        latex_clean.clean_tex(tex_path, clean_path, base_dir)
        s = clean_path.read_text(encoding="utf-8")
        for mac in unwrap:
            s = latex_clean.expand_named_macro(s, mac)
        s = _neutralize_bibliography(s)
        clean_path.write_text(s, encoding="utf-8")

        p = _run_pandoc(clean_path, base_dir, bib, with_citeproc=True)
        if p.returncode != 0 and _is_bib_error(p.stderr):
            # bib 读不了 → 不带 citeproc 再来一次，正文优先
            p = _run_pandoc(clean_path, base_dir, None, with_citeproc=False)
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
            c = b.get("c") or []
            cap = _inline_text(c[1][1]) if len(c) > 1 and isinstance(c[1], list) and len(c[1]) > 1 else ""
            # 论文没写表题时，pandoc 会拿**列名**自动生成一个表题，
            # 拼出来是 "RoleTask skillsCoordinationSupply"（没空格，重复表头）→ 丢掉。
            if rows and cap.replace(" ", "") == "".join(rows[0]):
                cap = ""
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
        main_tex = find_main_tex(base)
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
