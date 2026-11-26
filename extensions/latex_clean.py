#!/usr/bin/env python3
"""arXiv LaTeX 源码 → pandoc 能读的清洗（第 2 版：花括号配对扫描）。

pandoc 的 LaTeX reader 比真 LaTeX 弱，三类东西会让它直接报错：
  1. 带可选参数的调用：\\label[appendix]{x}、\\cite[see][p.~3]{key}、\\includegraphics[width=..]{f}
  2. 自定义宏：\\newcommand{\\fixblue}[1]{...} / \\DeclareRobustCommand{\\x}[1]{...}
     —— pandoc 不认识，**遇到未知宏会一路吃到文件尾**，报 "unexpected end of input"
  3. \\pdfoutput=1 之类只在真 LaTeX 下有意义的指令
另外 \\input / \\include 的子文件必须内联（pandoc 不会自己去找）。

做法：
  · 用**花括号深度扫描**取出宏的完整定义体（正则做不到：定义体里自带花括号）
  · 把每个自定义宏的**调用点**就地展开成它的参数（\\fixblue{X} → X），
    顺带把定义删掉 —— 这样 pandoc 眼里只剩标准 LaTeX
  · \\cite / \\citep / \\citet 换成 pandoc 认识的 \\cite（引文用 --bibliography 解析）
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import List


# ── 花括号配对扫描 ────────────────────────────────────────────
def _match_group(s, i):
    """s[i] 必须是 '{'，返回 (内容, 结束位置)。找不到配对返回 (None, -1)。"""
    assert s[i] == "{", "调用方要传 '{' 的位置"
    depth = 0
    j = i
    while j < len(s):
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return s[i + 1:j], j + 1
        j += 1
    return None, -1


def _find_matching_brace(s, start):
    """start 指向 '{'，返回配对 '}' 的下标。"""
    _, end = _match_group(s, start)
    return end - 1 if end > 0 else -1


# ── 宏定义抽取与展开 ──────────────────────────────────────────
_DEF_RE = re.compile(r"\\(?:newcommand|renewcommand|DeclareRobustCommand)\s*\{")
_USE_RE = re.compile(r"\\([A-Za-z]+)\s*\{")


def extract_macros(s):
    """把自定义宏定义抠出来，返回 (去掉定义的正文, {宏名: 参数个数})。"""
    defs = {}
    out = []
    i = 0
    while True:
        m = _DEF_RE.search(s, i)
        if not m:
            out.append(s[i:])
            break
        out.append(s[i:m.start()])
        brace = s.index("{", m.end() - 1)
        name, end = _match_group(s, brace)
        if name is None:
            out.append(s[m.start():])
            break
        # \newcommand{\name}[nargs]{body} —— name 形如 \fixblue
        mm = re.match(r"\s*\\([A-Za-z]+)\s*$", name)
        if not mm:
            out.append(s[m.start():end])
            i = end
            continue
        mac = mm.group(1)
        j = end
        nargs = 1
        # 可选参数 [n]
        while j < len(s) and s[j] in " \t":
            j += 1
        if j < len(s) and s[j] == "[":
            k = s.index("]", j)
            nargs = int(s[j + 1:k]) if s[j + 1:k].isdigit() else 1
            j = k + 1
        while j < len(s) and s[j] in " \t\n":
            j += 1
        if j < len(s) and s[j] == "{":
            body, end2 = _match_group(s, j)
        else:
            body, end2 = "", j
        defs[mac] = (nargs, body)
        out.append("% " + f"macro {mac} removed\n")
        i = end2
    return "".join(out), defs


def expand_named_macro(s, mac):
    """把指定名字的宏 \\mac{arg} / \\mac*{arg} 展开成 arg（pandoc 不认它时用）。

    ⚠️ 展开后**两侧要补空格**：否则 `\\quad` + `app:welfare-proofs` 会粘成
    一个不存在的宏 `\\quadapp`（pandoc 直接报错，2026-11-21 踩过）。
    """
    pat = re.compile(r"\\" + re.escape(mac) + r"\*?\s*\{")
    i = 0
    out = []
    while True:
        m = pat.search(s, i)
        if not m:
            out.append(s[i:])
            break
        out.append(s[i:m.start()])
        brace = s.index("{", m.start())
        arg, end = _match_group(s, brace)
        if arg is None:
            out.append(s[m.start():m.end()])
            i = m.end()
            continue
        out.append(" " + _humanize_ref(arg) + " ")
        i = end
    # 无参用法（\\quad / \\noindent / \\nobreak …）——这类宏后面不跟花括号，
    # 直接删掉（pandoc 不认，留着会报错）
    s = "".join(out)
    s = re.sub(r"\\" + re.escape(mac) + r"(?![A-Za-z*])\s*(?![{])", " ", s)
    return s


def _humanize_ref(key):
    """把引用键变成人能读的文字：app:welfare-proofs → welfare proofs。"""
    k = key.strip()
    if not k:
        return k
    k = k.split(":")[-1]
    k = k.replace("-", " ").replace("_", " ")
    return re.sub(r"\s+", " ", k).strip()


def expand_bare_macro_uses(s, defs):
    """展开**不带花括号**的自定义宏用法：\\ours → \\textsc{Coconut}。

    ⚠️ 这一段是 2026-11-26 补的，漏了它会**静默丢词**：
      extract_macros 把宏定义删了，expand_macro_uses 只管 `\\ours{...}` 这种带参调用，
      而 `\\section{\\ours: Chain of Continuous Thought}` 这种**裸用法**没人管。
      pandoc 不认识 `\\ours`，会把它连同后面的内容一起吃掉 ——
      结果整篇论文里 "Coconut" 这个方法名消失了，连章节标题都变成
      「: Chain of Continuous Thought」（前面凭空多一个冒号）。

    规则：
      · 只替换**完整命令名**（`\\ours` 后面不能跟字母），避免误伤 `\\oursfoo`
      · 支持星号形式 `\\ours*`
      · 宏体里的 #1 / #2 一律换成空（裸用法本来就没有实参）
      · 宏体里可能还有别的裸宏（`\\ours` → `\\textbf{\\coconut}`）→ 迭代到收敛
    """
    if not defs:
        return s
    for _ in range(5):
        changed = False
        for mac, (nargs, body) in defs.items():
            # 宏体里的占位符换空；\xspace 之类「只在排版时起作用的空格宏」换成真空格
            plain = body
            for i in range(1, max(nargs, 3) + 1):
                plain = plain.replace(f"#{i}", "")
            plain = re.sub(r"\\(?:xspace|hspace\{[^}]*\})\b", " ", plain)
            plain = plain.rstrip()      # 尾部空格交给下面的分隔符逻辑决定
            # \\mac 后面**不能**跟 `{`（带参调用由 expand_macro_uses 处理），
            # 也不能跟字母（避免 \\oursfoo 误伤）
            pat = re.compile(r"\\" + re.escape(mac) + r"(?![A-Za-z])\*?(?!\s*\{)")
            # 展开后要不要补空格：LaTeX 里 \\ours: 渲染成 "Coconut:"（冒号前不留空格），
            # 而 \\ours (Chain) 要留空格 —— 看后一个字符是不是标点；
            # 源码本来就有空格就别再补一个（否则会变成两个空格）。
            def _repl(_m, _p=plain, _src=s):
                rest = _src[_m.end():]
                nxt = rest.lstrip()
                if not nxt or nxt[0] in ",.;:!?)]}'":
                    return _p
                if rest[:1] in (" ", "\n", "\t"):
                    return _p
                return _p + " "
            new_s, n = pat.subn(_repl, s)
            if n:
                changed = True
                s = new_s
        if not changed:
            break
    return s


def expand_macro_uses(s, defs):
    """把 \\mac{arg} 就地展开成 arg（只处理已知的自定义宏）。"""
    if not defs:
        return s
    i = 0
    out = []
    while i < len(s):
        m = _USE_RE.match(s, i)
        if not m or m.group(1) not in defs:
            out.append(s[i])
            i += 1
            continue
        mac = m.group(1)
        brace = m.end() - 1
        arg, end = _match_group(s, brace)
        if arg is None:
            out.append(s[i])
            i += 1
            continue
        # 宏体里的 #1 → 第一个参数
        nargs, body = defs[mac]
        if nargs >= 1:
            body = body.replace("#1", arg)
            if nargs >= 2:
                body = re.sub(r"#2", "", body)
        out.append(body)
        i = end
    return "".join(out)


# ── 可选参数剥离 ─────────────────────────────────────────────
_OPT_ARG_RE = re.compile(r"(\\[A-Za-z]+)\s*\[(?:[^\[\]\\]|\\.)*\]")


def strip_opt_args(s):
    """\\label[appendix]{x} → \\label{x}；\\cite[a][b]{k} → \\cite{b}。"""
    s = _OPT_ARG_RE.sub(lambda m: m.group(1), s)
    # \includegraphics[width=..]{f} 这类：宽度/高度参数丢掉（图文无关紧要）
    s = re.sub(r"(\\includegraphics)\s*\[[^\]]*\]", r"\1", s)
    return s


# ── \input / \include 内联 ────────────────────────────────────
def inline_includes(s, base_dir):
    for _ in range(8):
        m = re.search(r"\\(?:input|include)\s*\{([^}]+)\}", s)
        if not m:
            break
        rel = m.group(1)
        cand = None
        for p in (base_dir / rel, base_dir / (rel + ".tex"), base_dir / rel / "main.tex"):
            if p.exists():
                cand = p
                break
        if cand is None:
            s = s[:m.start()] + "% MISSING INCLUDE " + rel + s[m.end():]
            continue
        s = s[:m.start()] + cand.read_text(encoding="utf-8", errors="replace") + s[m.end():]
    return s


# ── 本地 .sty / .cls 中和 ─────────────────────────────────────
_PKG_RE = re.compile(r"\\(usepackage|documentclass)(\[[^\]]*\])?\{([^}]*)\}")
_DEF_NAME_RE = re.compile(
    r"\\(?:newcommand|renewcommand|DeclareRobustCommand)\s*\{\s*\\([A-Za-z@]+)")


def _defined_names(s: str):
    return {m.group(1) for m in _DEF_NAME_RE.finditer(s)}


def _harvest_pkg_macros(path: Path, already: set):
    """把 .sty/.cls 里的 \\newcommand/\\renewcommand 定义抠出来，返回可重放的定义行。

    只认 newcommand / renewcommand / DeclareRobustCommand —— \\def 不认：
    ICLR 模板里是 `\\def\\maketitle{...}`，那是**排版命令**，搬进来会把
    整个题名块替换成一大坨 TeX 源码（比残渣更糟）。
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    _, defs = extract_macros(text)
    lines = []
    for mac, (nargs, body) in defs.items():
        name = mac.lstrip("\\")
        if name in already:
            continue
        already.add(name)
        opt = f"[{nargs}]" if nargs and nargs > 1 else ""
        lines.append(f"\\newcommand{{\\{name}}}{opt}{{{body}}}")
    return lines


