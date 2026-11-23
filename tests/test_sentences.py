"""句子切分与点词分词（从 shadow-learning 搬来，论文版加了一处：公式占位符保护）。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from extensions import schema, sentences


class TestSplitSentences(unittest.TestCase):
    def test_basic_split(self):
        s = "This is a sentence. And this is another one here."
        out = sentences.split_sentences(s)
        self.assertGreaterEqual(len(out), 2)

    def test_abbreviation_not_split(self):
        s = "Dr. Smith wrote a very long paper about markets. It was published last year."
        out = sentences.split_sentences(s)
        self.assertTrue(any("Dr." in x for x in out))
        # 「Dr.」后面不应该被切开
        self.assertTrue(any(x.startswith("Dr. Smith wrote") for x in out))

    def test_decimal_not_split(self):
        s = "The value is 3.14 in this measurement. Another clause follows here."
        out = sentences.split_sentences(s)
        self.assertTrue(any("3.14" in x for x in out))

    def test_math_placeholder_not_split(self):
        """回归：公式占位符里有小数点，不能把一句切成两半。"""
        inner = "0.8521"
        t = "The threshold is " + schema.wrap_math(inner) + " for this model. Next sentence here."
        out = sentences.split_sentences(t)
        self.assertEqual(len(out), 2)
        self.assertIn(schema.wrap_math(inner), out[0])

    def test_math_placeholder_restored(self):
        inner = r"\frac{1}{2}"
        t = "We have " + schema.wrap_math(inner) + " in this model. Next sentence here."
        out = sentences.split_sentences(t)
        self.assertIn(schema.wrap_math(inner), out[0])

    def test_short_fragments_dropped(self):
        self.assertEqual(sentences.split_sentences("Hi. Ok."), [])

    def test_cjk_branch(self):
        s = "这是一句比较长的中文句子，用来测试切分。后面还有另一句比较长的中文。"
        out = sentences.split_sentences(s)
        self.assertGreaterEqual(len(out), 1)


class TestLigatures(unittest.TestCase):
    def test_ffi_ligature(self):
        # "efficient" 的连字是 U+FB03（ffi），不是 U+FB01（fi）
        self.assertEqual(sentences.normalize_ligatures("e\ufb03cient"), "efficient")

    def test_fi_ligature(self):
        self.assertEqual(sentences.normalize_ligatures("e\ufb01cient"), "eficient")

    def test_fl_ligature(self):
        self.assertEqual(sentences.normalize_ligatures("ﬂow"), "flow")


class TestTokenize(unittest.TestCase):
    def test_words_only(self):
        toks = sentences.tokenize_words("The AI model (OIm) predicts 17.2%.")
        words = [w for _, _, w in toks]
        self.assertIn("model", words)
        self.assertIn("predicts", words)
        self.assertNotIn("17", words)  # 数字不是单词

    def test_hyphenated_kept(self):
        toks = sentences.tokenize_words("a state-of-the-art result")
        self.assertIn("state-of-the-art", [w for _, _, w in toks])


if __name__ == "__main__":
    unittest.main()
