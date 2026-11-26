"""重建中文版：英文原文变了哪段就重译哪段，没变的复用旧译文。

配套 tools/reimport_all.py 使用：清洗器修 bug → 英文原文变了 → 中文版必须跟着重建。

用法：
    .venv/bin/python tools/rebuild_zh.py              # 全部 6 篇
    .venv/bin/python tools/rebuild_zh.py 2412.06769   # 指定
"""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from extensions import db, zh  # noqa: E402

SNAP_DIR = Path.home() / "WorkBuddy" / "shadow-paper-snapshots"
ALL_PIDS = ["2412.06769", "2506.09985", "2512.21711",
            "2603.09184", "2604.12493", "2608.13570"]


def load_snapshot():
    snaps = sorted(SNAP_DIR.glob("*.db"))
    if not snaps:
        raise SystemExit("没有快照，先备份再重导")
    conn = sqlite3.connect(str(snaps[-1]))
    conn.row_factory = sqlite3.Row
    out = {}
    for r in conn.execute("SELECT id, data_json, zh_json FROM papers"):
        out[r["id"]] = (json.loads(r["data_json"]),
                        json.loads(r["zh_json"]) if r["zh_json"] else None)
    conn.close()
    return out


def main():
    pids = sys.argv[1:] or ALL_PIDS
    snap = load_snapshot()
    for pid in pids:
        old_en, old_zh = snap.get(pid, (None, None))
        if not old_en:
            print(f"=== {pid}: 快照里没有，跳过")
            continue
        print(f"=== {pid}", flush=True)
        try:
            out = zh.rebuild_paper(pid, old_en, old_zh or {"sections": []})
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ 重建失败：{e!r}"[:200])
            continue
        db.save_zh_paper(pid, out)
        zh.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (zh.CACHE_DIR / f"{pid}.json").write_text(
            json.dumps(out, ensure_ascii=False), encoding="utf-8")
        # 体检
        toc_en = sum(1 for t in out.get("toc", []) if _is_en(t.get("title", "")))
        sec_en = sum(1 for s in out.get("sections", []) if _is_en(s.get("title", "")))
        print(f"  ✓ 段落 {out.get('translation_blocks')}｜缺译 {out.get('translation_missing')}"
              f"｜警告 {len(out.get('translation_warnings') or [])}"
              f"｜目录英文 {toc_en}/{len(out.get('toc', []))}｜小节英文 {sec_en}"
              f"｜标题中文 {out.get('title_zh', '')[:24]}")


def _is_en(s: str) -> bool:
    """粗判是不是没译的英文（没有中文字符）。"""
    return bool(s.strip()) and not any("一" <= c <= "鿿" for c in s)


if __name__ == "__main__":
    main()
