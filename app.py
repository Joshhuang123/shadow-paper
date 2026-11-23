#!/usr/bin/env python3
"""Shadow Paper —— 论文阅读器（新版本）。

启动：
    python app.py            # http://localhost:5003
    python app.py --port 5004
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from flask import Flask, jsonify, send_from_directory

from extensions import db

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"


def create_app() -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["JSON_AS_ASCII"] = False
    app.config["MAX_CONTENT_LENGTH"] = 128 * 1024 * 1024
    secret = db.DATA_DIR / ".secret"
    db.DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not secret.exists():
        secret.write_text(os.urandom(24).hex())
        try:
            secret.chmod(0o600)
        except OSError:
            pass
    app.secret_key = os.environ.get("SHADOW_SECRET") or secret.read_text().strip()

    db.init_db()

    from extensions import dictionary, papers, translate, tts
    dictionary.register_routes(app)
    papers.register_routes(app)
    translate.register_routes(app)
    tts.register_routes(app)

    # ── 页面 ────────────────────────────────────────────────
    @app.route("/")
    def index():
        return send_from_directory(WEB_DIR, "index.html")

    @app.route("/read/<pid>")
    def reader(pid):
        return send_from_directory(WEB_DIR, "reader.html")

    @app.route("/static/<path:filename>")
    def static_files(filename):
        return send_from_directory(WEB_DIR, filename)

    @app.route("/assets/<path:filename>")
    def assets(filename):
        return send_from_directory(db.ASSETS_DIR, filename)

    @app.route("/api/health")
    def health():
        return jsonify({"success": True, "app": "shadow-paper",
                        "papers": len(db.list_papers())})

    return app


app = create_app()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5003)
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()
