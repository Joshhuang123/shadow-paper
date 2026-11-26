"""library.py —— 把论文库变成「能问答的知识库」。

这个模块是 shadow-paper 从「阅读器」长成「IMA 式论文库」的核心：
阅读器给人读，本模块给 agent 用。

三层：
    ① 检索层  BM25（纯 Python，零第三方依赖）——把问题变成「哪几段原文相关」
    ② 取证层  每条命中都带 paper_id / 章节 / 原文片段 ——答案必须能落到纸上
    ③ 生成层  调 MiniMax（OpenAI 兼容）写答案，**只允许用检索到的原文**

设计上的三条硬规矩（都是踩过坑换来的）：
    · **不许编。** 检索不到就说检索不到 —— 宁可答「库里没有」，也不给一个像模像样的假答案。
    · **每个断言都要有出处。** 返回的答案带 [paper_id / 章节] 标记，能一键跳回原文。
    · **默认不调模型。** search / get 全是纯本地、零成本、确定性；只有 ask 才调 API。

为什么用 BM25 而不是向量：
    论文问答里，问题里的术语（方法名、指标名、benchmark）几乎总是**字面出现在原文**里。
    BM25 对术语精确匹配比向量检索更准，也不需要 embedding 模型、不花钱、可离线、可复现。
    真要语义泛化，那是 semantix 那条线的活（它有向量层），不该在这儿硬造一个。
"""

from __future__ import annotations

import json
import math
import os
import re
import urllib.error
import urllib.request
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import db, schema

# ── 分词 ───────────────────────────────────────────────────────────────
# 英文按词（保留 - 和 . 内部的数字），中文按字 + 二元组（中文没有空格，
# 不切词的话整段只会命中一个字）。
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9'\-]*|\d+(?:\.\d+)?|[\u4e00-\u9fff]")
_STOP = {
    "the", "a", "an", "of", "and", "or", "to", "in", "is", "are", "was", "were",
    "be", "been", "for", "with", "that", "this", "these", "those", "it", "its",
    "as", "by", "on", "at", "from", "we", "our", "can", "which", "than", "then",
    "what", "how", "why", "does", "do", "did", "not", "but", "also", "more",
    "most", "such", "using", "use", "used", "via", "into", "over", "under",
    "的", "了", "和", "是", "在", "有", "也", "就", "不", "与", "及", "被", "把",
}


def tokenize(text: str) -> List[str]:
    """英文按词、中文按「单字 + 相邻二字组」。数学占位符要摘掉（⟦MATH:…⟧）。"""
    text = schema.strip_math(text or "")
    toks: List[str] = []
    raw = _TOKEN_RE.findall(text.lower())
    for t in raw:
        if t in _STOP:
            continue
        toks.append(t)
    # 中文二元组（相邻两个汉字）
    cjk = [c for c in text if "一" <= c <= "鿿"]
    for i in range(len(cjk) - 1):
        bg = cjk[i] + cjk[i + 1]
        if bg not in _STOP:
            toks.append(bg)
    return toks


