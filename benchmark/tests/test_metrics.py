"""Metrics 数值单元测试（纯函数，无外部依赖）。"""
from __future__ import annotations

from metrics.metrics import (
    cache_stats,
    mean,
    p50,
    p95,
    percentile,
    std,
    summarize,
)
from tests.conftest import default_row


def test_mean_std():
    assert mean([1, 2, 3]) == 2.0
    assert mean([]) is None
    assert std([1, 2, 3]) == 1.0
    assert std([5]) == 0.0
    assert std([]) is None


def test_percentile():
    assert percentile([], 50) is None
    assert percentile([1, 2, 3, 4], 50) == 2.0   # nearest-rank
    assert percentile([1, 2, 3], 95) == 3.0
    xs = list(range(1, 101))                     # 1..100
    assert percentile(xs, 95) == 95.0
    assert p50(xs) == 50.0
    assert p95(xs) == 95.0


def test_cache_stats():
    rows = [
        default_row(prompt_tokens=100, cached_tokens=60),
        default_row(prompt_tokens=200, cached_tokens=0),
    ]
    cs = cache_stats(rows)
    assert cs["cached_tokens"] == 60
    assert cs["cache_hit_rate"] == 0.2
    assert cs["recompute_tokens"] == 240


def test_cache_stats_empty_prompt():
    cs = cache_stats([default_row(prompt_tokens=0, cached_tokens=0)])
    assert cs["cache_hit_rate"] is None


def test_summarize_legacy_fields():
    rows = [
        default_row(prompt_tokens=100, completion_tokens=20, total_tokens=120,
                    latency_ms=100.0, rss_mb=50.0, gpu_mb=100.0, cached_tokens=80),
        default_row(prompt_tokens=200, completion_tokens=30, total_tokens=230,
                    latency_ms=300.0, rss_mb=60.0, gpu_mb=110.0, cached_tokens=0),
    ]
    sm = summarize(rows)
    # 旧字段（与 agent_bench.summarize 一致）
    assert sm["prompt_tokens"] == 300
    assert sm["completion_tokens"] == 50
    assert sm["total_tokens"] == 350
    assert sm["rounds"] == 2
    assert sm["avg_latency_ms"] == 200.0
    assert sm["max_latency_ms"] == 300.0
    assert sm["peak_rss_mb"] == 60.0
    assert sm["peak_gpu_mb"] == 110.0
    # E0 新字段
    assert sm["p50_latency_ms"] == 100.0
    assert sm["p95_latency_ms"] == 300.0
    assert sm["cached_tokens"] == 80
    assert sm["cache_hit_rate"] == round(80 / 300, 4)
    assert sm["recompute_tokens"] == 220


def test_summarize_none_memory():
    sm = summarize([default_row(rss_mb=None, gpu_mb=None)])
    assert sm["peak_rss_mb"] is None
    assert sm["peak_gpu_mb"] is None


def test_summarize_throughput():
    rows = [
        default_row(total_tokens=120, latency_ms=1000.0,
                    timings={"predicted_per_second": 50.0}),
        default_row(total_tokens=230, latency_ms=3000.0,
                    timings={"predicted_per_second": 30.0}),
    ]
    sm = summarize(rows)
    # 端到端吞吐 = (120+230) tokens / 4s
    assert sm["throughput_tps"] == round(350 / 4.0, 2)
    # decode 吞吐 = timings.predicted_per_second 均值
    assert sm["decode_tps"] == 40.0


def test_summarize_throughput_no_timings():
    rows = [default_row(total_tokens=100, latency_ms=1000.0, timings=None)]
    sm = summarize(rows)
    assert sm["throughput_tps"] == round(100 / 1.0, 2)
    assert sm["decode_tps"] is None


def test_throughput_stats_empty():
    from metrics.metrics import throughput_stats
    assert throughput_stats([]) == {"throughput_tps": None, "decode_tps": None}
