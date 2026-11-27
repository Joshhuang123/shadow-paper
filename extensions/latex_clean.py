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
_DEF_RE = re.compile(r"\\(?:[A-Za-z]*[Cc]ommand)\s*\{")
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
        # ⚠️ 名字可能带空的默认参数：`\providecommand{\algorithmiccomment{}}{}`
        #    （2611.32606 实测踩中）—— 这种名字 pandoc 解析不了，整篇导入失败。
        #    → 名字里的 `{}` 剥掉再判断；实在不像宏名的，定义照样删掉
        #      （留着它 pandoc 一样报错），只是不登记成可展开的宏。
        mm = re.match(r"\s*\\([A-Za-z]+)\s*(\{\})?\s*$", name)
        if not mm:
            out.append("% macro definition removed\n")
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
        # ⚠️ 默认参数块（2026-11-27 实测，2611.32606）：
        #   \newcommand{\msg}[3][]{...#3...}
        #   —— 读完 [3] 后面还有一段 `[]`（第 1 个参数的默认值），
        #      不吃掉它就会**留在正文里**，pandoc 读到 `#3` 直接报错。
        #   默认块一定是方括号形式（函数体是花括号），所以贪心吃 [] 是安全的。
        while True:
            k = j
            while k < len(s) and s[k] in " \t\n":
                k += 1
            if k < len(s) and s[k] == "[":
                close = s.find("]", k)
                if close == -1:
                    break
                j = close + 1
                continue
            break
        while j < len(s) and s[j] in " \t\n":
            j += 1
        if j < len(s) and s[j] == "{":
            body, end2 = _match_group(s, j)
        else:
            body, end2 = "", j
        defs[mac] = (nargs, body)
        out.append("% " + f"macro {mac} removed\n")
        i = end2
    s = "".join(out)
    # LaTeX3 风格的定义（\NewDocumentCommand \name {m}{body}）单独再抠一遍
    s, l3_defs = extract_latex3_macros(s)
    for k, v in l3_defs.items():
        defs.setdefault(k, v)
    return s, defs


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
            # 宏体里的占位符换空（裸用法没有实参）；\xspace 之类「只在排版时起作用的空格宏」换成真空格
            if _is_layout_body(body):
                continue          # 排版宏：不展开
            plain = _expand_body(body, [])
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
                first = rest[:1]
                if not first:
                    return _p
                if first.isspace():
                    return _p          # 源码本来就有空格，不用补
                if first.isalpha():
                    # LaTeX 会**吃掉控制词后面的空格**（`\ours uses` 渲染成 "Coconutuses"），
                    # 所以这里补一个空格，把作者本意还原。
                    return _p + " "
                # 标点、下划线（`\cred_i` → "r_i"）等：紧贴着，不补
                return _p
            new_s, n = pat.subn(_repl, s)
            if n:
                changed = True
                s = new_s
        if not changed:
            break
    return s


def _expand_body(body: str, args) -> str:
    """把宏体里的 #1..#N 换成实参；没有对应实参的 #N 换成空串。

    ⚠️ 必须**按 N 从大到小**替换：先替 #1 会把 #10 的前半截吃掉
    （"#10" → "arg0"，#10 的后半截变成 "0"）。

    ⚠️ 宏体里**一个参数都没引用**时，实参要**接在体后面**（2026-11-27 实测）：
      宏包用 `\\DeclareTextFontCommand{\\textbf}{\\bfseries\\sffamily}` 重定义了 \\textbf ——
      这种命令的体里没有 `#1`，实参是**隐式**跟在体后面的（LaTeX 语义：
      `\\textbf{粗体}` = `<体> 粗体`）。
      按普通规则展开 → 实参被整段丢掉 → **正文里的话凭空消失**
      （实测 Coconut 论文里 "Latent reasoning in LLMs." 整句不见了）。
    """
    if args and not re.search(r"#[0-9]", body):
        # 体不引用任何参数 → 实参接在后面（\DeclareTextFontCommand 这类）
        body = body + " " + " ".join(a for a in args if a)
    out = body
    for i in range(10, 0, -1):
        out = out.replace(f"#{i}", args[i - 1] if i <= len(args) else "")
    return out


_L3_DEF_RE = re.compile(
    r"\\(?:New|Provide|Renew|Declare)Document(Command|Environment)\s*\\([A-Za-z@]+)\s*")


def _l3_arg_count(spec: str) -> int:
    """LaTeX3 参数说明 `{m}` / `{mm}` / `{m O{}}` → 实参个数（近似）。

    `m` = 必填、`O` = 可选、`s`/`e`/`x` 都不吃参数。
    只数顶层（不在花括号里的）m 和 O。
    """
    n = 0
    depth = 0
    for ch in spec:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif depth == 0 and ch in "mO":
            n += 1
    return max(1, n)