# ── 中英别名表（查询侧扩展用，零成本、确定性）────────────────
# 中文问句 → 论文里真正会出现的英文术语
ZH_ALIASES: Dict[str, List[str]] = {
    "推理速度": ["inference latency", "clock time", "wall-clock", "speedup", "throughput"],
    "推理时延": ["inference latency", "clock time", "wall-clock"],
    "速度": ["speedup", "throughput", "latency"],
    "耗时": ["clock time", "latency", "runtime"],
    "准确率": ["accuracy", "acc"],
    "跑分": ["accuracy", "benchmark", "score"],
    "基准": ["benchmark"],
    "过拟合": ["overfitting"],
    "泛化": ["generalization", "generalisation"],
    "幻觉": ["hallucination"],
    "微调": ["fine-tuning", "finetune", "SFT"],
    "强化学习": ["reinforcement learning", "RL", "GRPO", "PPO"],
    "监督学习": ["supervised", "SFT"],
    "自监督": ["self-supervised", "self-supervised learning"],
    "蒸馏": ["distillation", "EMA teacher", "teacher"],
    "老师模型": ["teacher", "EMA teacher"],
    "潜空间": ["latent space"],
    "潜状态": ["latent state"],
    "隐状态": ["hidden state"],
    "连续思维": ["continuous thought"],
    "思维链": ["chain of thought", "CoT"],
    "广度优先": ["breadth-first", "BFS"],
    "深度优先": ["depth-first", "DFS"],
    "树搜索": ["tree search", "BFS"],
    "捷径": ["shortcut", "dataset artifact"],
    "走捷径": ["shortcut", "shortcut learning"],
    "伪推理": ["pseudo-reasoning", "fake reasoning"],
    "因果干预": ["causal intervention", "steering", "causal"],
    "干预": ["intervention", "steering", "perturbation"],
    "探针": ["probe", "probing", "linear probe"],
    "反例": ["counterexample"],
    "对抗": ["adversarial"],
    "世界模型": ["world model"],
    "机器人": ["robot", "robotic", "Franka", "Droid"],
    "零样本": ["zero-shot"],
    "规划": ["planning", "plan"],
    "扩展性": ["scaling", "scale"],
    "规模": ["scaling", "scale", "model size"],
    "权重平均": ["EMA", "exponential moving average"],
    "嵌入": ["embedding"],
    "注意力": ["attention"],
    "上下文": ["context"],
    "困惑度": ["perplexity"],
    "语言空间": ["language space", "language mode"],
    "表示": ["representation"],
    "数据集": ["dataset", "benchmark"],
    "消融": ["ablation"],
    "基线": ["baseline"],
    "指标": ["metric"],
    "损失": ["loss"],
    "梯度": ["gradient"],
    "奖励": ["reward"],
}

# 英文术语 → 中文别名（英文问句时补中文，让中文索引也能命中）
EN_ALIASES: Dict[str, List[str]] = {
    "latent space": ["潜空间"],
    "latent reasoning": ["潜空间推理"],
    "continuous thought": ["连续思维"],
    "chain of thought": ["思维链"],
    "pseudo-reasoning": ["伪推理"],
    "shortcut": ["捷径"],
    "clock time": ["推理时延", "推理速度"],
    "inference latency": ["推理时延"],
    "throughput": ["吞吐量"],
    "latency": ["推理时延", "时延"],
    "speedup": ["加速比", "推理速度"],
    "overfitting": ["过拟合"],
    "generalization": ["泛化"],
    "hallucination": ["幻觉"],
    "fine-tuning": ["微调"],
    "reinforcement learning": ["强化学习"],
    "self-supervised": ["自监督"],
    "distillation": ["蒸馏"],
    "world model": ["世界模型"],
    "zero-shot": ["零样本"],
    "tree search": ["树搜索"],
    "breadth-first": ["广度优先"],
    "dataset artifact": ["捷径", "数据集痕迹"],
    "causal intervention": ["因果干预"],
    "counterexample": ["反例"],
    "adversarial": ["对抗"],
    "planning": ["规划"],
    "scaling": ["扩展性", "规模"],
    "EMA teacher": ["老师模型", "权重平均"],
    "embedding": ["嵌入"],
    "attention": ["注意力"],
    "language space": ["语言空间"],
}


def _expand_query_tokens(text: str) -> List[str]:
    """把查询里的中文别名换成英文术语、英文术语换成中文别名，一起进 BM25。

    这是**查询侧**扩展，不改索引 —— 索引里中英本来都在。
    """
    extra: List[str] = []
    low = (text or "").lower()
    for zh, ens in ZH_ALIASES.items():
        if zh in (text or ""):
            extra.extend(ens)
    for en, zhs in EN_ALIASES.items():
        if en in low:
            extra.extend(zhs)
    return extra


