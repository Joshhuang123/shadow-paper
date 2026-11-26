"""SQLite 层（论文版精简 schema）。

跟 shadow-learning 的区别：
    原项目 13 张表，其中大部分服务于「儿童英语学习」（语法、音素、知识图谱、跟读熟练度…）。
    论文版只留 6 张：论文本体、查词缓存、生词 SRS、阅读进度、笔记、导入暂存。

刻意**不复用**原项目的 db.py：
    迁移 marker 机制、parent_data 拆分历史这些都是英语学习版走出来的包袱，
    论文版不需要，抄过来反而要解释一堆用不上的表。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA_DIR / "shadow-paper.db"
ASSETS_DIR = DATA_DIR / "assets"
PAPERS_DIR = DATA_DIR / "papers"

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    id           TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    authors_json TEXT NOT NULL DEFAULT '[]',
    source_type  TEXT NOT NULL DEFAULT 'arxiv',
    n_sections   INTEGER NOT NULL DEFAULT 0,
    n_blocks     INTEGER NOT NULL DEFAULT 0,
    n_words      INTEGER NOT NULL DEFAULT 0,
    data_json    TEXT NOT NULL,
    zh_json      TEXT,                 -- 中文版（同一篇的译文，不是另一篇）
    imported_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_papers_updated ON papers(updated_at);

CREATE TABLE IF NOT EXISTS dict_cache (
    word       TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    found      INTEGER NOT NULL DEFAULT 1,
    fetched_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS vocab_reviews (
    word            TEXT PRIMARY KEY,
    state           TEXT NOT NULL DEFAULT 'learning',
    added_ts        INTEGER NOT NULL,
    next_review_ts  INTEGER NOT NULL,
    last_review_ts  INTEGER,
    review_count    INTEGER NOT NULL DEFAULT 0,
    correct_count   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_vocab_due ON vocab_reviews(next_review_ts);

CREATE TABLE IF NOT EXISTS reading_progress (
    paper_id   TEXT PRIMARY KEY,
    section_id TEXT,
    block_idx  INTEGER NOT NULL DEFAULT 0,
    updated_ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id   TEXT NOT NULL,
    section_id TEXT NOT NULL DEFAULT '',
    block_idx  INTEGER NOT NULL DEFAULT 0,
    text       TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notes_paper ON paper_notes(paper_id);
"""

_local = threading.local()


def get_db():
    """thread-local 连接（sqlite3.Row 行工厂，dict_cache 的代码要按列名取值）。"""
    conn = getattr(_local, "conn", None)
    if conn is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH), timeout=20, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


def _ensure_zh_column():
    """老库加 zh_json 列（ALTER TABLE 不能写进 CREATE TABLE IF NOT EXISTS）。"""
    cols = {r[1] for r in get_db().execute("PRAGMA table_info(papers)")}
    if "zh_json" not in cols:
        get_db().execute("ALTER TABLE papers ADD COLUMN zh_json TEXT")


def save_zh_paper(pid: str, doc: dict) -> bool:
    """存中文版。存进同一篇的 zh_json —— 书架不出现重复条目，原文永远在。"""
    import time
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    get_db().execute(
        "UPDATE papers SET zh_json = ?, updated_at = ? WHERE id = ?",
        (json.dumps(doc, ensure_ascii=False), now, pid),
    )
    return True


def load_zh_paper(pid: str):
    """取中文版；没译过就返回 None（调用方回落到英文）。"""
    row = get_db().execute("SELECT zh_json FROM papers WHERE id = ?", (pid,)).fetchone()
    if not row or not row["zh_json"]:
        return None
    try:
        return json.loads(row["zh_json"])
    except json.JSONDecodeError:
        return None


def has_zh(pid: str) -> bool:
    return load_zh_paper(pid) is not None


def init_db():
    conn = get_db()
    conn.executescript(SCHEMA)
    _ensure_zh_column()
    return conn


def write_txn():
    """事务上下文（跟原项目同名同义）。"""
    class _Txn:
        def __enter__(self):
            get_db().execute("BEGIN IMMEDIATE")
            return get_db()

        def __exit__(self, exc_type, exc, tb):
            if exc_type is None:
                get_db().execute("COMMIT")
            else:
                get_db().execute("ROLLBACK")
            return False

    return _Txn()


def save_paper(doc: dict, source_type: str = "arxiv") -> str:
    """写入/覆盖一篇论文，返回 paper id。"""
    import time
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    conn = get_db()
    n_sections = len(doc.get("sections", []))
    n_blocks = sum(len(s.get("blocks", [])) for s in doc.get("sections", []))
    n_words = int(doc.get("stats", {}).get("words", 0))
    conn.execute(
        "INSERT OR REPLACE INTO papers "
        "(id, title, authors_json, source_type, n_sections, n_blocks, n_words,"
        " data_json, imported_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (doc["id"], doc.get("title", ""), json.dumps(doc.get("authors", []), ensure_ascii=False),
         source_type, n_sections, n_blocks, n_words,
         json.dumps(doc, ensure_ascii=False), now, now),
    )
    return doc["id"]


def load_paper(pid: str):
    row = get_db().execute("SELECT data_json FROM papers WHERE id = ?", (pid,)).fetchone()
    if not row:
        return None
    return json.loads(row["data_json"])


def list_papers() -> list:
    rows = get_db().execute(
        "SELECT id, title, authors_json, source_type, n_sections, n_blocks, n_words, imported_at "
        "FROM papers ORDER BY updated_at DESC"
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["authors"] = json.loads(d.pop("authors_json") or "[]")
        out.append(d)
    return out


def delete_paper(pid: str) -> bool:
    conn = get_db()
    conn.execute("DELETE FROM papers WHERE id = ?", (pid,))
    conn.execute("DELETE FROM reading_progress WHERE paper_id = ?", (pid,))
    conn.execute("DELETE FROM paper_notes WHERE paper_id = ?", (pid,))
    return True
