"""导入前后**逐段比对**：旧英文 doc 的每段，在新 doc 里还有没有对应。

判据用「最佳匹配的相似度」而不是「前 N 字是否原样出现」——
因为导入器修复后，旧段落**开头会变**（比如 "Coconut" 找回来了），
按前缀比对会把「改好了」误判成「丢了」。

用法：.venv/bin/python tools/diff_docs.py [阈值]
"""
import difflib
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from extensions import db  # noqa: E402

SNAP_DIR = Path.home() / "WorkBuddy" / "shadow-paper-snapshots"


def blocks_of(doc):
    out = []
    for s in doc.get("sections", []):
        for b in s.get("blocks", []):
            t = (b.get("t") or "").strip()
            if t:
                out.append((s.get("title", ""), b.get("k"), t))
    return out


def main():
    thresh = float(sys.argv[1]) if len(sys.argv) > 1 else 0.85
    snap = sorted(SNAP_DIR.glob("*.db"))[-1]
    conn = sqlite3.connect(str(snap))
    conn.row_factory = sqlite3.Row
    pids = [r["id"] for r in conn.execute("SELECT id FROM papers")]
    for pid in pids:
        row = conn.execute("SELECT data_json FROM papers WHERE id=?", (pid,)).fetchone()
        old = json.loads(row["data_json"])
        new = db.load_paper(pid)
        if not new:
            print(f"=== {pid}: 新库里没有")
            continue
        old_b = blocks_of(old)
        new_b = blocks_of(new)
        lost = []
        for sec, k, t in old_b:
            if len(t) < 60:
                continue
            best = 0.0
            best_txt = ""
            for _s2, _k2, t2 in new_b:
                if abs(len(t2) - len(t)) > max(120, len(t) * 0.6):
                    continue
                r = difflib.SequenceMatcher(None, t[:600], t2[:600]).ratio()
                if r > best:
                    best, best_txt = r, t2
            if best < thresh:
                lost.append((sec, k, t, best_txt, best))
        print(f"=== {pid}: 旧 {len(old_b)} 段 → 新 {len(new_b)} 段；"
              f"旧节 {len(old['sections'])} → 新节 {len(new['sections'])}；"
              f"**疑似丢失 {len(lost)} 段**")
        for sec, k, t, bt, r in lost[:8]:
            print(f"    ✗ [{sec}][{k}] 相似度 {r:.2f}")
            print(f"      旧: {t[:110]!r}")
            print(f"      新: {bt[:110]!r}")
    conn.close()


if __name__ == "__main__":
    main()
