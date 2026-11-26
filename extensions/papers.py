"""论文相关的 HTTP 路由：导入 / 列表 / 详情 / 删除 / 阅读进度 / 笔记 / 生词本。"""
from __future__ import annotations

import json
import time
from pathlib import Path

from flask import jsonify, request, send_from_directory

from . import db, schema, vocab
from .auth import _api_rate_limit_ok

MAX_PDF_BYTES = 120 * 1024 * 1024


def _client_ip():
    return request.remote_addr or "unknown"


# ── 导入 ──────────────────────────────────────────────────────
def register_routes(app):
    @app.route("/api/paper/import/arxiv", methods=["POST"])
    def import_arxiv_route():
        ok, retry = _api_rate_limit_ok(_client_ip(), "import")
        if not ok:
            return jsonify({"success": False, "error": f"导入太频繁，{retry} 秒后再试"}), 429
        payload = request.get_json(silent=True) or {}
        pid = (payload.get("id") or "").strip()
        if not pid:
            return jsonify({"success": False, "error": "缺少 arXiv 编号"}), 400
        from . import arxiv_import
        try:
            doc = arxiv_import.import_arxiv(
                pid,
                db.ASSETS_DIR,
                unwrap_cache=db.DATA_DIR / "unwrap_cache.json",
            )
        except arxiv_import.ImportError_ as e:
            return jsonify({"success": False, "error": str(e)}), 400
        except Exception as e:  # noqa: BLE001
            return jsonify({"success": False, "error": f"导入失败：{e}"}), 500
        errs = schema.validate(doc)
        if errs:
            return jsonify({"success": False, "error": "论文结构不合法：" + "；".join(errs[:3])}), 500
        db.save_paper(doc, "arxiv")
        return jsonify({"success": True, "id": doc["id"], "title": doc.get("title"),
                        "stats": doc.get("stats", {})})

    @app.route("/api/paper/import/pdf", methods=["POST"])
    def import_pdf_route():
        ok, retry = _api_rate_limit_ok(_client_ip(), "import")
        if not ok:
            return jsonify({"success": False, "error": f"导入太频繁，{retry} 秒后再试"}), 429
        f = request.files.get("file")
        if not f:
            return jsonify({"success": False, "error": "没有文件"}), 400
        raw = f.read()
        if len(raw) > MAX_PDF_BYTES:
            return jsonify({"success": False, "error": "文件太大"}), 413
        from . import pdf_import
        try:
            doc = pdf_import.import_pdf(raw, f.filename or "paper.pdf", db.ASSETS_DIR)
        except pdf_import.ImportError_ as e:
            return jsonify({"success": False, "error": str(e)}), 400
        except Exception as e:  # noqa: BLE001
            return jsonify({"success": False, "error": f"导入失败：{e}"}), 500
        errs = schema.validate(doc)
        if errs:
            return jsonify({"success": False, "error": "论文结构不合法：" + "；".join(errs[:3])}), 500
        db.save_paper(doc, "pdf")
        return jsonify({"success": True, "id": doc["id"], "title": doc.get("title"),
                        "stats": doc.get("stats", {})})

    # ── 读取 ───────────────────────────────────────────────────
    @app.route("/api/ask", methods=["POST"])
    def ask_route():
        """问这个库 —— 只依据库内论文原文回答，每个结论标出处。

        这是 shadow-paper 从「阅读器」长成「IMA 式论文库」的那一步：
        人可以在页面上读，agent 可以通过 MCP 问，**两边共用同一个检索+取证内核**
        （extensions/library.py），所以网页上看到的答案和 agent 拿到的答案同源。
        """
        from . import library

        payload = request.get_json(silent=True) or {}
        q = (payload.get("question") or "").strip()
        if not q:
            return jsonify({"success": False, "error": "问题不能为空"}), 400
        try:
            res = library.ask(
                q,
                top_k=int(payload.get("top_k") or 8),
                paper_id=(payload.get("paper_id") or None),
            )
        except Exception as e:  # noqa: BLE001
            return jsonify({"success": False, "error": f"{e!r}"[:200]}), 500
        out = {
            "success": True,
            "question": q,
            "answer": res.get("answer") or "",
            "citations": res.get("citations") or [],
            "passages": res.get("passages") or [],
            "used_llm": bool(res.get("used_llm")),
        }
        if res.get("error"):
            out["error"] = res["error"]
        return jsonify(out)

    @app.route("/api/library")
    def library_route():
        """库概览：几篇、多少词、多少检索片段。"""
        from . import library

        return jsonify({"success": True, **library.library_stats()})

    @app.route("/api/papers")
    def papers_list():
        papers = db.list_papers()
        for row in papers:
            row["has_zh"] = db.has_zh(row["id"])
        return jsonify({"success": True, "papers": papers})

    @app.route("/api/paper/<pid>")
    def paper_detail(pid):
        doc = db.load_paper(pid)
        if not doc:
            return jsonify({"success": False, "error": "没有这篇论文"}), 404
        # ?lang=zh 取中文版（没译过就自动回落英文）。
        # has_zh 一起返回，前端据此决定「中文/原文」开关给不给点。
        lang = (request.args.get("lang") or "").lower()
        zh_doc = db.load_zh_paper(pid)
        if lang in ("zh", "cn", "zh-cn") and zh_doc:
            return jsonify({"success": True, "paper": zh_doc, "lang": "zh",
                            "has_zh": True, "title_en": doc.get("title")})
        return jsonify({"success": True, "paper": doc, "lang": "en",
                        "has_zh": bool(zh_doc), "title_zh": (zh_doc or {}).get("title_zh")})

    @app.route("/api/paper/<pid>", methods=["DELETE"])
    def paper_delete(pid):
        db.delete_paper(pid)
        return jsonify({"success": True})

    @app.route("/api/paper/<pid>/progress", methods=["GET", "POST"])
    def paper_progress(pid):
        if request.method == "GET":
            row = db.get_db().execute(
                "SELECT section_id, block_idx, updated_ts FROM reading_progress WHERE paper_id = ?",
                (pid,)).fetchone()
            return jsonify({"success": True, "progress": dict(row) if row else None})
        payload = request.get_json(silent=True) or {}
        db.get_db().execute(
            "INSERT OR REPLACE INTO reading_progress (paper_id, section_id, block_idx, updated_ts) "
            "VALUES (?,?,?,?)",
            (pid, payload.get("section_id", ""), int(payload.get("block_idx", 0) or 0),
             int(time.time() * 1000)),
        )
        return jsonify({"success": True})

    @app.route("/api/paper/<pid>/notes", methods=["GET", "POST"])
    def paper_notes(pid):
        conn = db.get_db()
        if request.method == "GET":
            rows = conn.execute(
                "SELECT id, section_id, block_idx, text, created_at FROM paper_notes "
                "WHERE paper_id = ? ORDER BY created_at DESC", (pid,)).fetchall()
            return jsonify({"success": True, "notes": [dict(r) for r in rows]})
        payload = request.get_json(silent=True) or {}
        text = (payload.get("text") or "").strip()
        if not text:
            return jsonify({"success": False, "error": "空笔记"}), 400
        conn.execute(
            "INSERT INTO paper_notes (paper_id, section_id, block_idx, text, created_at) "
            "VALUES (?,?,?,?,?)",
            (pid, payload.get("section_id", ""), int(payload.get("block_idx", 0) or 0),
             text, int(time.time() * 1000)),
        )
        return jsonify({"success": True})

    # ── 生词本 / 复习 ──────────────────────────────────────────
    @app.route("/api/vocab/lookup", methods=["POST"])
    def vocab_lookup():
        payload = request.get_json(silent=True) or {}
        word = (payload.get("word") or "").strip()
        if not word:
            return jsonify({"success": False, "error": "缺 word"}), 400
        review = vocab.record_lookup(word, payload.get("meaning", ""))
        return jsonify({"success": True, "review": review})

    @app.route("/api/vocab/review", methods=["POST"])
    def vocab_review():
        payload = request.get_json(silent=True) or {}
        review = vocab.record_review((payload.get("word") or "").strip(),
                                     bool(payload.get("correct")))
        if not review:
            return jsonify({"success": False, "error": "这个词不在生词本里"}), 404
        return jsonify({"success": True, "review": review})

    @app.route("/api/vocab/review-queue")
    def vocab_queue():
        limit = request.args.get("limit", 20)
        return jsonify({"success": True, "queue": vocab.due_queue(limit)})

    @app.route("/api/vocab/stats")
    def vocab_stats():
        return jsonify({"success": True, **vocab.stats()})

    @app.route("/api/vocab/words")
    def vocab_words():
        return jsonify({"success": True, "words": vocab.list_words()})

    @app.route("/api/vocab/remove", methods=["POST"])
    def vocab_remove():
        payload = request.get_json(silent=True) or {}
        vocab.remove((payload.get("word") or "").strip())
        return jsonify({"success": True})
