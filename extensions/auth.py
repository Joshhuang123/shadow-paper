"""最小限流（从 shadow-learning 搬来同一套语义：per-IP 滑动窗口 + 分桶）。

论文版没有「家长 PIN」那层鉴权（这是个单机自用工具），但**限流要留着**：
查词会打公网（道 / datamuse），不压住会被上游拉黑。
"""
from __future__ import annotations

import threading
import time

_API_RATE = {}
_API_LOCK = threading.Lock()

_API_LIMITS = {
    "dict": {"max": 120, "window": 60},
    "import": {"max": 20, "window": 3600},
    "global": {"max": 600, "window": 60},
}
_SWEEP_THRESHOLD = 512


def _sweep_stale_api_rate(now: float) -> int:
    if len(_API_RATE) < _SWEEP_THRESHOLD:
        return 0
    trims = {}
    stale_keys = []
    for key, timestamps in _API_RATE.items():
        window = _API_LIMITS.get(key[1], _API_LIMITS["global"])["window"]
        fresh = [t for t in timestamps if now - t < window]
        if not fresh:
            stale_keys.append(key)
        elif len(fresh) != len(timestamps):
            trims[key] = fresh
    for key, fresh in trims.items():
        _API_RATE[key] = fresh
    for key in stale_keys:
        del _API_RATE[key]
    return len(stale_keys)


def _api_rate_limit_ok(ip, bucket="global"):
    """返回 (ok, retry_after_sec)。"""
    with _API_LOCK:
        cfg = _API_LIMITS.get(bucket, _API_LIMITS["global"])
        now = time.time()
        key = (ip, bucket)
        arr = [t for t in _API_RATE.get(key, []) if now - t < cfg["window"]]
        if len(arr) >= cfg["max"]:
            retry = int(cfg["window"] - (now - arr[0]))
            return False, max(retry, 1)
        arr.append(now)
        _API_RATE[key] = arr
        _sweep_stale_api_rate(now)
        return True, 0
