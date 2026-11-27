"""中文版质量体检 + 修复：扫出脏段落，只重译脏的。

扫什么（都是实测踩出来的）：
  · 思维链残留（`<mm:think>` 混进译文）
  · 译文里出现原文原句 / 几乎没有中文（没翻）
  · 译文为空
  · 标题里把方法名意译了（Coconut → 椰子）

用法：
    .venv/bin/python tools/fix_zh_quality.py            # 体检（只报告）
    .venv/bin/python tools/fix_zh_quality.py --fix      # 体检 + 重译 + 存回
"""
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from extensions import db, zh  # noqa: E402

ALL_PIDS = ["2412.06769", "2506.09985", "2512.21711",
            "2603.09184", "2604.12493", "2608.13570", "2611.32606"]

# 专有名词被意译的典型样本（正则）：方法名原样保留是硬要求
_BAD_TITLE_RE = re.compile(r"椰子|闭环语义|闭合推断|词元嵌入器")

# 参考文献/致谢这类段落**本来就该是英文**（作者名、论文标题、期刊名），
# 不参与"没翻"判定 —— 否则整段参考文献都会被误报。
# ⚠️ 这里**不能**把 acknowledgements 放进跳过列表（2026-11-26 实测踩中）：
#   "Acknowledgement" 这一节本身该译成「致谢」，跳过它就永远译不到；
#   参考文献条目是**块级**特征（作者 + 年份），用 _is_reference_text 逐块认就行。
_SKIP_SECTION_RE = re.compile(
    r"^\s*(references?|bibliography|参考文献|附录|appendix)\b", re.I)

# 参考文献条目长这样："Achiam, Josh, Steven Adler, Sandhini Agarwal, et al. 2023. “Gpt-4 …"
# ⚠️ LaTeX 的 bibliography 排在最后一个 \section 之后 → 会被塞进**最后一节**里，
#    所以不能只按「小节标题是不是 References」来跳，得按**条目特征**认。
def _is_reference_text(t: str) -> bool:
    """像不像一条参考文献（姓 + 名 + 年份）。"""
    t = (t or "").strip()
    if len(t) < 40 or len(t) > 1500:
        return False
    head = t[:260]
    if not re.match(r"^[A-ZÀ-ɏ][\w'’\-]+,", head):
        return False
    return bool(re.search(r"\b(19|20)\d{2}\b", head))


def pairs_of(en_doc, zh_doc):
    """产出 [(位置说明, 英文原文, 中文译文, kind, ctx)]，位置说明用来写回。"""
    out = []
    for si, (se, sz) in enumerate(zip(en_doc.get("sections") or [],
                                      zh_doc.get("sections") or [])):
        # 参考文献这类小节整节跳过（不判、不改）
        if _SKIP_SECTION_RE.match(se.get("title") or ""):
            continue
        out.append((f"sections[{si}].title", se.get("title") or "",
                    sz.get("title") or "", "title", se.get("title") or ""))
        for bi, (be, bz) in enumerate(zip(se.get("blocks") or [],
                                         sz.get("blocks") or [])):
            k = be.get("k")
            if k in ("p", "quote"):
                if _is_reference_text(be.get("t") or ""):
                    continue          # 参考文献条目：本来就该是英文
                out.append((f"sections[{si}].blocks[{bi}].t", be.get("t") or "",
                            bz.get("t") or "", "text", se.get("title") or ""))
            elif k == "list":
                for li, (ie, iz) in enumerate(zip(be.get("items") or [],
                                                  bz.get("items") or [])):
                    out.append((f"sections[{si}].blocks[{bi}].items[{li}]",
                                ie or "", iz or "", "text", se.get("title") or ""))
            elif k in ("fig", "table"):
                out.append((f"sections[{si}].blocks[{bi}].caption",
                            be.get("caption") or "", bz.get("caption") or "",
                            "text", se.get("title") or ""))
    out.append(("title_zh", en_doc.get("title") or "", zh_doc.get("title_zh") or "",
                "title", "论文标题"))
    out.append(("abstract_zh", en_doc.get("abstract") or "",
                zh_doc.get("abstract_zh") or "", "text", "论文摘要"))
    # ⚠️ 用**免费通道**（MyMemory）临时填过的，必须**重新用高质量模型翻一遍**：
    #    MyMemory 是众包翻译，学术术语会错（"collectives" → "客服代表"）。
    #    它们现在是中文、逃过了常规判据 → 靠 zh_fallback 名单捞回来
    #    （中文值置空 → 判据判成"译文为空" → 强制重译）。
    fb = {}
    for path in zh_doc.get("zh_fallback") or []:
        if path == "title_zh":
            fb[path] = (path, en_doc.get("title") or "", "", "title", "论文标题")
        elif path == "abstract_zh":
            fb[path] = (path, en_doc.get("abstract") or "", "", "text", "论文摘要")
        else:
            m = re.match(r"sections\[(\d+)\]\.title", path)
            if m and int(m.group(1)) < len(en_doc.get("sections") or []):
                i = int(m.group(1))
                fb[path] = (path, en_doc["sections"][i].get("title") or "", "",
                            "title", "小节标题")
    if fb:
        merged = [fb.get(pair[0], pair) for pair in out]
        known = {pair[0] for pair in out}
        merged.extend(pair for path, pair in fb.items() if path not in known)
        out = merged
    return out