def extract_latex3_macros(s: str):
    """抠出 LaTeX3 风格的宏定义：`\\NewDocumentCommand \\hname { m } { ...#1... }`。

    为什么（2026-11-27 实测，2611.32606）：
      LaTeX3 的定义命令**宏名不带花括号**（`\\NewDocumentCommand \\hname {m}{body}`），
      老式 `\\newcommand{\\hname}` 的正则完全匹配不上 →
      定义留在正文里，pandoc 读到 `#1` 直接报 "unexpected #1"，整篇导不进来。
      抠出来登记成普通宏，调用点才能被正常展开（不然调用处的文字会丢）。
    """
    defs = {}
    out = []
    i = 0
    while True:
        m = _L3_DEF_RE.search(s, i)
        if not m:
            out.append(s[i:])
            break
        out.append(s[i:m.start()])
        j = m.end()
        # 参数说明 {m} / {m O{}}
        spec, j2 = _match_group(s, j)
        if spec is None:
            out.append(s[m.start():])
            i = m.end()
            continue
        j = j2
        while j < len(s) and s[j] in " \t\n":
            j += 1
        # 定义体 {body}
        body, j3 = _match_group(s, j) if j < len(s) and s[j] == "{" else (None, j)
        if body is None:
            out.append(s[m.start():j2])
            i = j2
            continue
        defs[m.group(2)] = (_l3_arg_count(spec), body)
        out.append(f"% macro {m.group(2)} removed\n")
        i = j3
    return "".join(out), defs


_AT_NAME_RE = re.compile(r"\\[A-Za-z]+@[A-Za-z@]*")
_DEF_CMD_RE = re.compile(r"\\(?:gdef|def|edef|xdef)\b")
_LET_CMD_RE = re.compile(r"\\let\b")


def _token_at(s: str, j: int):
    """取 s[j] 处的一个 TeX 记号（\\name 或单个字符），返回 (token, 结束位置)。"""
    m = re.match(r"\\[A-Za-z@]+|.", s[j:], re.S)
    if not m:
        return None, j
    return m.group(0), j + m.end()


def strip_internal_commands(s: str) -> str:
    """删掉**名字里带 @** 的命令（LaTeX 内部名，\\makeatletter 之后的类内部宏）。

    为什么（2026-11-27 实测，2611.32606）：
      pandoc 遇到 `unexpected @` 就整篇报错。
      这类命令来自类文件 / 排版代码（`\\gdef\\@author{...}`、`\\archer@teaser` …），
      **不是正文内容**。
      定义类的（\\gdef/\\def…）连同它的参数一起删，
      其余的只删命令本身（留个空格）。

    ⚠️ 只删定义类的参数体很关键：只删 `\\@author` 这个名字、
      把 `{...}` 留下，**作者信息就会变成正文出现在文章开头**。

    ⚠️ `\\let\\a@b\\c@d` 要**整条删**：两个操作数都没了，
      光杆的 `\\let` 会让 pandoc 报 "unexpected"（实测踩中）。
    """
    out = []
    i = 0
    n = len(s)
    while i < n:
        m = _LET_CMD_RE.match(s, i)
        if m:
            j = m.end()
            while j < n and s[j] in " \t\n":
                j += 1
            t1, j1 = _token_at(s, j)
            t2, j2 = _token_at(s, j1)
            if t1 and t2 and ("@" in t1 or "@" in t2):
                out.append(" ")
                i = j2
                continue
            out.append(m.group(0))
            i = m.end()
            continue
        m = _DEF_CMD_RE.match(s, i)
        if m:
            j = m.end()
            while j < n and s[j] in " \t\n":
                j += 1
            nm = _AT_NAME_RE.match(s, j)
            if nm:
                k = nm.end()
                while k < n and s[k] in " \t\n":
                    k += 1
                if k < n and s[k] == "{":
                    _, k2 = _match_group(s, k)
                    if k2 != -1:
                        k = k2
                out.append(" ")
                i = k
                continue
            out.append(m.group(0))
            i = m.end()
            continue
        m2 = _AT_NAME_RE.match(s, i)
        if m2:
            out.append(" ")
            i = m2.end()
            continue
        out.append(s[i])
        i += 1
    return "".join(out)