# ── BM25 ───────────────────────────────────────────────────────────────
class BM25:
    """标准 BM25（k1=1.5, b=0.75）。文档 = 论文里的一个块。"""

    def __init__(self, docs: List[List[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.n = len(docs)
        self.docs = docs
        self.df = Counter()
        for d in docs:
            for t in set(d):
                self.df[t] += 1
        self.avgdl = (sum(len(d) for d in docs) / self.n) if self.n else 0.0
        self.tf = [Counter(d) for d in docs]

    def idf(self, term: str) -> float:
        n = self.df.get(term, 0)
        # +0.5 平滑；未出现词给一个很小的正 idf（不是负数）
        return math.log(1 + (self.n - n + 0.5) / (n + 0.5))

    def score(self, query_tokens: Iterable[str], i: int) -> float:
        tf, dl = self.tf[i], len(self.docs[i])
        s = 0.0
        for t in query_tokens:
            f = tf.get(t, 0)
            if not f:
                continue
            denom = f + self.k1 * (1 - self.b + self.b * (dl / self.avgdl if self.avgdl else 1))
            s += self.idf(t) * (f * (self.k1 + 1)) / (denom or 1)
        return s


# ── 索引 ───────────────────────────────────────────────────────────────
class Passage:
    __slots__ = ("pid", "title", "section", "section_id", "text", "kind", "sec_title")

    def __init__(self, pid: str, title: str, section: str, section_id: str, text: str, kind: str,
                 sec_title: str = ""):
        self.pid, self.title, self.section, self.section_id = pid, title, section, section_id
        self.text, self.kind = text, kind
        self.sec_title = sec_title or section

    def as_dict(self, snippet: str = "") -> Dict[str, Any]:
        return {
            "paper_id": self.pid,
            "title": self.title,
            "section": self.section,
            "section_id": self.section_id,
            "kind": self.kind,
            "text": snippet or self.text[:600],
        }


_INDEX: Optional[BM25] = None
_TITLE_INDEX: Optional[BM25] = None
_PASSAGES: List[Passage] = []




def _iter_blocks(doc: Dict[str, Any]):
    for sec in doc.get("sections", []):
        for blk in sec.get("blocks", []):
            yield sec, blk


def build_index(force: bool = False) -> Tuple[BM25, List[Passage]]:
    """建/取索引。文档 = 块（段落/公式/列表/引用）；图和表也进索引（带图注）。"""
    global _INDEX, _PASSAGES
    if _INDEX is not None and not force:
        return _INDEX, _PASSAGES
    passages: List[Passage] = []
    for row in db.list_papers():
        doc = db.load_paper(row["id"])
        if not doc:
            continue
        pid = doc.get("id") or row["id"]
        title = doc.get("title") or row.get("title") or pid
        # 中文版（有就拿来）：让中文提问直接命中，不用每次先翻检索词。
        # ⚠️ 中文 doc 是英文 doc 的深拷贝、只替换文字，**sections/blocks 结构一一对应**，
        #    所以按位置并行走最准（按 id 匹配反而可能错位）。
        zh_doc = db.load_zh_paper(pid) or {}
        zh_secs = list(zh_doc.get("sections") or [])
        en_secs = list(doc.get("sections") or [])

        for si, sec in enumerate(en_secs):
            zsec = zh_secs[si] if si < len(zh_secs) else {}
            zblocks = list(zsec.get("blocks") or [])
            for bi, blk in enumerate(sec.get("blocks", [])):
                zblk = zblocks[bi] if bi < len(zblocks) else {}
                kind = blk.get("k", "p")
                text = blk.get("t") or ""
                if kind == "fig":
                    text = (blk.get("caption") or "") + " " + text
                if kind == "table":
                    # ⚠️ 表格的数字在 **rows**，不在 t（2026-11-24 实测）：
                    #    块结构是 {k:"table", rows:[[...],[...]], caption:"..."}，
                    #    只读 t 的话**整张表的内容对检索完全隐形** ——
                    #    而论文里最硬的那个数（MMLU 37.5→47.7、耗时 0.09 vs 0.26）
                    #    全在表里。必须把 rows 摊平成文本一起进索引。
                    cap = blk.get("caption") or ""
                    rows = blk.get("rows") or []
                    flat = " ; ".join(" ".join(str(c) for c in row) for row in rows)
                    text = (cap + " " + flat + " " + text).strip()
                text = (text or "").strip()
                if len(text) < 12:
                    continue
                sec_title = sec.get("title", "")
                # ⚠️ 章节标题必须进索引（2026-11-24 实测）：
                #    问「推理速度快多少」，答案在附录的 Clock-Time Reasoning Efficiency
                #    Metric 那张表里 —— 但表里的 token 是 "No-CoT 0.03 CoT 0.26 Coconut 0.09"，
                #    跟「推理速度」一个词都对不上，**光靠块内文字永远检索不到那张表**。
                #    把标题拼进索引文本，块就带上了「这一段在讲什么」的语境。
                zh_text = ""
                if zblk:
                    zh_text = (zblk.get("t") or zblk.get("caption") or "").strip()
                # 中文在前、英文在后：**中文问题命中中文部分，英文问题命中英文部分**，
                # 一条 passage 两种语言都能检索到，答案还能给出英文原文依据。
                body = f"{sec_title}\n{zh_text}\n{text}" if zh_text else f"{sec_title}\n{text}"
                passages.append(Passage(pid, title, sec_title, sec.get("id", ""), body, kind,
                                    sec_title))
        # 再单独给每个章节一条「章节级」条目：
        # 有些问题问的就是某个章节（"它的消融实验怎么做的"），只靠块级条目会漏。
        for si, sec in enumerate(en_secs):
            t = (sec.get("title") or "").strip()
            if len(t) >= 3:
                passages.append(Passage(pid, title, t, sec.get("id", ""), t, "head", t))
                if si < len(zh_secs):
                    zt = (zh_secs[si].get("title") or "").strip()
                    if zt and zt != t:
                        passages.append(Passage(pid, title, zt, sec.get("id", ""), zt, "head", zt))
    _PASSAGES = passages
    _INDEX = BM25([tokenize(p.text) for p in passages])
    # 标题单独一份索引（BM25F 思路）：
    #   「Coconut 相比 CoT 快多少」—— "Coconut" 在**参考文献**里出现几十次，
    #   和正文同权时那一节稳赢，真正写着 Clock-Time 的那一节反而进不了前列。
    #   标题单独算一份、权重更高，命中「小节标题」的排序才符合直觉。
    _TITLE_INDEX = BM25([tokenize(p.sec_title) for p in passages])
    return _INDEX, _PASSAGES


def library_stats() -> Dict[str, Any]:
    idx, passages = build_index()
    rows = db.list_papers()
    total_words = 0
    for r in rows:
        total_words += int(r.get("n_words") or 0)
    return {
        "papers": len(rows),
        "passages": len(passages),
        "words": total_words,
        "titles": [{"id": r["id"], "title": r.get("title"), "authors": r.get("authors"),
                    "words": r.get("n_words"), "sections": r.get("n_sections"),
                    "source": r.get("source_type"),
                    "has_zh": db.has_zh(r["id"])} for r in rows],
    }


# ── 检索 ───────────────────────────────────────────────────────────────
def search(query: str, limit: int = 8, paper_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """BM25 检索。返回带出处的片段，按分数降序。

    命中片段用**原文**，不做改写 —— agent 要能直接引用。
    """
    idx, passages = build_index()
    if not passages:
        return []
    qt = tokenize(query)
    # 中文问句 ↔ 英文术语 的别名扩展（零成本、确定性）
    qt = qt + tokenize(" ".join(_expand_query_tokens(query)))
    if not qt:
        return []
    scored: List[Tuple[float, int]] = []
    for i, p in enumerate(passages):
        if paper_id and p.pid != paper_id:
            continue
        s = idx.score(qt, i)
        if _TITLE_INDEX is not None:
            # 标题命中的权重更高（BM25F 思路）
            s += 3.0 * _TITLE_INDEX.score(qt, i)
        if s > 0:
            scored.append((s, i))
    scored.sort(key=lambda x: -x[0])
    out: List[Dict[str, Any]] = []
    for s, i in scored[: max(1, limit)]:
        p = passages[i]
        d = p.as_dict(p.text[:900])
        d["score"] = round(s, 3)
        out.append(d)
    return out


def get_paper(paper_id: str, section: Optional[str] = None, max_chars: int = 12000,
              lang: str = "en") -> Dict[str, Any]:
    """取一篇论文（可限定到某节）。

    lang="zh" 取中文版（没译过自动回落英文，并在结果里标出来）。
    """
    doc = db.load_paper(paper_id)
    if not doc:
        return {"ok": False, "error": f"库里没有 {paper_id}"}
    zh_doc = db.load_zh_paper(paper_id)
    if lang in ("zh", "cn", "zh-cn") and zh_doc:
        doc = zh_doc
    secs = doc.get("sections", [])
    if section:
        key = section.strip().lower()
        secs = [s for s in secs if key in (s.get("title") or "").lower()]
        if not secs:
            return {"ok": False, "error": f"{paper_id} 里没有章节「{section}」",
                    "available": [s.get("title") for s in doc.get("sections", [])][:60]}
    chunks: List[str] = []
    used: List[str] = []
    for s in secs:
        used.append(s.get("title") or "")
        body = "\n".join(
            (b.get("t") or "") + (f" 〔图〕{b.get('caption','')}" if b.get("k") == "fig" else "")
            for b in s.get("blocks", [])
        )
        chunks.append(f"\n## {s.get('title','')}\n{body}")
    text = "\n".join(chunks)[:max_chars]
    return {
        "ok": True,
        "id": paper_id,
        "lang": "zh" if (lang in ("zh", "cn", "zh-cn") and zh_doc) else "en",
        "has_zh": bool(zh_doc),
        "title": doc.get("title"),
        "authors": doc.get("authors"),
        "date": doc.get("date"),
        "venue": doc.get("venue"),
        "abstract": doc.get("abstract"),
        "sections_used": used,
        "truncated": len("".join(chunks)) > max_chars,
        "text": text,
    }


# ── 生成层 ─────────────────────────────────────────────────────────────
_API = os.environ.get("MINIMAX_API_BASE", "https://api.minimaxi.com/v1")
_MODEL = os.environ.get("SHADOW_PAPER_MODEL", "MiniMax-M3.1-Flash")


def _call_llm(prompt: str, system: str = "", max_tokens: int = 2500) -> Tuple[str, Optional[str]]:
    """调 MiniMax（OpenAI 兼容）。返回 (文本, 错误)。

    没有 key / 调用失败 → 返回错误而不是编答案。**这是这个模块最要紧的一条。**
    """
    key = os.environ.get("MINIMAX_API_KEY")
    if not key:
        return "", "没设 MINIMAX_API_KEY —— 只返回检索片段，不生成答案"
    body = {
        "model": _MODEL,
        "messages": ([{"role": "system", "content": system}] if system else [])
                    + [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.2,
    }
    req = urllib.request.Request(
        f"{_API}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return "", f"模型接口 HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return "", f"模型接口失败：{e!r}"[:160]
    # ⚠️ MiniMax 的 M3 系列**默认先思考**，思考 token 也算进 max_tokens。
    #    max_tokens 给小了会出现：finish_reason="length"、message 里**只有
    #    reasoning_content、没有 content** —— 直接 KeyError('content')。
    #    所以：token 预算要留够「思考 + 答案」，且这里不能假设 content 一定存在。
    try:
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
    except Exception:  # noqa: BLE001
        return "", "模型返回结构异常"
    content = msg.get("content")
    if not content:
        why = choice.get("finish_reason")
        if msg.get("reasoning_content"):
            # 思考长度**不稳定**：同一个 prompt，有时思考 100 token 就够，
            # 有时 400 全被思考吃掉、正文一个字没吐（finish_reason=length）。
            # 所以这种失败要**加倍预算自动重试**，不能直接放弃。
            if max_tokens < 8000:
                return _call_llm(prompt, system=system, max_tokens=min(8000, max_tokens * 3))
            return "", (f"模型把 max_tokens={max_tokens} 全用在了思考上、没吐出正文"
                        f"（finish_reason={why}）")
        return "", f"模型没返回正文（finish_reason={why}）"
    return content, None


_ANSWER_SYSTEM = """你是「论文知识库」的问答助手。你只能依据下面给出的论文原文片段回答问题。

硬规矩：
1. **只用片段里的信息**，不要用你自己的先验知识补充、推断、举例。
2. 每个结论后面标出处，格式 [paper_id / 章节名]。
3. 片段不足以回答时，直接说「库里给出的材料不足以回答」，并说明缺什么。**不要编。**
4. 论文之间口径不一致时，把两边的说法都列出来并注明出处，不要替作者调和。
5. 用中文回答；原文是英文时，术语保留英文并给中文解释。
6. 回答末尾用一行 `——依据：<篇号列表>` 汇总用到的论文。"""


_EXPAND_SYSTEM = """你把中文问题翻译成英文论文检索关键词，用来在一堆英文论文里做 BM25 检索。

只输出关键词，逗号分隔，不要解释、不要句子。

关键：**BM25 是字面匹配，只认原文真出现过的词。** 所以关键词要覆盖
论文作者可能用的**多种说法**，而不只是最直译的那个词。
例如「推理速度」→ clock time, wall-clock time, inference time, latency,
efficiency, tokens per second, throughput, seconds per question
「潜空间」→ latent space, continuous thought, hidden state representation
「反驳」→ refute, counterexample, causal analysis, adversarial, critique

一次给 10-15 个词，逗号分隔。宁可多给几个近义表达，也不要漏掉论文真会用的写法。"""


def _expand_query(question: str) -> str:
    """把中文问句翻成英文检索词。

    为什么需要（2026-11-24 实测）：库里全是英文正文，BM25 是字面匹配，
    中文问句「推理速度快多少」在英文原文里一个 token 都命中不了 → 0 结果。
    这一步只在**检索不到**或**问题含中文**时做一次，英文问句不花这个钱。
    """
    if not re.search(r"[\u4e00-\u9fff]", question):
        return ""
    out, err = _call_llm(f"问题：{question}\n\n英文检索关键词：", system=_EXPAND_SYSTEM,
                         max_tokens=1500)
    if err:
        return ""
    return re.sub(r"[\n\r]", " ", out).strip()[:400]


def ask(question: str, top_k: int = 8, paper_id: Optional[str] = None) -> Dict[str, Any]:
    """检索 + 生成带引用的答案。

    返回：answer（中文，带 [paper_id / 章节] 标记）、citations（出处列表）、
    passages（检索到的原文片段）、used_llm（是否真的调了模型）。
    """
    hits = search(question, limit=top_k, paper_id=paper_id)
    expanded = ""
    # 先扩一次检索词再搜 —— 但**只在检索弱的时候**：
    #   有了中文版之后，中文提问常常第一轮就命中得很准
    #   （实测「推理速度有多快」→ 54 分、「潜空间树搜索」→ 59 分），
    #   这时再翻一次检索词纯属多花一次模型调用。
    #   阈值：最高分 ≥12 就算命中得实，不翻。
    strong = bool(hits) and hits[0]["score"] >= 12
    if not strong:
        expanded = _expand_query(question)
        if expanded:
            more = search(expanded, limit=top_k, paper_id=paper_id)
            seen = {(h["paper_id"], h["section"], h["text"][:80]) for h in hits}
            for h in more:
                k = (h["paper_id"], h["section"], h["text"][:80])
                if k not in seen:
                    hits.append(h)
                    seen.add(k)
            hits.sort(key=lambda h: -h["score"])
            hits = hits[: top_k * 2]
    if not hits:
        return {
            "answer": "库里没有检索到相关材料，不能回答这个问题。",
            "citations": [], "passages": [], "used_llm": False,
            "note": "检索无命中",
        }
    ctx = "\n\n".join(
        f"[{i+1}] 论文 {h['paper_id']} · 章节《{h['section']}》\n{h['text']}"
        for i, h in enumerate(hits)
    )
    prompt = f"问题：{question}\n\n以下是从论文库检索到的原文片段：\n\n{ctx}\n\n请依据这些片段回答。"
    text, err = _call_llm(prompt, system=_ANSWER_SYSTEM)
    citations = []
    for h in hits:
        key = (h["paper_id"], h["section"])
        if key not in [(c["paper_id"], c["section"]) for c in citations]:
            citations.append({"paper_id": h["paper_id"], "title": h["title"],
                              "section": h["section"]})
    if err:
        return {
            "answer": "",
            "error": err,
            "citations": citations,
            "passages": hits,
            "used_llm": False,
            "note": "模型没调成，但检索片段照给 —— agent 可以自己读片段回答",
        }
    return {"answer": text, "citations": citations, "passages": hits, "used_llm": True,
            "expanded_query": expanded}
