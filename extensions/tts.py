"""TTS：把词/句读出来。

为什么用 **macOS 自带的 `say`** 而不是 edge-tts：
    edge-tts 要连微软的 websocket 端点，实测这台机器 **403**（WSServerHandshakeError）。
    `say` 是系统自带的、**完全离线**、秒回，而且 macOS 上音色够用（Samantha / Alex / …）。
    → 引擎选型先按「能不能用」排，再按「好不好听」排。

产出格式：
    `say` 只会出 AIFF —— Safari 能播，**Chrome 播不了**，
    所以再转一道 m4a（afconvert，macOS 自带），浏览器通吃，还小一个数量级。

缓存：
    按 (文本 + 音色) 的 sha1 命名落盘，同一个词只合成一次。
    词条 TTS 的调用量很大（点一次查词就读一次），不缓存会把 `say` 跑成瓶颈。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from flask import jsonify, request, send_from_directory

from .auth import _api_rate_limit_ok
from .db import DATA_DIR

TTS_DIR = DATA_DIR / "tts"

# 音色：英文用 Samantha（清晰、不机器），中文用 Tingting
DEFAULT_VOICE = {
    "en": "Samantha",
    "zh": "Tingting",
}

MAX_CHARS = 600


def _hash(text: str, voice: str) -> str:
    return hashlib.sha1(f"{voice}\x00{text}".encode("utf-8")).hexdigest()[:20]


def _pick_voice(text: str, requested: str = "") -> str:
    if requested:
        return requested
    has_cjk = any("一" <= c <= "鿿" for c in text)
    return DEFAULT_VOICE["zh" if has_cjk else "en"]


def _synthesize(text: str, voice: str) -> bytes:
    """调 macOS say 出音频，返回 m4a 字节。"""
    say = shutil.which("say")
    if not say:
        raise RuntimeError("这台机器没有 say（macOS 自带语音），无法合成")
    with tempfile.TemporaryDirectory() as td:
        aiff = Path(td) / "a.aiff"
        r = subprocess.run([say, "-v", voice, "-o", str(aiff), text],
                           capture_output=True, timeout=45)
        if r.returncode != 0 or not aiff.exists() or aiff.stat().st_size < 128:
            raise RuntimeError((r.stderr or b"say failed").decode("utf-8", "replace")[:120])
        afconvert = shutil.which("afconvert")
        if afconvert:
            m4a = Path(td) / "a.m4a"
            r2 = subprocess.run([afconvert, "-f", "m4af", "-d", "aac", str(aiff), str(m4a)],
                                capture_output=True, timeout=45)
            if r2.returncode == 0 and m4a.exists() and m4a.stat().st_size > 128:
                return m4a.read_bytes()
        return aiff.read_bytes()


def synthesize(text: str, voice: str = "") -> dict:
    """合成并返回 {path, ext, cached}。"""
    text = (text or "").strip()
    if not text:
        raise ValueError("文本为空")
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS]
    voice = _pick_voice(text, voice)
    key = _hash(text, voice)
    TTS_DIR.mkdir(parents=True, exist_ok=True)
    m4a = TTS_DIR / f"{key}.m4a"
    aiff = TTS_DIR / f"{key}.aiff"
    if m4a.exists():
        return {"path": m4a, "ext": "m4a", "cached": True}
    if aiff.exists():
        return {"path": aiff, "ext": "aiff", "cached": True}
    data = _synthesize(text, voice)
    # m4a 的特征：4 字节长度 + "ftyp"；AIFF 的特征：开头就是 "FORM"
    ext = "m4a" if data[4:8] == b"ftyp" else "aiff"
    if ext == "aiff":
        aiff.write_bytes(data)
        return {"path": aiff, "ext": "aiff", "cached": False}
    tmp = m4a.with_name(f".{m4a.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, m4a)
    return {"path": m4a, "ext": "m4a", "cached": False}


def register_routes(app):
    @app.route("/api/tts", methods=["POST"])
    def tts():
        ok, retry = _api_rate_limit_ok(request.remote_addr or "unknown", "global")
        if not ok:
            return jsonify({"success": False, "error": f"太频繁，{retry} 秒后再试",
                            "retryable": True, "retry_after": retry}), 429
        payload = request.get_json(silent=True) or {}
        text = (payload.get("text") or "").strip()
        if not text:
            return jsonify({"success": False, "error": "文本为空"}), 400
        try:
            info = synthesize(text, payload.get("voice") or "")
        except Exception as e:  # noqa: BLE001
            return jsonify({"success": False, "error": f"合成失败：{e}",
                            "retryable": True}), 502
        return jsonify({"success": True,
                        "audio_url": f"/audio/tts/{info['path'].name}",
                        "cached": info["cached"]})

    @app.route("/audio/tts/<path:filename>")
    def tts_audio(filename):
        # 防路径穿越：只允许我们自己生成的文件名
        if "/" in filename or ".." in filename or not filename.endswith((".m4a", ".aiff")):
            return jsonify({"success": False, "error": "bad filename"}), 400
        return send_from_directory(str(TTS_DIR), filename, max_age=86400)