def strip_comments(s: str) -> str:
    """把 LaTeX 注释的内容清掉，但**保留一个 `%`**。

    为什么（2026-11-27 实测，2412.06769）：
      **注释里也会出现宏调用** —— 源码里有一行
      `%\\shibo{needs to be polished }`（作者的批注）。
      展开宏的时候，它会把「注释后面整段正文」当成宏的第二个实参吃掉 ——
      实测一整段正文（"Experimentally, Coconut successfully enhances…"）直接消失，
      而且**不报错**。所以顺序必须是：**先清注释，再展开宏**。

    ⚠️ 为什么是「留一个 %」而不是「整行删掉」（实测踩过）：
      注释可能**在一个多行参数里面**（比如 `\\caption{…}` 的参数跨了几行，
      中间夹一行注释）。把注释整行删掉 → 那一行变成**空行** →
      空行会截断多行参数 → pandoc 报 "unexpected }"。
      留一个 `%` 就既清掉了内容，又不会把行变空。

    ⚠️ `%` 前面要有空格：紧跟在命令后面（`\\foo%`）时，pandoc 不当它是注释，
    会把 `%` 当成命令 → "unexpected % ..."。

    `\\%`（转义百分号）要保留：前面是**偶数**个反斜杠才是注释起点。
    """
    out = []
    for line in s.split("\n"):
        res = []
        cut = False
        for ch in line:
            if ch == "%":
                nb = 0
                j = len(res) - 1
                while j >= 0 and res[j] == "\\":
                    nb += 1
                    j -= 1
                if nb % 2 == 0:
                    cut = True
                    break
            res.append(ch)
        if cut:
            if res and not res[-1].isspace():
                res.append(" ")
            res.append("%")
        out.append("".join(res))
    return "\n".join(out)


def _is_layout_body(body: str) -> bool:
    """宏体里带 @ / 条件跳转 → 这是**排版宏**（类文件里的），不要展开。

    为什么（2026-11-27 实测，2611.32606）：
      类文件里的 `\\maketitle` 定义体是
      `\\global\\let\\archer@printfrontmatter… \\ifarcher@abstractready … \\else … \\fi`。
      一旦展开，等于把**类内部代码注入正文** →
      里面的 @ 命令和 \\if/\\else 结构让 pandoc 整篇报错
      （unexpected @ / unexpected \\else）。
      排版宏的内容本来就不是正文，展开它没有收益 → 不展开，
      让 pandoc 当未知命令丢掉即可。
    """
    if "@" in body:
        return True
    if re.search(r"\\(?:if|ifx|ifnum|ifdim|else|fi)\b", body):
        return True
    return False


_DOCUMENT_RE = re.compile(r"\\begin\s*\{document\}")


def drop_preamble(s: str) -> str:
    """切掉 `\\begin{document}` 之前的整段（类宏、颜色定义、宏定义…）。

    为什么（2026-11-27 实测，2611.32606）：
      前言里全是排版代码（`\\patchcmd{\\l@section}{…\\@plus\\p@}…`、类的前置定义），
      pandoc 解析不了里面的 @ 命令和 \\if/\\else 结构 → 整篇报错。
      而前言**没有一句是正文**。

    顺序很关键：必须在**宏展开之后**切 ——
      否则正文里的宏调用会因为定义被切掉而丢内容
      （定义已经被就地展开进正文了，切掉的是"没展开过的副本"）。
    """
    m = _DOCUMENT_RE.search(s)
    if not m:
        return s
    return s[m.start():]


def expand_macro_uses(s, defs):
    """把 \\mac{arg} 就地展开成 arg（只处理已知的自定义宏）。

    ⚠️ **多参数宏必须把所有实参都取出来**（2026-11-27 实测，2611.32606）：
      以前只取第一个参数、其余的 #2..#N 一律清空 ——
      `\\newcommand{\\msg}[3][]{...\\textit{#3}...}` 这种，
      正文里的 #3 会**留在文本里**，pandoc 直接报 "unexpected #3"，
      而且就算能过，第二个实参的内容也会被悄悄丢掉（静默丢内容）。
    """
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
        nargs, body = defs[mac]
        brace = m.end() - 1
        args = []
        end = brace
        after_last = brace      # 最后一个实参结束处的原始位置（不吃掉后面的空格）
        # 依次取 nargs 个实参。
        # ⚠️ 下一个实参必须**紧挨着**：中间不能有空行。
        #    否则 `\foo{a}` 和后面某一段的 `{b}` 会被凑成一对，
        #    把中间整段正文吃掉（实测 2611.32606 / 2412.06769 都中过招）。
        for _ in range(max(1, nargs)):
            arg, end2 = _match_group(s, end)
            if arg is None:
                break
            args.append(arg)
            after_last = end2
            end = end2
            # 下一个实参前面可能有空格，但**不能跨空行**
            k = end
            while k < len(s) and s[k] in " \t":
                k += 1
            if k < len(s) and s[k] == "\n":
                if k + 1 < len(s) and s[k + 1] == "\n":
                    break            # 跨段了 → 后面那个 {…} 不是本宏的实参
                end = k + 1
            else:
                end = k
        if not args:
            out.append(s[i])
            i += 1
            continue
        if _is_layout_body(body):
            # 排版宏（类文件里的）：不展开 —— 展开会把类内部代码注进正文
            out.append(s[i:end])
            i = end
            continue
        out.append(_expand_body(body, args))
        i = after_last      # ← 用"最后一个实参结束处"，别把宏后面的空格吃掉
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


