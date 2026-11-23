"""整句翻译（英 → 中）。

用 MyMemory（免费、无需 key）。⚠ **路径里不能有 /api/v1/** ——
带上前缀会 404（前端原来写的就是带前缀的版本，所以「翻译整句」一直没反应）。
正确路径：`https://api.mymemory.translated.net/get?q=…&langpair=en|zh-CN`

为什么放服务端而不是浏览器直连：
    ① 浏览器的直连要过 CORS，MyMemory 不一定给论文站放行；
    ② 服务端能缓存 —— 论文里同一句话经常重复出现；
    ③ 出错时能给前端一个明确的中文提示，而不是"没反应"。

上游全挂时**如实报错**（前端会显示"翻译暂时不可用"），
不拿别的东西冒充译文。
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

from flask import jsonify, request

from .auth import _api_rate_limit_ok
from .db import DATA_DIR

CACHE_DIR = DATA_DIR / "translate_cache"
CACHE_TTL = 30 * 86400  # 30 天
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/157.0 Safari/537.36"

ENDPOINTS = (
    "https://api.mymemory.translated.net/get?q={q}&langpair={pair}",
)


class TranslateError(Exception):
    pass


def _cache_path(text: str, pair: str) -> Path:
    key = hashlib.sha1(f"{pair}\x00{text}".encode("utf-8")).hexdigest()[:20]
    return CACHE_DIR / f"{key}.json"


def translate(text: str, to: str = "zh-CN", frm: str = "en") -> dict:
    text = (text or "").strip()
    if not text:
        raise TranslateError("要翻译的句子是空的")
    if len(text) > 900:
        text = text[:900]
    pair = f"{frm}|{to}"

    cp = _cache_path(text, pair)
    if cp.exists():
        try:
            data = json.loads(cp.read_text(encoding="utf-8"))
            if time.time() - data.get("ts", 0) < CACHE_TTL:
                return {"text": data["translated"], "cached": True, "source": data.get("source")}
        except Exception:
            pass

    url = ENDPOINTS[0].format(q=urllib.parse.quote(text), pair=urllib.parse.quote(pair))
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    last = ""
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            raw = r.read().decode("utf-8", "replace")
        data = json.loads(raw)
        status = data.get("responseStatus")
        if status not in (200, "200", None):
            raise TranslateError(str(data.get("responseDetails") or f"上游状态 {status}"))
        tr = (data.get("responseData") or {}).get("translatedText")
        if not tr:
            raise TranslateError("上游没给译文")
    except TranslateError:
        raise
    except Exception as e:  # noqa: BLE001
        last = f"{type(e).__name__}: {e}"
        raise TranslateError(f"翻译服务连不上（{last[:90]}）") from e

    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cp.write_text(json.dumps({"text": tr, "ts": time.time(), "source": "mymemory"},
                                 ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return {"text": tr, "cached": False, "source": "mymemory"}


def register_routes(app):
    @app.route("/api/translate")
    def api_translate():
        ok, retry = _api_rate_limit_ok(request.remote_addr or "unknown", "global")
        if not ok:
            return jsonify({"success": False, "error": f"太频繁，{retry} 秒后再试",
                            "retryable": True, "retry_after": retry}), 429
        q = request.args.get("q", "")
        to = request.args.get("to", "zh-CN")
        frm = request.args.get("from", "en")
        try:
            r = translate(q, to, frm)
        except TranslateError as e:
            return jsonify({"success": False, "error": str(e), "retryable": True}), 502
        return jsonify({"success": True, "translated": r["text"],
                        "cached": r["cached"], "source": r["source"]})
