"""论文文档 schema 的契约测试。

schema 是前后端的唯一真相：前端按 blocks 的 k 分派渲染，后端按同一套生成。
这里把「什么算合法」钉死，避免以后改了一处忘了另一处。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from extensions import schema


class TestSchema(unittest.TestCase):
    def _ok_doc(self):
        return schema.make_doc(
            "p1", "标题",
            sections=[schema.make_section("s1", "Intro", 1) | {"blocks": [
                {"k": "p", "t": "正文"},
                {"k": "eq", "latex": "x^2"},
                {"k": "fig", "src": "assets/a.png", "caption": "cap"},
                {"k": "table", "rows": [["a", "b"]], "caption": ""},
                {"k": "list", "items": ["x"]},
                {"k": "quote", "t": "定理"},
            ]}],
        )

    def test_valid_doc_passes(self):
        self.assertEqual(schema.validate(self._ok_doc()), [])

    def test_missing_id_rejected(self):
        d = self._ok_doc()
        del d["id"]
        self.assertIn("缺 id", "；".join(schema.validate(d)))

    def test_missing_title_rejected(self):
        d = self._ok_doc()
        del d["title"]
        self.assertTrue(schema.validate(d))

    def test_unknown_block_kind_rejected(self):
        d = self._ok_doc()
        d["sections"][0]["blocks"][0]["k"] = "video"
        self.assertTrue(any("未知块类型" in e for e in schema.validate(d)))

    def test_equation_requires_latex(self):
        d = self._ok_doc()
        d["sections"][0]["blocks"][1] = {"k": "eq", "latex": "  "}
        self.assertTrue(any("缺 latex" in e for e in schema.validate(d)))

    def test_figure_requires_src(self):
        d = self._ok_doc()
        d["sections"][0]["blocks"][2] = {"k": "fig", "caption": "x"}
        self.assertTrue(any("缺 src" in e for e in schema.validate(d)))

    def test_bad_level_rejected(self):
        d = self._ok_doc()
        d["sections"][0]["level"] = 9
        self.assertTrue(any("level 非法" in e for e in schema.validate(d)))

    def test_empty_sections_rejected(self):
        d = self._ok_doc()
        d["sections"] = []
        self.assertTrue(schema.validate(d))


class TestMathPlaceholder(unittest.TestCase):
    def test_wrap_and_split_roundtrip(self):
        t = "a " + schema.wrap_math("x_1 + y") + " b"
        parts = schema.split_math(t)
        self.assertEqual(parts[0][0], "text")
        self.assertEqual(parts[1], ("math", "x_1 + y"))

    def test_strip_math_removes_placeholder(self):
        t = "before " + schema.wrap_math("\\frac{1}{2}") + " after"
        out = schema.strip_math(t)
        self.assertNotIn("⟦", out)
        self.assertNotIn("⟧", out)

    def test_placeholder_survives_text_with_math(self):
        t = schema.wrap_math("a.b.c") + " tail"
        self.assertEqual(len(schema.split_math(t)), 2)


if __name__ == "__main__":
    unittest.main()
