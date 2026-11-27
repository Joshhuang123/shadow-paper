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
            "2603.09184", "2604.12493", "2608.13570", "2611.32606"]


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
    force = "--force" in sys.argv
    pids = [a for a in sys.argv[1:] if not a.startswith("--")] or ALL_PIDS
    snap = load_snapshot()
    for pid in pids:
        old_en, old_zh = snap.get(pid, (None, None))
        # 指纹比对：英文原文没变 → 中文版已是最新，别重翻（纯浪费）
        cur_en = db.load_paper(pid)
        cur_zh = db.load_zh_paper(pid)
        # 指纹只证明"英文没变"，**不证明译文写进去了** ——
        # 之前就是靠这个跳过了一篇"整篇还是英文"的中文版（静默失败）。
        # 所以再加一条：**中文占比**必须达标，才算"已是最新"。
        body_now = "".join((b.get("t") or "") for sec in (cur_zh or {}).get("sections", [])
                            for b in sec.get("blocks", []))
        cjk_now = sum(1 for c in body_now if "一" <= c <= "鿿")
        letters_now = sum(1 for c in body_now if c.isalpha())
        ratio_now = cjk_now / max(1, cjk_now + letters_now)
        if (not force and cur_en and cur_zh
                and cur_zh.get("en_fingerprint") == zh.en_fingerprint(cur_en)
                and ratio_now >= 0.30):
            print(f"=== {pid}: 英文原文未变，中文版已是最新（中文 {ratio_now:.0%}）→ 跳过", flush=True)
            continue
        if cur_zh:
            print(f"=== {pid}: 指纹相同但中文占比只有 {ratio_now:.0%} → 强制重建", flush=True)
        print(f"=== {pid}", flush=True)
        try:
            if not old_en:
                # 库里新加的论文：没有旧译文可复用 → 整篇翻
                print("  （新论文，无旧译文可复用 → 整篇翻译）", flush=True)
                out = zh.translate_paper(pid, force=True)
            else:
                out = zh.rebuild_paper(pid, old_en, old_zh or {"sections": []})
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ 重建失败：{e!r}"[:200])
            continue
        # 体检：中文版的正文**必须大部分是中文**。
        # 静默失败（译文没写进 results）时，段数、缺译数、警告数全都正常，
        # 只有这个比例能发现 —— 实测踩过：92 个标题全英文、整篇还是英文，
        # 而报告里写的是「缺译 0」。
        body = "".join((b.get("t") or "") for sec in out.get("sections", [])
                       for b in sec.get("blocks", []))
        cjk = sum(1 for c in body if "一" <= c <= "鿿")
        letters = sum(1 for c in body if c.isalpha())
        ratio = cjk / max(1, cjk + letters)
        flag = "  ← 异常！译文没写进去" if ratio < 0.25 else ""
        print(f"    · 正文中文字符占比 {ratio:.1%}{flag}")
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
