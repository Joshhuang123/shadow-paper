"""zh.py —— 论文中文版生成器。

为什么需要（2026-11-26 实测）：
  · arXiv 原生只有英文，没有官方中文版；
  · **中文读者问的问题，BM25 在英文原文上根本匹配不到** ——
    「推理速度快多少」在英文论文里一个 token 都命中不了，
    之前只能靠每次提问前先让模型翻一版检索词（多花一次调用、还可能翻偏）。
  · 有了中文版，检索层可以直接跑在中文上，答案也是中文，出处仍指回英文原文。

三条硬规矩（都是踩过坑换来的）：
  1. **公式不译。** 行内公式是 ⟦MATH:…⟧ 占位符，译文里必须**原样、按同样顺序**保留，
     少一个或多一个都算译文坏了 —— 宁可重译这一段。
  2. **数字不许增删。** 科普内容的底线是「不能编数字」。译文里出现原文没有的数字
     （比如把 "one million" 译成 "100 万"）会被记成警告，人工再看一眼。
  3. **术语全库统一。** 同一个概念在六篇里必须译成同一个词，
     否则 agent 检索时中文说法对不上。

译文存在 papers.zh_json（不是另建一篇），这样：
  · 书架不出现重复条目；
  · 原文永远在，随时能切回去对照。
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import db

# ── 配置 ───────────────────────────────────────────────────────────────
API = os.environ.get("MINIMAX_API_BASE", "https://api.minimaxi.com/v1")
MODEL = os.environ.get("SHADOW_PAPER_ZH_MODEL", "MiniMax-M3.1-Flash")
CACHE_DIR = db.DATA_DIR / "zh_cache"
CONCURRENCY = int(os.environ.get("SHADOW_PAPER_ZH_CONCURRENCY", "6"))

# ── 术语表 ─────────────────────────────────────────────────────────────
# 跨篇统一。英文（中文）—— 译文里必须用中文，括号里保留英文原名由提示词决定。
TERMS: Dict[str, str] = {
    "latent space": "潜空间",
    "latent reasoning": "潜空间推理",
    "latent state": "潜在状态",
    "continuous thought": "连续思维",
    "chain of continuous thought": "连续思维链",
    "pseudo-reasoning": "伪推理",
    "shortcut": "捷径",
    "inference latency": "推理时延",
    "clock time": "推理时延",
    "throughput": "吞吐量",
    "benchmark": "基准",
    "baseline": "基线",
    "ablation": "消融实验",
    "fine-tuning": "微调",
    "fine-tune": "微调",
    "reinforcement learning": "强化学习",
    "self-supervised": "自监督",
    "world model": "世界模型",
    "embedding": "嵌入表示",
    "hidden state": "隐藏状态",
    "attention": "注意力",
    "perplexity": "困惑度",
    "generalization": "泛化",
    "distillation": "蒸馏",
    "causal intervention": "因果干预",
    "probe": "探针",
    "adversarial": "对抗",
    "counterexample": "反例",
    "downstream": "下游",
    "ground truth": "真值",
    "chain of thought": "思维链",
    "greedy decoding": "贪心解码",
    "temperature sampling": "温度采样",
    "supervised fine-tuning": "有监督微调",
    "exponential moving average": "指数滑动平均",
}
_TERM_LINE = "\n".join(f"- {k} → {v}" for k, v in TERMS.items())

SYSTEM = f"""你是英文学术论文的中文译者。把你收到的论文片段译成中文。

规则（每一条都要守住）：
1. **行内公式占位符**形如 ⟦MATH:…⟧ —— **原样保留**，位置对应原句，**个数和顺序都不能变**。
2. **数字原样保留**，不得增删、不得换算（不要把 "one million" 写成 "100 万"；要写就写 "one million（百万）"）。
3. 专有名词/方法名/数据集名**保留英文原名**，首次出现时在后面加中文，如 "latent space（潜空间）"。
4. 术语用下面这张表，全库统一：
{_TERM_LINE}
5. 保持学术准确、句子完整；不要加解释、不要评论、不要总结、不要加标题。
6. 表格只译图注/表注，**表内的数字和单元格一律不译**。
7. 图表编号（Figure 1 / Table 2）译成「图 1」「表 2」，但编号本身不变。

只输出译文。"""

TITLE_SYSTEM = """你是英文学术论文的章节标题译者。把论文的小节标题译成中文。

规则：
1. 只输出译文标题本身，不要加「译文：」之类的前后缀，不要解释。
2. 保留专有名词/方法名的英文原名，首次出现时加中文（如 latent space（潜空间））。
3. 术语用下面这张表：
%s
4. 标题短也要译完整，**绝不要输出「（无正文）」「(empty)」这类占位**。

