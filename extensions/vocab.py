"""生词本 + 艾宾浩斯复习（SRS）。

⚠️ shadow-learning 里有**两套互不相通**的 SRS：
    · 服务端 `vocab.py`：状态机 learning→practicing→familiar→mastered，间隔 0/2/5/14 天，
      存 SQLite `vocab_reviews`，有到期索引
    · 前端 `index.js`：localStorage 里自己一套，间隔 1/3/7/14/30 天
  两套间隔不同、存储不同、而且**服务端的查词入口在前端根本没接上**（`/api/vocab/lookup` 无人调用）。

论文版只保留服务端那套，并把 `/api/vocab/lookup` **真正接进查词点击路径** ——
读论文遇到术语点一下就该进生词本，这正是这个 App 对论文最有用的地方。
"""
from __future__ import annotations

import time

from .db import get_db

SRS_INTERVALS_DAYS = {"learning": 0, "practicing": 2, "familiar": 5, "mastered": 14}
SRS_STATES = ("learning", "practicing", "familiar", "mastered")


def _srs_next_state(current: str, correct: bool) -> str:
    """答对升一档，答错降一档（learning 保底，mastered 封顶）。"""
    try:
        idx = SRS_STATES.index(current)
    except ValueError:
        idx = 0
    if correct:
        return SRS_STATES[min(idx + 1, len(SRS_STATES) - 1)]
    return SRS_STATES[max(idx - 1, 0)]


def _srs_interval_ms(state: str) -> int:
    return SRS_INTERVALS_DAYS.get(state, 0) * 86400000


def record_lookup(word: str, meaning: str = "") -> dict:
    """查词命中 → 记进生词本（已存在则不动状态）。"""
    w = (word or "").strip().lower()
    if not w:
        return {}
    now = int(time.time() * 1000)
    conn = get_db()
    row = conn.execute("SELECT word FROM vocab_reviews WHERE word = ?", (w,)).fetchone()
    if row:
        return get_review(w) or {}
    conn.execute(
        "INSERT OR IGNORE INTO vocab_reviews "
        "(word, state, added_ts, next_review_ts, review_count, correct_count) VALUES (?,?,?,?,0,0)",
        (w, "learning", now, now + _srs_interval_ms("learning")),
    )
    return get_review(w) or {}


def record_review(word: str, correct: bool) -> dict:
    """答一次复习 → 推进状态。词不存在返回 None。"""
    w = (word or "").strip().lower()
    conn = get_db()
    row = conn.execute("SELECT state FROM vocab_reviews WHERE word = ?", (w,)).fetchone()
    if not row:
        return {}
    new_state = _srs_next_state(row["state"], correct)
    now = int(time.time() * 1000)
    conn.execute(
        "UPDATE vocab_reviews SET state=?, next_review_ts=?, last_review_ts=?, "
        "review_count=review_count+1, correct_count=correct_count+? WHERE word=?",
        (new_state, now + _srs_interval_ms(new_state), now, 1 if correct else 0, w),
    )
    return get_review(w) or {}


def get_review(word: str):
    w = (word or "").strip().lower()
    row = get_db().execute(
        "SELECT word, state, added_ts, next_review_ts, last_review_ts, review_count, correct_count "
        "FROM vocab_reviews WHERE word = ?", (w,)).fetchone()
    return dict(row) if row else None


def due_queue(limit: int = 20) -> list:
    limit = max(1, min(50, int(limit)))
    now = int(time.time() * 1000)
    rows = get_db().execute(
        "SELECT word, state, next_review_ts FROM vocab_reviews "
        "WHERE next_review_ts <= ? ORDER BY next_review_ts ASC LIMIT ?", (now, limit)).fetchall()
    return [dict(r) for r in rows]


def stats() -> dict:
    conn = get_db()
    now = int(time.time() * 1000)
    out = {"learning": 0, "practicing": 0, "familiar": 0, "mastered": 0, "total": 0}
    for r in conn.execute("SELECT state, COUNT(*) n FROM vocab_reviews GROUP BY state"):
        if r["state"] in out:
            out[r["state"]] = r["n"]
        out["total"] += r["n"]
    row = conn.execute("SELECT COUNT(*) n FROM vocab_reviews WHERE next_review_ts <= ?", (now,)).fetchone()
    out["due_now"] = row["n"] if row else 0
    return out


def list_words(limit: int = 200) -> list:
    rows = get_db().execute(
        "SELECT word, state, added_ts, next_review_ts, review_count, correct_count "
        "FROM vocab_reviews ORDER BY added_ts DESC LIMIT ?", (max(1, min(500, int(limit))),)).fetchall()
    return [dict(r) for r in rows]


def remove(word: str) -> bool:
    w = (word or "").strip().lower()
    get_db().execute("DELETE FROM vocab_reviews WHERE word = ?", (w,))
    return True
