"""zh.py —— 论文中文版生成器。

为什么需要（2026-11-26 实测）：
  · arXiv 原生只有英文，没有官方中文版；
  · **中文读者问的问题，BM25 在英文原文上根本匹配不到** ——
    「推理速度快多少」在英文论文里一个 token 都命中不了，
    之前只能靠每次提问前先让模型翻一版检索词（多花一次调用、还可能翻偏）。
  · 有了中文版，检索层可以直接跑在中文上，答案也是中文，出处仍指回英文原文。

三条硬规矩（都是踩过坑换来的）：
  1. **公式不译。** 行内公式是 ⟦MATH:…⟧ 占位符，译文里必须**原样保留**（内容一个字不改），
     少一个或多一个都算译文坏了 —— 宁可重译这一段。
     ⚠️ 校验比的是**多重集**不是顺序：中文语序和英文不同，模型把两个占位符对调位置是正常的，
     占位符自带公式、逐个渲染，顺序不影响正确性（2026-11-26 实测，按顺序判会误杀好译文）。
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
2. **方法名/系统名/数据集名/产品名一律原样保留英文，一个字都不改**
   （Coconut、V-JEPA 2、Qwen-3、GSM8k、ProsQA、Q-SET…）。
   **严禁意译** —— Coconut 不能译成「椰子」，Clooze 不能译成「闭合」，
   翻译它们只会让读者查不到原论文。
3. 只有**通用术语**才按下面这张表译：
%s
4. 标题是「方法名: 描述」这种结构时，方法名原样保留、冒号照留、后面的描述照译，
   例如 `Coconut: Chain of Continuous Thought` → `Coconut：连续思维链`。
5. 标题短也要译完整，**绝不要输出「（无正文）」「(empty)」这类占位**。

标题译文示例：
- Introduction → 引言
- Related Work → 相关工作
- Experimental Setup → 实验设置
- Overall Results → 总体结果
- Chain of Continuous Thought → 连续思维链
- Clock-Time Reasoning Efficiency Metric → 推理时延效率指标
- Empirical Results with Coconut → Coconut 的实证结果
""" % _TERM_LINE

# 数学占位符
_MATH_RE = re.compile(r"⟦MATH:.*?⟧⟧", re.S)
_NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")

# 思维链标签：模型偶尔会把「思考过程」直接吐进 content（2026-11-26 实测踩中）。
# 标签里可能夹零宽字符（</mm:think>），所以正则要宽松。
_THINK_RE = re.compile(r"<\s*/?\s*mm:think\s*>|<\s*/?\s*think\s*>", re.I)
_CJK_RE = re.compile(r"[一-鿿]")


def _strip_think(text: str) -> str:
    """把思维链从正文里剥掉，只留最后的答案。

    为什么要剥（2026-11-26 实测）：
      M3 系列默认先思考。多数时候思考在 `reasoning_content` 字段里，
      但**偶尔**模型会把 `<think>…</think>` 整块塞进 `content`，
      而思考里还夹着半截译文 —— 整段当译文收下，
      中文摘要就变成「半截译文 + 一段英文原文 + <think> 尾巴 + 完整译文」。
    """
    if not text:
        return ""
    if _THINK_RE.search(text):
        # 取最后一个闭合标签之后的内容（答案总在思考之后）
        parts = _THINK_RE.split(text)
        tail = parts[-1].strip()
        if tail:
            return tail
        return parts[-2].strip() if len(parts) >= 2 else ""
    return text.strip()