标题译文示例：
- Introduction → 引言
- Related Work → 相关工作
- Experimental Setup → 实验设置
- Overall Results → 总体结果
- Chain of Continuous Thought → 连续思维链
- Clock-Time Reasoning Efficiency Metric → 推理时延效率指标
""" % _TERM_LINE

# 数学占位符
_MATH_RE = re.compile(r"⟦MATH:.*?⟧⟧", re.S)
_NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")


def _call_llm(prompt: str, max_tokens: int = 3000, retries: int = 4,
              context: str = "", title_mode: bool = False) -> Tuple[str, Optional[str]]:
    key = os.environ.get("MINIMAX_API_KEY")
    if not key:
        return "", "没设 MINIMAX_API_KEY"
    sys_msg = TITLE_SYSTEM if title_mode else SYSTEM
    if title_mode:
        sys_msg += "\n\n只输出译好的标题，不要加任何前后缀。"
        if context:
            sys_msg += f"\n\n（层级背景，**不要出现在译文里**：{context}）"
    elif context:
        sys_msg += f"\n\n（你正在翻译的这一段，属于章节：{context}。这只是背景，**不要译、不要出现在译文里**。）"
    body = {
        "model": MODEL,
        "messages": [{"role": "system", "content": sys_msg}, {"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.2,
    }
    last = ""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                f"{API}/chat/completions",
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
            )
            with urllib.request.urlopen(req, timeout=180) as r:
                data = json.loads(r.read().decode("utf-8"))
            choice = (data.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            content = msg.get("content")
            if content:
                return content, None
            # M3 系列默认先思考，思考 token 也占预算；不够就加倍重来
            if msg.get("reasoning_content") and max_tokens < 12000:
                body["max_tokens"] = min(12000, max_tokens * 3)
                max_tokens = body["max_tokens"]
                continue
            last = f"模型没吐正文（finish_reason={choice.get('finish_reason')}）"
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code in (429, 500, 502, 503):
                time.sleep(2.0 * (attempt + 1))
                continue
        except Exception as e:  # noqa: BLE001
            last = repr(e)[:120]
            time.sleep(1.5 * (attempt + 1))
    return "", last


def _math_seq(text: str) -> List[str]:
    return _MATH_RE.findall(text or "")


def _nums(text: str) -> List[str]:
    return _NUM_RE.findall(re.sub(r"⟦MATH:.*?⟧⟧", " ", text or ""))


def translate_text(text: str, context: str = "", kind: str = "text") -> Tuple[Optional[str], Optional[str], Dict[str, Any]]:
    """翻一段。返回 (译文, 错误, 校验信息)。

    校验：
      · 公式占位符个数与顺序必须一致（不一致 = 译文坏了 → 返回 None 让调用方重试）
      · 数字集合：译文多出来的数字记成警告（不阻断，但要人看一眼）
    """
    text = (text or "").strip()
    if not text:
        return None, "空文本", {}
    src_math = _math_seq(text)
    # ⚠️ 章节语境**必须放 system 消息**，不能和正文拼在 user 消息里
    #    （2026-11-26 实测踩中：拼在一起时模型把「（这一段所在的章节：总体结果）」
    #    也当成正文给翻了，章节标题里就多出这一行提示词）。
    if kind == "title":
        out, err = _call_llm(f"章节标题：{text}", context=context, title_mode=True)
    else:
        out, err = _call_llm(text, context=context)
    if err or not out:
        return None, err or "空输出", {}
    out = out.strip()
    # 清洗：模型偶尔把「译文：」「（本段所在章节：X）」这类前后缀复读出来。
    for pre in ("译文：", "翻译：", "译文:", "翻译:"):
        if out.startswith(pre):
            out = out.split("：", 1)[1].strip() if "：" in out else out.split(":", 1)[1].strip()
    out = re.sub(r"^（[^）]{0,40}章节[:：][^）]{0,60}）\s*", "", out)
    out = re.sub(r"^\(?[（(][^）)]{0,40}所在章节[^）)]{0,20}[）)]\s*", "", out)
    out_math = _math_seq(out)
    if len(out_math) != len(src_math) or out_math != src_math:
        return None, "公式占位符对不上", {"src": len(src_math), "out": len(out_math)}
    src_nums = set(_nums(text))
    extra = sorted(set(_nums(out)) - src_nums)
    return out, None, {"extra_numbers": extra, "src_numbers": len(src_nums)}


# ── 逐篇翻译 ───────────────────────────────────────────────────────────
def _blocks_to_translate(doc: Dict[str, Any]):
    """产出 [(位置, 种类, 文本, 章节标题)]。

    哪些翻、哪些不翻：
      p / list / quote 的 t      → 翻
      fig 的 caption             → 翻
      table 的 caption           → 翻；**rows 不翻**（数字和指标名）
      eq                        → 不翻（LaTeX）
      section title             → 翻（标题是目录和检索的一部分）
    """
    jobs = []
    for si, sec in enumerate(doc.get("sections", [])):
        st = sec.get("title") or ""
        jobs.append(("sec", si, st, st))
        for bi, blk in enumerate(sec.get("blocks", [])):
            k = blk.get("k")
            if k in ("p", "list", "quote") and (blk.get("t") or "").strip():
                jobs.append(("blk", (si, bi), blk["t"], st))
            elif k == "fig" and (blk.get("caption") or "").strip():
                jobs.append(("fig", (si, bi), blk["caption"], st))
            elif k == "table" and (blk.get("caption") or "").strip():
                jobs.append(("table", (si, bi), blk["caption"], st))
    return jobs


def translate_paper(pid: str, force: bool = False) -> Dict[str, Any]:
    """翻译一篇论文 → 中文 doc。带缓存（断点续跑）。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / f"{pid}.json"
    if cache.exists() and not force:
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass

    doc = db.load_paper(pid)
    if not doc:
        raise SystemExit(f"库里没有 {pid}")

    jobs = _blocks_to_translate(doc)
    # 去重（同一段文本在论文里可能出现多次，只翻一次）
    uniq: Dict[tuple, tuple] = {}
    for _kind, _pos, text, _st in jobs:
        key = ("sec" if _kind == "sec" else "txt", text)
        if key not in uniq:
            uniq[key] = (_kind, text, _st)
    texts = list(uniq.keys())
    print(f"  {pid}: {len(texts)} 段待译（全文 {len(jobs)} 个可译单元）", flush=True)

    results: Dict[tuple, str] = {}
    warns: List[Dict[str, Any]] = []
    done = [0]

    def work(item) -> None:
        kind_tag, text, ctx = item
        for attempt in range(3):
            zh, err, info = translate_text(text, ctx, kind="title" if kind_tag == "sec" else "text")
            if zh:
                results[key] = zh
                if info.get("extra_numbers"):
                    warns.append({"text": text[:120], "extra_numbers": info["extra_numbers"]})
                break
            time.sleep(1.0)
        else:
            results[key] = ""      # 译不出来就留空，别编
        done[0] += 1
        if done[0] % 25 == 0:
            print(f"    {done[0]}/{len(texts)}", flush=True)

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        list(ex.map(work, texts))

    # 组装中文 doc
    out = json.loads(json.dumps(doc))     # 深拷贝
    out["title"] = doc.get("title")       # 论文标题保留原名（专有名词）
    out["lang"] = "zh"
    out["title_zh"] = ""
    for si, sec in enumerate(out.get("sections", [])):
        st = sec.get("title") or ""
        sec["title"] = results.get(("sec", st), st)
        for blk in sec.get("blocks", []):
            k = blk.get("k")
            if k in ("p", "list", "quote"):
                t = blk.get("t") or ""
                if t.strip():
                    blk["t"] = results.get(("txt", t), t)
            elif k == "fig" and (blk.get("caption") or "").strip():
                c = blk["caption"]
                blk["caption"] = results.get(("txt", c), c)
            elif k == "table" and (blk.get("caption") or "").strip():
                c = blk["caption"]
                blk["caption"] = results.get(("txt", c), c)
    out["translation_warnings"] = warns
    out["translation_blocks"] = len(texts)
    out["translation_missing"] = sum(1 for v in results.values() if not v)

    cache.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"    ✓ {pid} 译完：{len(texts) - out['translation_missing']}/{len(texts)} 段，"
          f"警告 {len(warns)} 条", flush=True)
    return out


def translate_all(pids: List[str], force: bool = False) -> Dict[str, Any]:
    """翻译多篇并存进 papers.zh_json。"""
    summary = {}
    for pid in pids:
        try:
            zh = translate_paper(pid, force=force)
        except SystemExit as e:
            print(f"  ✗ {e}")
            continue
        db.save_zh_paper(pid, zh)
        summary[pid] = {
            "blocks": zh.get("translation_blocks"),
            "missing": zh.get("translation_missing"),
            "warnings": len(zh.get("translation_warnings") or []),
        }
    return summary
