"""LaTeX 清洗器的单元测试（全部离线，不打网、不调 pandoc）。

清洗器解决的是 pandoc 的三类硬伤：
  1. 带可选参数的调用（\\label[appendix]{x}）→ 直接报错
  2. 自定义宏 → pandoc 遇到不认识的宏会一路吃到文件尾，报 "unexpected end of input"
  3. \\input / \\include 的子文件必须内联
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from extensions import latex_clean


class TestStripOptArgs(unittest.TestCase):
    def test_label_optional_arg(self):
        s = r"\label[appendix]{app:proofs}"
        self.assertEqual(latex_clean.strip_opt_args(s), r"\label{app:proofs}")

    def test_cite_two_optional_args(self):
        s = r"\cite[see][p.~3]{ord2026}"
        out = latex_clean.strip_opt_args(s)
        self.assertTrue(out.startswith(r"\cite"))
        self.assertIn("ord2026", out)

    def test_includegraphics_width_dropped(self):
        s = r"\includegraphics[width=0.8\textwidth]{fig/a.pdf}"
        out = latex_clean.strip_opt_args(s)
        self.assertIn("{fig/a.pdf}", out)
        self.assertNotIn("width", out)

    def test_normal_command_untouched(self):
        s = r"\textbf{hello} \emph{world}"
        self.assertEqual(latex_clean.strip_opt_args(s), s)


class TestMacroHandling(unittest.TestCase):
    def test_newcommand_single_line_extracted(self):
        src = r"\newcommand{\fixblue}[1]{{\color{blue}#1}} hello \fixblue{world}"
        body, defs = latex_clean.extract_macros(src)
        self.assertIn("fixblue", defs)
        self.assertNotIn(r"\newcommand", body)

    def test_newcommand_multiline_extracted(self):
        src = "\\newcommand{\\x}[1]{%\n  #1%\n}\nbody"
        body, defs = latex_clean.extract_macros(src)
        self.assertIn("x", defs)
        self.assertNotIn(r"\newcommand", body)

    def test_expand_macro_use(self):
        defs = {"fixblue": (1, r"{{\color{blue}#1}}")}
        out = latex_clean.expand_macro_uses(r"a \fixblue{b} c", defs)
        self.assertIn("b", out)
        self.assertNotIn(r"\fixblue", out)

    def test_expand_named_macro_adds_space(self):
        # ⚠ 回归测试：不补空格的话 \quad + app:xxx 会粘成 \quadapp:xxx（pandoc 直接报错）
        out = latex_clean.expand_named_macro(r"\quad app:welfare-proofs", "quad")
        self.assertNotIn(r"\quadapp", out)
        self.assertIn("app:welfare-proofs", out)

    def test_humanize_ref(self):
        self.assertEqual(latex_clean._humanize_ref("app:welfare-proofs"), "welfare proofs")
        self.assertEqual(latex_clean._humanize_ref("sec:intro"), "intro")


class TestBraceScanner(unittest.TestCase):
    def test_match_group_simple(self):
        content, end = latex_clean._match_group("{abc}rest", 0)
        self.assertEqual(content, "abc")
        self.assertEqual(end, 5)

    def test_match_group_nested(self):
        content, end = latex_clean._match_group("{a{b}c}rest", 0)
        self.assertEqual(content, "a{b}c")
        self.assertEqual(end, 7)

    def test_match_group_unbalanced(self):
        content, end = latex_clean._match_group("{abc", 0)
        self.assertIsNone(content)


class TestIncludeInlining(unittest.TestCase):
    def test_include_inlined(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "sub.tex").write_text("SUB CONTENT", encoding="utf-8")
            s = latex_clean.inline_includes("before \\input{sub.tex} after", base)
            self.assertIn("SUB CONTENT", s)
            self.assertNotIn(r"\input", s)

    def test_missing_include_marked(self):
        with tempfile.TemporaryDirectory() as td:
            s = latex_clean.inline_includes(r"\input{nope.tex}", Path(td))
            self.assertIn("MISSING INCLUDE", s)


class TestMacroAt(unittest.TestCase):
    def test_finds_macro_before_column(self):
        text = "line one\nsome \\nameref*{key} text"
        self.assertEqual(latex_clean.macro_at(text, 2, 14), "nameref")


class TestBareMacroExpansion(unittest.TestCase):
    """裸宏（不带花括号的自定义宏）必须展开 —— 2026-11-26 实测踩中。

    真实案例：\\newcommand{\\ours}{\\textsc{Coconut}\\xspace}，
    源码里写成 \\section{\\ours: Chain of Continuous Thought}。
    不展开的话 pandoc 不认识 \\ours，会把它连同后面的内容一起吃掉，
    结果整篇论文里方法名 "Coconut" 消失，标题变成「: Chain of Continuous Thought」。
    """

    DEFS = {"ours": (1, r"\textsc{Coconut}")}

    def test_bare_use_expanded(self):
        out = latex_clean.expand_bare_macro_uses(r"\section{\ours: Chain of Continuous Thought}", self.DEFS)
        self.assertNotIn(r"\ours", out)
        self.assertIn("Coconut", out)

    def test_no_space_before_punctuation(self):
        out = latex_clean.expand_bare_macro_uses(r"\ours: Chain", self.DEFS)
        self.assertNotIn(r"\ours", out)
        self.assertIn("}: Chain", out)      # 冒号前不留空格

    def test_space_before_bracket(self):
        out = latex_clean.expand_bare_macro_uses(r"paradigm, \ours (Chain)", self.DEFS)
        self.assertIn("} (Chain)", out)     # 括号前留一个空格，且不重复

    def test_no_double_space(self):
        out = latex_clean.expand_bare_macro_uses(r"\ours (Chain)", self.DEFS)
        self.assertNotIn("  ", out)

    def test_braced_use_left_to_other_handler(self):
        out = latex_clean.expand_bare_macro_uses(r"\ours{arg}", self.DEFS)
        self.assertIn(r"\ours{arg}", out)   # 带参调用不归这里管

    def test_prefix_command_not_matched(self):
        out = latex_clean.expand_bare_macro_uses(r"\oursfoo and \ours", self.DEFS)
        self.assertIn(r"\oursfoo", out)
        self.assertNotIn(r"\ours ", out.replace(r"\oursfoo", ""))


class TestLocalPackageNeutralization(unittest.TestCase):
    """本地 .sty/.cls 必须中和 —— 2026-11-26 实测踩中。

    pandoc 会自己去解析 \\usepackage{xxx} 指向的 .sty，解析到一半失败会把
    **题名块搅乱**：正文开头多出一段「maketitle thanks aketitle」，
    还凭空多一节叫「正文」（ICLR 模板两篇都中招）。
    """

    def test_local_sty_neutralized(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "conf.sty").write_text(
                r"\def\maketitle{\par}" + "\n" + r"\newcommand{\my}[1]{#1}", encoding="utf-8")
            s = r"\documentclass{conf}\usepackage{conf}\title{T}\begin{document}x\end{document}"
            out, harvested = latex_clean.neutralize_local_packages(s, base)
            self.assertNotIn(r"\usepackage{conf}", out)
            self.assertIn("local package neutralized", out)
            self.assertTrue(any("my" in h for h in harvested), "宏定义应该被搬进 tex")

    def test_system_package_kept(self):
        with tempfile.TemporaryDirectory() as td:
            s = r"\usepackage{amsmath}\usepackage{graphicx}"
            out, harvested = latex_clean.neutralize_local_packages(s, Path(td))
            self.assertIn(r"\usepackage{amsmath}", out)
            self.assertIn(r"\usepackage{graphicx}", out)
            self.assertEqual(harvested, [])

    def test_mixed_packages(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "conf.sty").write_text("x", encoding="utf-8")
            s = r"\usepackage{conf,amsmath}"
            out, _ = latex_clean.neutralize_local_packages(s, base)
            self.assertIn(r"\usepackage{amsmath}", out)
            self.assertNotIn(r"\usepackage{conf,", out)


if __name__ == "__main__":
    unittest.main()