def _looks_untranslated(src: str, out: str) -> Optional[str]:
    """译文看着像没翻（思维链漏出来 / 模型把原文抄回来了）→ 返回原因，否则 None。

    判据两条，都是实测踩出来的：
      ① 源文里有一长段**原样**出现在译文里（>=40 字符）→ 抄回来了；
      ② 长段落里中文字符占比过低（<12%）→ 基本没翻。

    ⚠️ 判据要在**去掉公式占位符之后**做（2026-11-26 实测踩中）：
      论文里有一整段是纯公式（`⟦MATH:…⟧⟧ ⟦MATH:…⟧⟧`），
      这种段落的"译文"本来就和原文一模一样 —— 按原文判会把它当没译，
      重试 3 次全失败 → **译文变空 → 整段在阅读器里消失**。
      所以：先剥公式，剩下的"散文"太短就不判。
    """
    if not out:
        return "空译文"
    src_prose = re.sub(r"⟦MATH:.*?⟧⟧", " ", src or "", flags=re.S)
    out_prose = re.sub(r"⟦MATH:.*?⟧⟧", " ", out, flags=re.S)
    src_flat = re.sub(r"\s+", " ", src_prose).strip()
    out_flat = re.sub(r"\s+", " ", out_prose).strip()
    if len(src_flat) >= 40:
        if src_flat[:40] in out_flat:
            return "译文里出现了原文原句（没翻）"
    if len(out_flat) >= 60:
        cjk = len(_CJK_RE.findall(out_flat))
        letters = sum(1 for c in out_flat if c.isalpha())
        if letters and cjk / max(1, cjk + letters) < 0.12:
            return "译文里几乎没有中文（没翻）"
    return None


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
                # ⚠ 模型偶尔把思维链整块吐进 content（2026-11-26 实测踩中，
                #   中文摘要变成「半截译文 + 英文原文 + think 尾巴 + 完整译文」），
                #   必须先剥掉思考、只留最后一段答案。
                content = _strip_think(content)
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
    # ⚠️ 比的是**多重集**，不是顺序（2026-11-26 实测踩中）：
    #   占位符自带公式（`⟦MATH:x⟧⟧` 自己就是完整的式子），阅读器逐个渲染，
    #   所以顺序变了不影响正确性。
    #   而中文语序常和英文不同 —— 模型把「当 i<t<j 时，M(...) 尚未定义」
    #   调成自然中文时，两个占位符会**对调位置**。
    #   按顺序判死会把这种**好译文**判成失败，重试 3 次全失败 → 整段变空。
    if sorted(out_math) != sorted(src_math):
        return None, "公式占位符对不上", {"src": len(src_math), "out": len(out_math)}
    # 译文看着像没翻 → 当失败，让调用方重试（别把半成品收进库里）
    bad = _looks_untranslated(text, out)
    if bad:
        return None, bad, {}
    src_nums = set(_nums(text))
    extra = sorted(set(_nums(out)) - src_nums)
    return out, None, {"extra_numbers": extra, "src_numbers": len(src_nums)}


# ── 逐篇翻译 ───────────────────────────────────────────────────────────
def segments(doc: Dict[str, Any]) -> List[Tuple[tuple, str, str, str]]:
    """产出 [(key, kind, text, ctx)] —— 要翻的**全部**单元。

    key 的作用有两个：去重，以及**复用旧译文**（重建时只重译真正变了的段落）。
    kind：'sec'（小节标题）/ 'title'（论文标题）/ 'text'（正文、摘要、图注、列表项）。

    ⚠️ 别把「哪些翻」写死在一处 —— 2026-11-26 实测漏了三处：
      ① 目录（toc）没跟着小节标题走 → 左边目录全是英文；
      ② 摘要没翻 → 题名下面一大段英文；
      ③ 列表项（items 数组）没翻 → 一串英文条目夹在中文正文里。
    """
    segs: List[Tuple[tuple, str, str, str]] = []
    for sec in doc.get("sections", []):
        st = sec.get("title") or ""
        if st.strip():
            segs.append((("sec", st), "sec", st, st))
        for blk in sec.get("blocks", []):
            k = blk.get("k")
            if k in ("p", "quote"):
                t = blk.get("t") or ""
                if t.strip():
                    segs.append((("txt", t), "text", t, st))
            elif k == "list":
                for item in blk.get("items") or []:
                    it = item or ""
                    if it.strip():
                        segs.append((("txt", it), "text", it, st))
            elif k in ("fig", "table"):
                c = blk.get("caption") or ""
                if c.strip():
                    segs.append((("cap", c), "text", c, st))
    title = doc.get("title") or ""
    if title.strip():
        segs.append((("meta", "title"), "title", title, "论文标题"))
    abstract = doc.get("abstract") or ""
    if abstract.strip():
        segs.append((("meta", "abstract"), "text", abstract, "论文摘要"))
    return segs


