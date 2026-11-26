#!/usr/bin/env python3
"""mcp_server.py —— 把 shadow-paper 的论文库接成 MCP，给别的 agent 用。

想法（2026-11-24）：shadow-paper 原本是**给人读**的。
既然论文都在本地了，那也该**让 agent 能问**——不然每次查都要人翻页面。

暴露四个工具：
    list_papers()              库有什么（篇号/标题/作者/词数/节数）
    search_papers(q, limit)    关键词检索 → 带出处的原文片段
    get_paper(id, section)     取一篇（或某节）的正文
    ask_library(question)      提问 → 基于库内原文的带引用答案

协议：stdio JSON-RPC（MCP 标准），和 ~/.semantix/mcp/server.mjs 同款写法。
    initialize / tools/list / tools/call，外加 notifications/* 忽略。

接法（Claude Code / MCode 等）：
    {
      "mcpServers": {
        "shadow-paper": {
          "command": "<shadow-paper 仓库的绝对路径>/.venv/bin/python",
          "args": ["<shadow-paper 仓库的绝对路径>/mcp_server.py"],
          "env": {"MINIMAX_API_KEY": "..."}
        }
      }
    }

⚠️ **只有 ask_library 会花钱**（调一次模型）。
   list/search/get 全是纯本地 BM25 + SQLite，零成本、确定性、离线可用。
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Dict

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

PROTOCOL_VERSION = "2024-11-05"


def _tools() -> list:
    return [
        {
            "name": "list_papers",
            "description": "列出论文库里的全部论文（篇号、标题、作者、词数、节数）。"
                           "先调它知道库里有什么，再决定要不要搜。",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "search_papers",
            "description": "在论文库里检索（BM25），返回带出处的原文片段。"
                           "用于「某篇论文里说了什么」「这个数字出自哪里」这类问题。"
                           "纯本地检索，不调模型、不花钱。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索词或问题"},
                    "limit": {"type": "integer", "description": "返回条数，默认 8", "default": 8},
                    "paper_id": {"type": "string", "description": "限定在某篇里检索（可选）"},
                },
                "required": ["query"],
            },
        },
        {
            "name": "get_paper",
            "description": "取一篇论文（或它某一节）的正文。paper_id 用 arXiv 编号（如 2412.06769）。"
                           "lang=zh 取中文版（已译的论文都有中文版）。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "paper_id": {"type": "string"},
                    "section": {"type": "string", "description": "章节名片段（可选）"},
                    "lang": {"type": "string", "enum": ["zh", "en"], "default": "zh",
                             "description": "zh=中文版，en=英文原文。默认中文。"},
                    "max_chars": {"type": "integer", "default": 12000},
                },
                "required": ["paper_id"],
            },
        },
        {
            "name": "ask_library",
            "description": "向论文库提问，得到基于库内原文的带引用答案。"
                           "每个结论标 [篇号/章节]。检索不到会说找不到，不编。"
                           "⚠️ 这一步会调一次大模型（要 MINIMAX_API_KEY），其余工具都不花钱。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "top_k": {"type": "integer", "default": 8},
                    "paper_id": {"type": "string", "description": "限定在某篇里问（可选）"},
                },
                "required": ["question"],
            },
        },
    ]


def _call(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    from extensions import library

    if name == "list_papers":
        st = library.library_stats()
        st["zh_papers"] = sum(1 for t in st.get("titles", []) if t.get("has_zh"))
        return st
    if name == "search_papers":
        hits = library.search(args.get("query") or "", limit=int(args.get("limit") or 8),
                              paper_id=args.get("paper_id") or None)
        return {"count": len(hits), "hits": hits}
    if name == "get_paper":
        return library.get_paper(args.get("paper_id") or "",
                                 section=args.get("section") or None,
                                 max_chars=int(args.get("max_chars") or 12000),
                                 lang=args.get("lang") or "zh")
    if name == "ask_library":
        return library.ask(args.get("question") or "", top_k=int(args.get("top_k") or 8),
                            paper_id=args.get("paper_id") or None)
    raise ValueError(f"没有这个工具：{name}")


def _text_result(payload: Any) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=1)}],
            "isError": False}


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        mid = msg.get("id")
        try:
            if method == "initialize":
                result = {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "shadow-paper", "version": "0.1.0"},
                }
            elif method == "tools/list":
                result = {"tools": _tools()}
            elif method == "tools/call":
                params = msg.get("params") or {}
                payload = _call(params.get("name") or "", params.get("arguments") or {})
                result = _text_result(payload)
            elif method in ("notifications/initialized", "notifications/cancelled"):
                continue
            elif method == "ping":
                result = {}
            else:
                if mid is None:
                    continue
                result = {"error": {"code": -32601, "message": f"未知方法：{method}"}}
        except Exception as e:  # noqa: BLE001
            result = {"error": {"code": -32603, "message": f"{e!r}"[:300],
                               "trace": traceback.format_exc()[-800:]}}
        if mid is None:
            continue
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result},
                                    ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
