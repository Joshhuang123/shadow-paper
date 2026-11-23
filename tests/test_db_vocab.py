"""生词本 SRS + DB 层的测试（用临时库，不碰真实数据）。"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from extensions import db, vocab


class _TempDB(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = (db.DATA_DIR, db.DB_PATH)
        db.DATA_DIR = Path(self._tmp.name)
        db.DB_PATH = db.DATA_DIR / "test.db"
        if getattr(db._local, "conn", None) is not None:
            try:
                db._local.conn.close()
            except Exception:
                pass
        db._local.conn = None
        db.init_db()

    def tearDown(self):
        if getattr(db._local, "conn", None) is not None:
            try:
                db._local.conn.close()
            except Exception:
                pass
        db._local.conn = None
        db.DATA_DIR, db.DB_PATH = self._old
        self._tmp.cleanup()


class TestVocabSRS(_TempDB):
    def test_lookup_creates_entry(self):
        r = vocab.record_lookup("Coordination")
        self.assertEqual(r["word"], "coordination")
        self.assertEqual(r["state"], "learning")

    def test_lookup_is_idempotent(self):
        vocab.record_lookup("coordination")
        r = vocab.record_lookup("coordination")
        self.assertEqual(r["review_count"], 0)

    def test_correct_advances_state(self):
        vocab.record_lookup("alpha")
        r = vocab.record_review("alpha", True)
        self.assertEqual(r["state"], "practicing")

    def test_wrong_falls_back(self):
        vocab.record_lookup("beta")
        vocab.record_review("beta", True)
        vocab.record_review("beta", True)
        r = vocab.record_review("beta", False)
        self.assertEqual(r["state"], "practicing")

    def test_learning_is_floor(self):
        vocab.record_lookup("gamma")
        r = vocab.record_review("gamma", False)
        self.assertEqual(r["state"], "learning")

    def test_mastered_is_cap(self):
        vocab.record_lookup("delta")
        for _ in range(6):
            r = vocab.record_review("delta", True)
        self.assertEqual(r["state"], "mastered")

    def test_review_unknown_word(self):
        self.assertEqual(vocab.record_review("nosuchword", True), {})

    def test_stats(self):
        vocab.record_lookup("one")
        vocab.record_lookup("two")
        s = vocab.stats()
        self.assertEqual(s["total"], 2)
        self.assertEqual(s["learning"], 2)

    def test_due_queue(self):
        vocab.record_lookup("soon")
        q = vocab.due_queue()
        self.assertEqual([x["word"] for x in q], ["soon"])

    def test_remove(self):
        vocab.record_lookup("gone")
        vocab.remove("gone")
        self.assertEqual(vocab.stats()["total"], 0)


class TestPaperStore(_TempDB):
    def test_save_and_load(self):
        doc = {
            "id": "p1", "title": "T", "authors": ["A"], "sections": [
                {"id": "s1", "title": "S", "level": 1, "blocks": [{"k": "p", "t": "x"}]}],
            "stats": {"words": 3},
        }
        db.save_paper(doc, "arxiv")
        got = db.load_paper("p1")
        self.assertEqual(got["title"], "T")
        self.assertEqual(got["sections"][0]["blocks"][0]["t"], "x")

    def test_list_papers(self):
        db.save_paper({"id": "a", "title": "A", "sections": [], "stats": {}}, "arxiv")
        db.save_paper({"id": "b", "title": "B", "sections": [], "stats": {}}, "pdf")
        ids = {p["id"] for p in db.list_papers()}
        self.assertEqual(ids, {"a", "b"})

    def test_delete(self):
        db.save_paper({"id": "x", "title": "X", "sections": [], "stats": {}}, "arxiv")
        db.delete_paper("x")
        self.assertIsNone(db.load_paper("x"))


if __name__ == "__main__":
    unittest.main()
