#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fix_zh_titles.py —— 只补译章节标题（正文不动）。

为什么单独一步：2026-11-26 第一轮翻译时，短标题（"Introduction"）用的是正文那套
提示词，模型以为收到的是空片段，回了「（无正文）」/「(empty)」。
后来给标题加了专门的提示词，正文翻译没问题、但已译好的那几篇标题还是坏的。
所以：正文保留，标题重译。

幂等：跑第二遍不会重复调用（已正确的标题会跳过）。
"""
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from extensions import db, zh  # noqa: E402

ALL = "--all" in sys.argv          # --all：所有标题都重译（白名单式挑错会漏）
ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]

BAD = ("（无正文）", "(empty)", "(无内容)", "(空)", "（无）", "（无译文）", "(无)",
        "未译", "翻译失败", "正文", "（翻译失败）")


def _is_bad(cur: str, en_title: str) -> bool:
    """这个中文标题是不是「占位/翻坏了」。

    2026-11-26 实测踩到：短标题用正文那套提示词时，模型会回
    「（无正文）」「(empty)」「（无）」「（无译文）」等等各种花样，
    写死一个白名单必然漏 —— 改用「短 + 含可疑词」判定。
    """
    c = (cur or "").strip()
    if not c:
        return True
    if c in BAD:
        return True
    if len(c) <= 12 and re.search(r"无正文|无译文|empty|未译|翻译失败|^\W*$|^[（(]无[）)]$", c):
        return True
    return False


def _only_math(t: str) -> bool:
    """标题只是一个公式占位符 —— 译它没意义，原样留着。"""
    stripped = re.sub(r"\s+", "", t or "")
    return bool(stripped) and set(stripped) <= set("⟦MATH:⟧()[]{}^_ ")


def fix_one(pid: str) -> dict:
    en = db.load_paper(pid)
    zh_doc = db.load_zh_paper(pid)
    if not en or not zh_doc:
        return {"pid": pid, "fixed": 0, "skipped": 0, "note": "没这篇/没中文版"}

    en_secs = list(en.get("sections") or [])
    zh_secs = list(zh_doc.get("sections") or [])
    jobs = []
    for si, sec in enumerate(en_secs):
        if si >= len(zh_secs):
            break
        en_title = (sec.get("title") or "").strip()
        cur = (zh_secs[si].get("title") or "").strip()
        if not en_title or len(en_title) < 2:
            continue
        if _only_math(en_title):
            continue                      # 只有公式的标题不译
        if not ALL and cur and not _is_bad(cur, en_title) and cur != en_title:
            continue                      # 已经译好了，跳过（--all 模式不跳过）
        jobs.append((si, en_title))

    if not jobs:
        return {"pid": pid, "fixed": 0, "skipped": 0, "note": "标题都正常"}

    def work(job):
        si, en_title = job
        out, err, _ = zh.translate_text(en_title, context="论文小节标题", kind="title")
        return si, en_title, (out or ""), err

    fixed = 0
    with ThreadPoolExecutor(max_workers=zh.CONCURRENCY) as ex:
        for si, en_title, out, err in ex.map(work, jobs):
            if out and not _is_bad(out, en_title):
                zh_secs[si]["title"] = out
                fixed += 1
            else:
                zh_secs[si]["title"] = en_title      # 译不出来就留原名，别编

    zh_doc["sections"] = zh_secs
    db.save_zh_paper(pid, zh_doc)
    cache = zh.CACHE_DIR / f"{pid}.json"
    cache.write_text(json.dumps(zh_doc, ensure_ascii=False), encoding="utf-8")
    return {"pid": pid, "fixed": fixed, "skipped": len(jobs) - fixed}


def main() -> int:
    pids = ARGS or [r["id"] for r in db.list_papers()]
    total = 0
    for pid in pids:
        r = fix_one(pid)
        total += r.get("fixed", 0)
        print(f"  {r['pid']:<14} 修好标题 {r.get('fixed',0):>3} 个  {r.get('note','')}")
    print(f"共修好 {total} 个标题")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
