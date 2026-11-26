"""重导全部论文（LaTeX 路径），并与快照对比，报告**内容变化**。

为什么需要（2026-11-26）：
  清洗器修了三个会**静默丢内容**的 bug：
    ① 裸自定义宏（\\ours）不展开 → 论文里的方法名整篇消失；
    ② 本地 .sty/.cls 不中和 → pandoc 啃坏题名块，正文多出「maketitle thanks aketitle」；
    ③ 主文件识别靠「最大的 .tex」→ 多文件论文会挑中附录。
  修完必须重导，否则库里还是旧的残缺版本。

用法：
    .venv/bin/python tools/reimport_all.py            # 全部
    .venv/bin/python tools/reimport_all.py 2412.06769 # 指定
"""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from extensions import arxiv_import, db, schema  # noqa: E402

SNAP_DIR = Path.home() / "WorkBuddy" / "shadow-paper-snapshots"
ALL_PIDS = ["2412.06769", "2506.09985", "2512.21711",
            "2603.09184", "2604.12493", "2608.13570"]


def latest_snapshot():
    snaps = sorted(SNAP_DIR.glob("*.db"))
    if not snaps:
        raise SystemExit("没有快照，先跑一次备份")
    return snaps[-1]


def load_snapshot():
    conn = sqlite3.connect(str(latest_snapshot()))
    conn.row_factory = sqlite3.Row
    out = {}
    for r in conn.execute("SELECT id, data_json, zh_json FROM papers"):
        out[r["id"]] = (json.loads(r["data_json"]),
                        json.loads(r["zh_json"]) if r["zh_json"] else None)
    conn.close()
    return out


def diff_sections(old, new):
    """返回 (标题变化, 块变化, 节数变化)。"""
    titles, blocks = [], 0
    os_, ns_ = old.get("sections") or [], new.get("sections") or []
    for a, b in zip(os_, ns_):
        if (a.get("title") or "") != (b.get("title") or ""):
            titles.append((a.get("id"), a.get("title"), b.get("title")))
        ba, bb = a.get("blocks") or [], b.get("blocks") or []
        for x, y in zip(ba, bb):
            if (x.get("t") or "") != (y.get("t") or ""):
                blocks += 1
    return titles, blocks, len(os_), len(ns_)


def main():
    pids = sys.argv[1:] or ALL_PIDS
    snap = load_snapshot()
    for pid in pids:
        print(f"\n=== {pid} ===", flush=True)
        try:
            doc = arxiv_import.import_arxiv(pid, db.ASSETS_DIR,
                                            unwrap_cache=db.DATA_DIR / "unwrap_cache.json")
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ 导入失败：{e}")
            continue
        errs = schema.validate(doc)
        if errs:
            print(f"  ✗ 结构不合法：{errs[:3]}")
            continue
        db.save_paper(doc, "arxiv")
        old_en, old_zh = snap.get(pid, (None, None))
        if old_en:
            titles, blocks, n_old, n_new = diff_sections(old_en, doc)
            print(f"  节数 {n_old} → {n_new}；标题变化 {len(titles)}；块变化 {blocks}")
            for sid, a, b in titles[:8]:
                print(f"    [{sid}] {a!r} → {b!r}")
            txt_old = json.dumps(old_en, ensure_ascii=False)
            txt_new = json.dumps(doc, ensure_ascii=False)
            print(f"    正文字数 {len(txt_old)} → {len(txt_new)}")
        # 中文版还在库里但对应的是旧英文 → 标脏
        if old_zh:
            print("    ⚠ 中文版对应旧英文，需要重建")
        print(f"  ✓ 已写入：{doc.get('title')}")


if __name__ == "__main__":
    main()
