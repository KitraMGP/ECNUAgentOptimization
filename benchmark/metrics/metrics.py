"""Metrics —— 指标统计（p50 / p95 / mean / std / cache_hit_rate / summarize）。

- ``summarize(rows)`` 保持旧 agent_bench.py 的输出字段（prompt_tokens /
  completion_tokens / total_tokens / rounds / avg_latency_ms / max_latency_ms /
  peak_rss_mb / peak_gpu_mb），并追加 E0 新指标（p50/p95/std latency、
  cached_tokens、cache_hit_rate、recompute_tokens）。
- 纯函数、无外部依赖（不用 numpy），可直接单测。
"""
from __future__ import annotations

import math
import statistics
from typing import Any, Dict, List, Optional

# ---- 基础统计 ----

def mean(xs: List[float]) -> Optional[float]:
    if not xs:
        return None
    return float(statistics.fmean(xs))


def std(xs: List[float]) -> Optional[float]:
    """样本标准差（ddof=1）；单元素返回 0.0 而非 None（避免下游比较失败）。"""
    if not xs:
        return None
    if len(xs) == 1:
        return 0.0
    return float(statistics.stdev(xs))


def percentile(xs: List[float], p: float) -> Optional[float]:
    """nearest-rank 分位数（p ∈ [0, 100]）；空输入返回 None。"""
    if not xs:
        return None
    s = sorted(xs)
    k = max(0, min(len(s) - 1, math.ceil(p / 100.0 * len(s)) - 1))
    return float(s[k])


def p50(xs: List[float]) -> Optional[float]:
    return percentile(xs, 50)


def p95(xs: List[float]) -> Optional[float]:
    return percentile(xs, 95)


def latency_stats(rows: List[dict]) -> Dict[str, Optional[float]]:
    """延迟统计：保留旧键（avg/max）+ 新键（p50/p95/std）。"""
    lats = [r["latency_ms"] for r in rows if r.get("latency_ms") is not None]
    if not lats:
        return {"avg_latency_ms": None, "max_latency_ms": None,
                "p50_latency_ms": None, "p95_latency_ms": None,
                "std_latency_ms": None}
    return {
        "avg_latency_ms": round(mean(lats), 1),
        "max_latency_ms": round(max(lats), 1),
        "p50_latency_ms": round(p50(lats), 1),
        "p95_latency_ms": round(p95(lats), 1),
        "std_latency_ms": round(std(lats), 1),
    }


def cache_stats(rows: List[dict]) -> Dict[str, Any]:
    """缓存指标：cached_tokens / cache_hit_rate / recompute_tokens。"""
    prompt = sum(r.get("prompt_tokens", 0) for r in rows)
    cached = sum(r.get("cached_tokens", 0) for r in rows)
    return {
        "cached_tokens": cached,
        "cache_hit_rate": round(cached / prompt, 4) if prompt > 0 else None,
        "recompute_tokens": max(0, prompt - cached),
    }


def summarize(rows: List[dict]) -> Dict[str, Any]:
    """场景行汇总：旧字段 + E0 新字段（不破坏旧字段名）。"""
    totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for r in rows:
        for k in totals:
            totals[k] += r[k]
    return {
        **totals,
        "rounds": len(rows),
        **latency_stats(rows),
        "peak_rss_mb": max((r["rss_mb"] for r in rows if r.get("rss_mb")), default=None),
        "peak_gpu_mb": max((r["gpu_mb"] for r in rows if r.get("gpu_mb")), default=None),
        **cache_stats(rows),
    }
