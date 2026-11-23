#!/usr/bin/env bash
# 起 Shadow Paper（论文阅读器）
set -e
cd "$(dirname "$0")"
PORT="${1:-5003}"
echo "Shadow Paper → http://127.0.0.1:${PORT}"
exec python3 app.py --port "${PORT}"
