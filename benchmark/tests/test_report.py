"""Report markdown 生成测试。"""
from __future__ import annotations

from report.markdown import generate_markdown_report
from tests.conftest import default_row
from metrics.metrics import summarize


def _sample_result():
    rows = [default_row(latency_ms=100.0), default_row(latency_ms=300.0)]
    sm = summarize(rows)
    sm["evaluation"] = {"task_success": True}
    return {
        "config": {"scenario": "multi_turn", "ctx_size": 2048,
                   "repeat": 1, "warmup": 0, "server_url": "http://127.0.0.1:8080/v1",
                   "temperature": 0.0, "seed": 42},
        "summary": {"multi_turn": sm},
        "scenarios": {"multi_turn": rows},
    }


def test_markdown_contains_sections():
    md = generate_markdown_report(_sample_result())
    assert "# Benchmark 实验报告" in md
    assert "## 1. 运行配置" in md
    assert "## 2. 场景指标" in md
    assert "### multi_turn" in md
    assert "cache_hit_rate" in md
    assert "p50_latency_ms" in md
    assert "任务判据（保真约束）" in md
    assert "task_success" in md
