"""看模型原始返回长什么样（思维链是否混在 content 里）。

用法：.venv/bin/python tools/probe_llm_raw.py
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from extensions import zh  # noqa: E402

key = os.environ.get("MINIMAX_API_KEY")
body = {
    "model": zh.MODEL,
    "messages": [
        {"role": "system", "content": zh.SYSTEM},
        {"role": "user", "content": "Most word tokens primarily ensure textual coherence and are not essential for reasoning."},
    ],
    "max_tokens": 1500,
    "temperature": 0.2,
}
req = urllib.request.Request(
    f"{zh.API}/chat/completions",
    data=json.dumps(body).encode("utf-8"),
    headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
)
with urllib.request.urlopen(req, timeout=120) as r:
    data = json.loads(r.read().decode("utf-8"))
ch = (data.get("choices") or [{}])[0]
msg = ch.get("message") or {}
print("finish_reason:", ch.get("finish_reason"))
print("有 reasoning_content:", bool(msg.get("reasoning_content")))
print("usage:", json.dumps(data.get("usage"), ensure_ascii=False))
content = msg.get("content") or ""
print("content 长度:", len(content))
print("content 里 mm:think 次数:", content.count("mm:think"))
print("--- content 前 400 字符（repr，看隐藏字符）---")
print(repr(content[:400]))
print("--- content 后 200 字符 ---")
print(repr(content[-200:]))
