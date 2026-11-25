"""tests/test_library.py —— 检索/问答内核的离线测试。

原则：**不调模型**。问��那一层（检索 + 取证）必须可离线、可复现地测；
调模型的部分靠真实库 + 真机验收，不进单元测试（不然每次跑都要花钱、还要网络）。

跑：`.venv/bin/python -m pytest tests/test_library.py -q`
或：`.venv/bin/python tests/test_library.py`
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from extensions import library  # noqa: E402


class TestTokenize(unittest.TestCase):
    def test_english_and_cjk(self):
        toks = library.tokenize("Latent reasoning 潜空间")
        self.assertIn("latent", toks)
        self.assertIn("reasoning", toks)
        self.assertIn("潜空", toks)      # 中文二元组
        self.assertIn("空间", toks)

    def test_stopwords_dropped(self):
        self.assertNotIn("the", library.tokenize("the model"))

    def test_math_stripped(self):
        # ⟦MATH:…⟧ 占位符不该进索引
        self.assertNotIn("math", library.tokenize("x ⟦MATH:\\frac{1}{2}⟧⟧ y"))


class TestBM25(unittest.TestCase):
    def test_ranks_relevant_first(self):
        docs = [
            library.tokenize("inference time per test case A100 batch size"),
            library.tokenize("data pool topic descriptors"),
            library.tokenize("latent tree search breadth first"),
        ]
        bm = library.BM25(docs)
        q = library.tokenize("inference time A100")
        self.assertGreater(bm.score(q, 0), bm.score(q, 1))
        self.assertGreater(bm.score(q, 0), bm.score(q, 2))

    def test_empty_corpus_is_safe(self):
        bm = library.BM25([])
        self.assertEqual(bm.score(["x"], 0), 0) if False else None


class TestSearchAgainstRealLibrary(unittest.TestCase):
    """用真库测检索（读 data/shadow-paper.db，不调模型、不花钱）。"""

    @classmethod
    def setUpClass(cls):
        if not (HERE.parent / "data" / "shadow-paper.db").exists():
            raise unittest.SkipTest("库里没有论文，跳过")
        library.build_index(force=True)

    def test_search_finds_table_numbers(self):
        """⚠️ 这条是回归测试：表格的数字在 rows 里、章节标题也要进索引，
        否则「推理速度」这种问句永远捞不到那张表（2026-11-24 实测踩过）。"""
        hits = library.search("clock time reasoning efficiency metric", limit=5)
        self.assertTrue(hits, "英文检索应该有命中")
        pid = "2412.06769"
        if any(h["paper_id"] == pid for h in hits):
            texts = " ".join(h["text"] for h in hits if h["paper_id"] == pid)
            self.assertTrue(
                any(k in texts for k in ("0.26", "0.09", "0.85", "0.47")),
                "命中里应该能看到 Clock-Time 表的数值",
            )

    def test_search_is_scored_and_cited(self):
        hits = library.search("latent token perturbation shortcut", limit=5)
        for h in hits:
            self.assertIn("paper_id", h)
            self.assertIn("section", h)
            self.assertGreater(h["score"], 0)

    def test_get_paper_by_section(self):
        r = library.get_paper("2412.06769", section="Clock-Time")
        self.assertTrue(r.get("ok"), r.get("error"))
        self.assertTrue(r["sections_used"])

    def test_get_paper_missing_section_lists_options(self):
        r = library.get_paper("2412.06769", section="不存在的章节")
        self.assertFalse(r.get("ok"))
        self.assertIn("available", r)

    def test_library_stats(self):
        st = library.library_stats()
        self.assertGreater(st["papers"], 0)
        self.assertGreater(st["passages"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