def neutralize_local_packages(s: str, base_dir: Path | None):
    """把指向**本地 .sty/.cls** 的 \\usepackage / \\documentclass 中和掉。

    为什么必须做（2026-11-26 实测，ICLR 模板两篇都中招）：
      pandoc 会自己去解析 \\usepackage{xxx} 指向的 .sty 文件。
      iclr2026_conference.sty 里有 `\\def\\maketitle{...}`、各种 `\\def\\section{...}`，
      pandoc 解析到一半失败 → **题名块被搅乱**，正文开头多出一段
      「maketitle thanks aketitle」，还会凭空多出一节叫「正文」。
      2604.12493 / 2603.09184 两篇都这样。

    做法：
      · 本地存在的 .sty/.cls → 先把里面的宏定义搬进 tex（宏不丢），
        再把引用换成注释 —— pandoc 就不会再碰这些文件；
      · 本地不存在的（amsmath 这类系统包）→ 原样留着，
        pandoc 也找不到它们，不存在风险。
    """
    if not base_dir:
        return s, []
    already = _defined_names(s)
    harvested: List[str] = []

    def _one(m):
        kind, opts, names = m.group(1), m.group(2) or "", m.group(3)
        keep, notes = [], []
        for name in [x.strip() for x in names.split(",") if x.strip()]:
            f = None
            for cand in (base_dir / f"{name}.sty", base_dir / f"{name}.cls",
                         base_dir / name / f"{name}.sty", base_dir / name / f"{name}.cls"):
                if cand.exists():
                    f = cand
                    break
            if f is None:
                keep.append(name)          # 系统包：pandoc 也找不到，原样留着
                continue
            harvested.extend(_harvest_pkg_macros(f, already))
            notes.append(f"% local package neutralized: {name}")
        if keep:
            notes.append(f"\\{kind}{opts}{{{','.join(keep)}}}")
        return "\n".join(notes)

    s = _PKG_RE.sub(_one, s)
    if harvested:
        s = "\n".join(harvested) + "\n" + s
    return s, harvested