def is_bad(en, cn, kind):
    if not (en or "").strip():
        return None          # 英文本身就空（比如论文没写图注）→ 不是问题
    if not (cn or "").strip():
        return "译文为空"
    if zh._THINK_RE.search(cn):
        return "思维链残留"
    if kind == "title" and _BAD_TITLE_RE.search(cn):
        return "专有名词被意译"
    # ⚠️ **CamelCase 的单词**是专有名词/基准名（AdvBench、PlanBench、DocSweep…），
    #    保留英文是对的 → 不判、不翻（翻了会变成"对抗性基准"这种半吊子）。
    #    ⚠️ 判据不能是"单个词" —— "Experiments"/"Dataset"/"Baselines" 也是单个词，
    #    但它们是普通词、**该翻**。真正的区别是**中间有大写**（CamelCase）。
    t = cn.strip().rstrip(".")
    if kind == "title" and " " not in t and re.match(r"^[A-Z][a-z]*[A-Z]", t):
        return None
    # ⚠️ 标题单独判「原文原样」：
    #   通用判据要求原文 ≥40 字符才查「原文原句」，
    #   而标题大多只有 10–30 字符 → 判据全都不触发 → **未译的标题被放过**。
    #   实测「Limitations」「Experiments」这类短标题整批没翻（35 个）。
    #   判据：译文 == 原文，且原文里有**小写英文单词**（不是纯缩写/专有名词）。
    if kind == "title" and cn.strip() == en.strip() and re.search(r"[a-z]", en):
        return "标题没翻"
    why = zh._looks_untranslated(en, cn)
    if why:
        return why
    return None


def set_by_path(doc, path, value):
    """path 形如 sections[3].blocks[2].items[0] / title_zh。

    ⚠️ 正则里 items 分支整体是 `items\[(\d+)\]`（**带下标**），
    所以 group(3) 是 "items[0]" 而不是 "items" ——
    早先按 `field == "items"` 判断，结果把译文写成了新键 `blk["items[0]"]`，
    列表项**一个都没改**，还留了一堆垃圾键。
    """
    if path == "title_zh":
        doc["title_zh"] = value
        return
    if path == "abstract_zh":
        doc["abstract_zh"] = value
        return
    # 小节标题：sections[N].title
    m2 = re.match(r"sections\[(\d+)\]\.title$", path)
    if m2:
        doc["sections"][int(m2.group(1))]["title"] = value
        return
    m = re.match(r"sections\[(\d+)\]\.blocks\[(\d+)\]\.(t|caption|items\[(\d+)\])", path)
    if not m:
        return
    si, bi = int(m.group(1)), int(m.group(2))
    field = m.group(3)
    blk = doc["sections"][si]["blocks"][bi]
    if field.startswith("items"):
        blk["items"][int(m.group(4))] = value
    else:
        blk[field] = value