def strip_param_definitions(s: str) -> str:
    """删掉**带 #N 参数**的定义命令（如 `\\newtcolorbox{takeawaybox}[2][]{... #1 ...}`）。

    为什么（2026-11-27 实测，2611.32606）：
      pandoc 遇到 `unexpected #2` 就整篇报错。
      这类定义来自 tcolorbox / algorithmic / tikz 等宏包，命令名五花八门
      （newtcolorbox / algnewcommand / providecommand / newenvironment…），
      一个个加白名单永远加不完 ——
      判据改成「**定义体里带 #N 参数**」，凡是这种定义一律删掉。
      它只是排版定义（定义一个环境/盒子怎么画），删掉不影响正文语义。

    实现：用花括号配对扫描（正则处理不了嵌套），
    找到 `\\cmd{arg}[opt...]{body}` 且 body 里出现 `#数字` → 整段换成注释。
    """
    out = []
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c != "\\":
            out.append(c)
            i += 1
            continue
        m = re.match(r"\\[A-Za-z@]+", s[i:])
        if not m:
            out.append(c)
            i += 1
            continue
        cmd = m.group(0)
        j = i + len(cmd)
        # 参数：{arg}
        while j < n and s[j] in " \t":
            j += 1
        if j >= n or s[j] != "{":
            out.append(cmd)
            i += len(cmd)
            continue
        arg, j2 = _match_group(s, j)
        if arg is None:
            out.append(cmd)
            i += len(cmd)
            continue
        j = j2
        # 可选参数：[...]（可有多个）
        while True:
            k = j
            while k < n and s[k] in " \t":
                k += 1
            if k < n and s[k] == "[":
                close = s.find("]", k)
                if close == -1:
                    break
                j = close + 1
                continue
            break
        # 定义体：{...}
        while j < n and s[j] in " \t":
            j += 1
        if j >= n or s[j] != "{":
            out.append(cmd)
            i += len(cmd)
            continue
        body, j3 = _match_group(s, j)
        if body is None:
            out.append(cmd)
            i += len(cmd)
            continue
        if re.search(r"#[0-9]", body):
            out.append(" ")
            i = j3
            continue
        out.append(cmd)
        i += len(cmd)
    return "".join(out)


def clean_tex(src: Path, out: Path, base_dir: Path | None = None):
    s = src.read_text(encoding="utf-8", errors="replace")
    stats = {}
    s = strip_opt_args(s)
    stats["opt_arg"] = 1
    # ⚠️ 先清注释（2026-11-27 实测）：注释里也可能有宏调用（`%\shibo{...}`），
    #    展开宏时会把注释后面的整段正文当成实参吃掉 —— 不报错，只是正文凭空少一段。
    s = strip_comments(s)
    # ⚠️ \input 进来的文件必须**在宏展开之前**内联（2026-11-27 实测，2611.32606）：
    #   论文正文常拆成几十个 \input{...}，宏定义在主文件、用法在子文件。
    #   如果最后才内联 → 子文件里的宏调用**从没被展开过** →
    #   pandoc 把它们当未知命令丢掉 → 正文里少词（\cred、\ours 这类）。
    s = inline_includes(s, base_dir or src.parent)
    # 本地 .sty/.cls 先处理：pandoc 去啃它们会把题名块搅乱
    s, harvested = neutralize_local_packages(s, base_dir or src.parent)
    stats["pkg_macros"] = len(harvested)
    s, defs = extract_macros(s)
    stats["macros"] = sorted(defs)
    s = expand_macro_uses(s, defs)
    # 裸用法（\ours: Chain of Continuous Thought）也必须展开，
    # 否则 pandoc 会把这个词连同方法名一起吃掉 —— 见 expand_bare_macro_uses 的说明。
    s = expand_bare_macro_uses(s, defs)
    # 带 #N 参数的定义命令（\newtcolorbox{...}[2][]{... #1 ...}）→ pandoc 直接报错
    s = strip_param_definitions(s)
    # 名字带 @ 的内部命令（\@author、\archer@teaser）→ pandoc 报 "unexpected @"
    s = strip_internal_commands(s)
    s = re.sub(r"\\pdfoutput\s*=\s*\d+\s*", "", s)
    s = re.sub(r"\\makeatletter|\\makeatother", "", s)
    # natbib 的 \citet/\citep/\citealt → \cite（引文统一交给 --bibliography）
    s = re.sub(r"\\(citet|citealp|citealt|citeauthor)\b", r"\\cite", s)
    # 前言（\begin{document} 之前）整段切掉：全是排版代码，pandoc 解析不了
    s = drop_preamble(s)
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
