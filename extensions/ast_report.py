#!/usr/bin/env python3
"""检查 pandoc JSON AST 的论文保真度：公式 / 图 / 章节 / 参考文献 / 正文量。"""
import json
import re
import sys
from collections import Counter

path = sys.argv[1]
ast = json.load(open(path, encoding="utf-8"))
blocks = ast.get("blocks", [])

kinds = Counter(b.get("t") for b in blocks)
print("=== 顶层块统计 ===")
for k, v in kinds.most_common():
    print(f"  {k:14s} {v}")

inlines = Counter()


def walk_inlines(b):
    if not isinstance(b, dict):
        return
    for v in b.get("c", []):
        if isinstance(v, dict):
            t = v.get("t")
            if t:
                inlines[t] += 1
            walk_inlines(v)
        elif isinstance(v, list):
            for it in v:
                walk_inlines(it)


for b in blocks:
    walk_inlines(b)
print("\n=== 行内节点统计 ===")
for k, v in inlines.most_common(12):
    print(f"  {k:14s} {v}")

# 公式：Math 节点的 tex 原文
maths = []


def walk_math(x):
    if isinstance(x, dict):
        if x.get("t") == "Math":
            maths.append(x.get("c", [None, ""])[1])
        for v in x.values():
            walk_math(v)
    elif isinstance(x, list):
        for it in x:
            walk_math(it)


walk_math(blocks)
print(f"\n=== 公式：{len(maths)} 个 Math 节点 ===")
for m in maths[:8]:
    print("  ", m[:110].replace("\n", " "))

imgs = []


def walk_img(x):
    if isinstance(x, dict):
        if x.get("t") == "Image":
            imgs.append(x.get("c", [None, {}, {}])[2])
        for v in x.values():
            walk_img(v)
    elif isinstance(x, list):
        for it in x:
            walk_img(it)


walk_img(blocks)
print(f"\n=== 图片：{len(imgs)} 个 ===")
for i in imgs[:10]:
    print("  ", i)

headers = [(b.get("c", [0, "", []])[1], b.get("c", [0, "", []])[0]) for b in blocks if b.get("t") == "Header"]
print(f"\n=== 章节标题：{len(headers)} 个 ===")
for h, lvl in headers[:18]:
    print(f"  L{lvl} {h[:70]}")


def text_of(x):
    if isinstance(x, dict):
        if x.get("t") == "Str":
            return x.get("c", "")
        if x.get("t") == "Space":
            return " "
        if x.get("t") == "Math":
            return " $FORMULA$ "
        return "".join(text_of(v) for v in x.get("c", []) if not isinstance(v, (int, str)))
    if isinstance(x, list):
        return "".join(text_of(v) for v in x)
    return ""


full = " ".join(text_of(b) for b in blocks)
full = re.sub(r"\s+", " ", full)
print(f"\n=== 正文可见文字：{len(full)} 字符 ===")
for kw in ["Abstract", "Introduction", "welfare", "threshold", "0.8521", "14 workers", "References", "equilibrium"]:
    print(f"  含「{kw}」:", kw in full)
i = full.find("P(Q)")
print("\n=== 公式附近正文 ===")
print(full[i - 150:i + 260] if i >= 0 else "（没找到 P(Q)）")