def clean_junk_keys(doc):
    """清掉早先写回时留下的垃圾键（`items[0]` 这种）。"""
    n = 0
    for sec in doc.get("sections", []):
        for blk in sec.get("blocks", []):
            for k in [k for k in blk if k.startswith("items[")]:
                blk.pop(k)
                n += 1
    return n


def sync_toc(doc):
    """目录条目按 id 复用小节标题（和 zh.assemble 同一套规则）。

    ⚠️ 不做这一步，目录就会**停在旧的英文标题**上：
      修复器改的是 sections[].title，toc 是独立数组 —— 改一处不动另一处，
      左边目录和正文标题对不上（2026-11-26 实测）。
    """
    by_id = {s.get("id"): s.get("title") for s in doc.get("sections", [])}
    n = 0
    for t in doc.get("toc", []):
        if t.get("id") in by_id and t.get("title") != by_id[t["id"]]:
            t["title"] = by_id[t["id"]]
            n += 1
    return n


def main():
    fix = "--fix" in sys.argv
    pids = [a for a in sys.argv[1:] if not a.startswith("--")] or ALL_PIDS
    total_bad = 0
    for pid in pids:
        en = db.load_paper(pid)
        zh_doc = db.load_zh_paper(pid)
        if not en or not zh_doc:
            print(f"=== {pid}: 没有中文版，跳过")
            continue
        pairs = pairs_of(en, zh_doc)
        bad = []
        for path, e, c, kind, ctx in pairs:
            why = is_bad(e, c, kind)
            if why:
                bad.append((path, e, c, kind, ctx, why))
        total_bad += len(bad)
        print(f"=== {pid}: {len(pairs)} 个单元，{len(bad)} 个有问题")
        for path, e, c, kind, ctx, why in bad[:8]:
            print(f"    [{path}] {why}｜英: {e[:50]!r}｜中: {c[:50]!r}")
        # 目录同步**无条件做**：就算没有脏段落，目录也可能停在旧标题上
        # （小节标题改了、toc 没改）。所以这条不放在「有脏段落才继续」的分支里。
        toc_n = sync_toc(zh_doc)
        junk = clean_junk_keys(zh_doc)
        if not bad:
            if toc_n or junk:
                db.save_zh_paper(pid, zh_doc)
                (zh.CACHE_DIR / f"{pid}.json").write_text(
                    json.dumps(zh_doc, ensure_ascii=False), encoding="utf-8")
                print(f"    ✓ 无脏段落；同步目录 {toc_n} 条、清垃圾键 {junk} 个")
            continue
        if not fix:
            continue
        # 重译
        results = {}

        def work(item):
            path, e, _c, kind, ctx, _why = item
            for attempt in range(3):
                # 重试要换温度 + 加提示（同一温度下重试是确定性重复，等于白等）
                out, err, _info = zh.translate_text(
                    e, ctx, kind="title" if kind == "title" else "text",
                    temperature=[0.2, 0.7, 1.0][min(attempt, 2)],
                    nudge=(attempt > 0))
                if out:
                    results[path] = out
                    return
                time.sleep(1.0)
            results[path] = None

        with ThreadPoolExecutor(max_workers=zh.CONCURRENCY) as ex:
            list(ex.map(work, bad))
        toc_n += sync_toc(zh_doc)
        ok = 0
        fallback = set(zh_doc.get("zh_fallback") or [])
        for path, e, _c, kind, ctx, _why in bad:
            out = results.get(path)
            if out:
                set_by_path(zh_doc, path, out)
                fallback.discard(path)     # 高质量译文到位 → 撤掉"待升级"标记
                ok += 1
        zh_doc["zh_fallback"] = sorted(fallback)
        toc_n += sync_toc(zh_doc)      # 改完标题再同步一次
        db.save_zh_paper(pid, zh_doc)
        (zh.CACHE_DIR / f"{pid}.json").write_text(
            json.dumps(zh_doc, ensure_ascii=False), encoding="utf-8")
        print(f"    ✓ 修好 {ok}/{len(bad)}（清垃圾键 {junk}，同步目录 {toc_n} 条）")
    print(f"\n合计问题单元：{total_bad}")


if __name__ == "__main__":
    main()