def clean_tex(src: Path, out: Path, base_dir: Path | None = None):
    s = src.read_text(encoding="utf-8", errors="replace")
    stats = {}
    s = strip_opt_args(s)
    stats["opt_arg"] = 1
    # 本地 .sty/.cls 先处理：pandoc 去啃它们会把题名块搅乱
    s, harvested = neutralize_local_packages(s, base_dir or src.parent)
    stats["pkg_macros"] = len(harvested)
    s, defs = extract_macros(s)
    stats["macros"] = sorted(defs)
    s = expand_macro_uses(s, defs)
    # 裸用法（\ours: Chain of Continuous Thought）也必须展开，
    # 否则 pandoc 会把这个词连同方法名一起吃掉 —— 见 expand_bare_macro_uses 的说明。
    s = expand_bare_macro_uses(s, defs)
    s = re.sub(r"\\pdfoutput\s*=\s*\d+\s*", "", s)
    s = re.sub(r"\\makeatletter|\\makeatother", "", s)
    # natbib 的 \citet/\citep/\citealt → \cite（引文统一交给 --bibliography）
    s = re.sub(r"\\(citet|citealp|citealt|citeauthor)\b", r"\\cite", s)
    s = inline_includes(s, base_dir or src.parent)
    out.write_text(s, encoding="utf-8")
    return stats, defs


if __name__ == "__main__":
    import sys
    src = Path(sys.argv[1])
    out = Path(sys.argv[2])
    stats, defs = clean_tex(src, out)
    print("清洗统计:", {k: (v if not isinstance(v, list) else len(v)) for k, v in stats.items()})
    print("宏:", stats["macros"])


def macro_at(text, line, col):
    """在 (line, col) 往前找最近的 \\command（带不带 * 都算）——收敛循环用来定位「pandoc 不认的宏」。"""
    lines = text.split("\n")
    if line - 1 >= len(lines):
        return None
    ln = lines[line - 1]
    head = ln[:col]
    ms = re.findall(r"\\([A-Za-z]+)\*?\s*\{?", head)
    if not ms:
        for back in range(1, 6):
            if line - 1 - back < 0:
                break
            ms = re.findall(r"\\([A-Za-z]+)\*?\s*\{?", lines[line - 1 - back])
            if ms:
                break
    return ms[-1] if ms else None