def assemble(doc: Dict[str, Any], results: Dict[tuple, str],
             warns: Optional[List[Dict[str, Any]]] = None,
             missing: int = 0, n_segments: int = 0) -> Dict[str, Any]:
    """英文 doc + 译文映射 → 中文 doc（纯函数，不调模型）。

    约定：
      · `title` 保留英文原名（专有名词），中文标题另存 `title_zh`；
      · 摘要中文版存 `abstract_zh`，英文摘要留在 `abstract`（可对照）；
      · **目录条目按 id 直接复用小节译文** —— toc 的 id 与 sections 的 id 一一对应，
        所以目录中文化**零成本**，而且两边永远一致。
    """
    out = json.loads(json.dumps(doc))
    out["lang"] = "zh"
    out["title_zh"] = results.get(("meta", "title"), "") or ""
    out["abstract_zh"] = results.get(("meta", "abstract"), "") or ""
    for sec in out.get("sections", []):
        st = sec.get("title") or ""
        if st.strip():
            zt = results.get(("sec", st), "")
            sec["title"] = zt if zt.strip() else st
        for blk in sec.get("blocks", []):
            k = blk.get("k")
            if k in ("p", "quote"):
                t = blk.get("t") or ""
                if t.strip():
                    zt = results.get(("txt", t), "")
                    # 译不出来就**保留原文**，别留空 —— 空段在阅读器里是「整段消失」
                    blk["t"] = zt if zt.strip() else t
            elif k == "list":
                blk["items"] = [
                    (results.get(("txt", it), "") if (it or "").strip() else "") or it
                    for it in (blk.get("items") or [])]
            elif k in ("fig", "table"):
                c = blk.get("caption") or ""
                if c.strip():
                    zc = results.get(("cap", c), "")
                    blk["caption"] = zc if zc.strip() else c
    # 目录：条目 id 与小节 id 一一对应 → 直接换成小节译文
    by_id = {s.get("id"): s.get("title") for s in out.get("sections", [])}
    for t in out.get("toc", []):
        if t.get("id") in by_id:
            t["title"] = by_id[t["id"]]
    out["translation_warnings"] = warns or []
    out["translation_blocks"] = n_segments
    out["translation_missing"] = missing
    return out


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

    segs = segments(doc)
    # 去重（同一段文本在论文里可能出现多次，只翻一次）
    uniq: Dict[tuple, tuple] = {}
    for key, kind, text, ctx in segs:
        if key not in uniq:
            uniq[key] = (kind, text, ctx)
    items = list(uniq.items())
    print(f"  {pid}: {len(items)} 段待译（全文 {len(segs)} 个可译单元）", flush=True)

    results: Dict[tuple, str] = {}
    warns: List[Dict[str, Any]] = []
    done = [0]

    def work(item) -> None:
        key, (kind_tag, text, ctx) = item
        for attempt in range(3):
            zh, err, info = translate_text(text, ctx,
                                           kind="title" if kind_tag in ("sec", "title") else "text")
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
            print(f"    {done[0]}/{len(items)}", flush=True)

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        list(ex.map(work, items))

    out = assemble(doc, results, warns,
                   missing=sum(1 for v in results.values() if not v),
                   n_segments=len(items))

    cache.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"    ✓ {pid} 译完：{len(items) - out['translation_missing']}/{len(items)} 段，"
          f"警告 {len(warns)} 条", flush=True)
    return out


