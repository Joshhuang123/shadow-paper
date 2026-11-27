"""用**免费通道**（MyMemory）补齐中文版缺的标题/摘要，并标记为「待升级」。

为什么需要（2026-11-27）：
    MiniMax 的 Token Plan 用量被打满（429「已达到 Token Plan 用量上限」），
    高质量翻译暂时跑不动 —— 但界面上的**论文标题和摘要**是英文，很显眼。
    MyMemory 是免费的、已经接在「翻译整句」功能上的通道，先顶上。

⚠️ **质量有差距**（MyMemory 是众包翻译记忆，学术术语会错：
   "Training agents and collectives" → "培训客服代表和团队" ✗）。
    所以补上的都记进 `zh_fallback` 列表，
    等 MiniMax 配额恢复后由 `tools/fix_zh_quality.py` **重新翻译覆盖**。

用法：.venv/bin/python tools/fill_zh_fallback.py
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from extensions import db, translate, zh  # noqa: E402

ALL_PIDS = ["2412.06769", "2506.09985", "2512.21711",
            "2603.09184", "2604.12493", "2608.13570", "2611.32606"]


def has_cjk(s):
    return any("一" <= c <= "鿿" for c in s or "")


def _chunks(text, limit=450):
    """按句切块（MyMemory 真实上限 **500 字符/次**，超了直接报错）。

    ⚠️ 截断的译文会让人以为"摘要就这么多"—— 实测 1253 字符的英文摘要
    被截到 900 → 译文缺尾巴。所以长文本必须切块。
    """
    out, buf = [], ""
    for piece in re.split(r"(?<=[.。!?！？])\s+", text or ""):
        if len(buf) + len(piece) + 1 > limit and buf:
            out.append(buf)
            buf = piece
        else:
            buf = (buf + " " + piece).strip() if buf else piece
    if buf:
        out.append(buf)
    return out


def mt(text, chunked=False):
    """MyMemory 翻译；失败返回空串。"""
    text = (text or "").strip()
    if not text:
        return ""
    try:
        if chunked and len(text) > 450:
            parts = []
            for ch in _chunks(text):
                try:
                    r = translate.translate(ch)
                except Exception:  # noqa: BLE001
                    return ""          # 有一块没译成 → 整篇不算数（半截摘要比英文摘要更糟）
                t = (r.get("text") or "").strip()
                if not t:
                    return ""
                parts.append(t)
            return " ".join(parts)
        r = translate.translate(text)
        return (r.get("text") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def main():
    pids = sys.argv[1:] or ALL_PIDS
    for pid in pids:
        en = db.load_paper(pid)
        z = db.load_zh_paper(pid)
        if not en or not z:
            continue
        marked = set(z.get("zh_fallback") or [])
        changed = 0

        # 1) 论文标题
        if not (z.get("title_zh") or "").strip():
            t = mt(en.get("title") or "")
            if t and has_cjk(t):
                z["title_zh"] = t
                marked.add("title_zh")
                changed += 1

        # 2) 摘要（被截断过的也重填：半截摘要比英文摘要更糟）
        abs_zh = (z.get("abstract_zh") or "").strip()
        if abs_zh and len(abs_zh) < len(en.get("abstract") or "") * 0.75:
            z["abstract_zh"] = ""
            marked.discard("abstract_zh")
        if not (z.get("abstract_zh") or "").strip():
            t = mt(en.get("abstract") or "", chunked=True)
            if t and has_cjk(t):
                z["abstract_zh"] = t
                marked.add("abstract_zh")
                changed += 1

        # 3) 还是英文的小节标题（纯专名/数据集名保持英文是对的，跳过）
        for i, (se, sz) in enumerate(zip(en.get("sections") or [], z.get("sections") or [])):
            en_t = (se.get("title") or "").strip()
            zh_t = (sz.get("title") or "").strip()
            if not en_t or zh_t == en_t or has_cjk(zh_t):
                continue
            if len(en_t) < 4 or en_t.endswith(".") and len(en_t) < 12:
                pass
            t = mt(en_t)
            if t and has_cjk(t):
                sz["title"] = t
                marked.add(f"sections[{i}].title")
                changed += 1

        # 目录跟着小节走
        by_id = {s.get("id"): s.get("title") for s in z.get("sections", [])}
        for t in z.get("toc", []):
            if t.get("id") in by_id:
                t["title"] = by_id[t["id"]]

        z["zh_fallback"] = sorted(marked)
        db.save_zh_paper(pid, z)
        (zh.CACHE_DIR / f"{pid}.json").write_text(
            json.dumps(z, ensure_ascii=False), encoding="utf-8")
        print(f"{pid}: 补 {changed} 处（累计待升级 {len(marked)}）")


if __name__ == "__main__":
    main()