def _old_translation_map(old_en: Dict[str, Any], old_zh: Dict[str, Any]) -> Dict[tuple, str]:
    """旧英文 doc + 旧中文 doc → {key: 旧译文}。

    按**英文原文**做键（不是按位置）：位置会因为修 bug 后节数变化而整体错位
    （2604.12493 删掉一节假「正文」后，后面所有小节的序号都变了）。

    ⚠️ **译文和原文一样的不算译文**（2026-11-26 实测踩中）：
      旧版根本没翻列表项（`items` 数组），所以旧中文版里那些条目 = 英文原文。
      要是把它们当"已有译文"复用，重建后这些条目**还是英文**，
      而且看起来"重建过了、段段都有译文"，极难发现。
    """
    m: Dict[tuple, str] = {}

    def put(key, en, zh):
        en = en or ""
        zh = zh or ""
        if not en.strip() or not zh.strip():
            return
        if zh.strip() == en.strip():      # 没翻过 → 当作没有译文
            return
        m.setdefault(key, zh)

    for so, sz in zip(old_en.get("sections") or [], old_zh.get("sections") or []):
        put(("sec", so.get("title") or ""), so.get("title") or "", sz.get("title") or "")
        for bo, bz in zip(so.get("blocks") or [], sz.get("blocks") or []):
            k = bo.get("k")
            if k in ("p", "quote"):
                put(("txt", bo.get("t") or ""), bo.get("t") or "", bz.get("t") or "")
            elif k == "list":
                for en, zh in zip(bo.get("items") or [], bz.get("items") or []):
                    put(("txt", en or ""), en or "", zh or "")
            elif k in ("fig", "table"):
                put(("cap", bo.get("caption") or ""), bo.get("caption") or "",
                    bz.get("caption") or "")
    return m


def rebuild_paper(pid: str, old_en: Dict[str, Any], old_zh: Dict[str, Any],
                  force: bool = False) -> Dict[str, Any]:
    """重建中文版：英文原文**变了哪段就重译哪段**，没变的复用旧译文。

    为什么需要（2026-11-26）：清洗器修 bug 后英文原文变了（方法名找回来了、
    公式宏展开了、假「正文」节没了），整篇重译 = 上千次模型调用；
    复用旧译文把成本压到只有真正变了的那些段。

    复用判据是**英文原文完全一致** —— 一致才复用，绝不"差不多就算"，
    否则会把旧译文套到新句子上（内容对不上，比没翻还糟）。
    """
    new_en = db.load_paper(pid)
    if not new_en:
        raise SystemExit(f"库里没有 {pid}")
    old_map = _old_translation_map(old_en, old_zh or {"sections": []})

    uniq: Dict[tuple, tuple] = {}
    for key, kind, text, ctx in segments(new_en):
        uniq.setdefault(key, (kind, text, ctx))
    items = list(uniq.items())

    results: Dict[tuple, str] = {}
    todo: List[Tuple[tuple, tuple]] = []
    for key, val in items:
        if not force and old_map.get(key):
            results[key] = old_map[key]
        else:
            todo.append((key, val))
    print(f"  {pid}: 共 {len(items)} 段 → 复用 {len(items) - len(todo)}，重译 {len(todo)}", flush=True)

    warns: List[Dict[str, Any]] = []
    done = [0]

    def work(item) -> None:
        key, (kind_tag, text, ctx) = item
        for _attempt in range(3):
            zh, err, info = translate_text(text, ctx,
                                           kind="title" if kind_tag in ("sec", "title") else "text")
            if zh:
                results[key] = zh
                if info.get("extra_numbers"):
                    warns.append({"text": text[:120], "extra_numbers": info["extra_numbers"]})
                break
            time.sleep(1.0)
        else:
            results[key] = ""
        done[0] += 1
        if done[0] % 25 == 0:
            print(f"    {done[0]}/{len(todo)}", flush=True)

    if todo:
        with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
            list(ex.map(work, todo))

    out = assemble(new_en, results, warns,
                   missing=sum(1 for v in results.values() if not v),
                   n_segments=len(items))
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
