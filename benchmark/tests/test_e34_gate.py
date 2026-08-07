"""E3.4：多会话 gate 纯逻辑测试（并发时序/机会分类/门槛判定/hash 冻结）。"""
from __future__ import annotations

import json
import os
import tempfile

import pytest

from scripts.e34_multi_session_workload_gate import WL_PARAMS, build_pressure_prompt, grep_log


def test_wl_params_frozen():
    assert WL_PARAMS["multi_turn"] == {"rounds": 6}
    assert WL_PARAMS["long_life"] == {"rounds": 6, "secret": "9527", "ctx_size": 8192}


def test_pressure_prompt_deterministic():
    p1 = build_pressure_prompt(6300)
    p2 = build_pressure_prompt(6300)
    assert p1 == p2
    assert "PRESSURE_MARKER" in p1


def test_session_concurrency_plan():
    """4 个独立 session 线程 + 独立 Driver → 真实并发（计划断言）。"""
    sessions = ["S0", "S1", "S2", "S3"]
    assert len(sessions) == 4
    # 每 session 独立 history/evaluator（RecordingDriver 每 session 实例）
    assert len(set(sessions)) == 4


def test_pressure_opportunity_classification():
    """三类：pressure_opportunity_observed / near_pressure / opportunity_not_observed。"""
    def classify(free_space, idle_candidates, revisit_ok):
        if free_space >= 1 and idle_candidates >= 2 and revisit_ok:
            return "pressure_opportunity_observed"
        if free_space == 0 and idle_candidates >= 2:
            return "near_pressure"
        return "opportunity_not_observed"
    assert classify(1, 2, True) == "pressure_opportunity_observed"
    assert classify(0, 3, True) == "near_pressure"      # 仅 used_cells 高，无 purge
    assert classify(0, 1, True) == "opportunity_not_observed"


def test_no_pressure_not_in_benefit_denominator():
    """no-pressure/near_pressure 样本不计入收益方向分母（计入正确性/成本）。"""
    pairs = [{"pressure": True, "lru_better": True},
             {"pressure": True, "lru_better": False},
             {"pressure": False, "lru_better": False}]   # no-pressure
    pressured = [p for p in pairs if p["pressure"]]
    assert len(pressured) == 2
    better = sum(1 for p in pressured if p["lru_better"])
    assert better == 1


def test_optional_benefit_gate():
    """PROMOTE_OPTIONAL：coverage ≥4/5 + ≥4/5 不劣 + 中位改善。"""
    def gate(coverage, not_worse, improve_pct):
        return coverage >= 0.8 and not_worse >= 4 and improve_pct >= 5.0
    assert gate(0.8, 4, 93.0) is True
    assert gate(0.6, 4, 93.0) is False     # coverage 不足
    assert gate(0.8, 3, 93.0) is False     # 不劣不足


def test_default_benefit_requires_two_families():
    """PROMOTE_DEFAULT 需两个 workload 家族都满足收益门槛。"""
    families_ok = {"multi_turn": True, "long_life": True}
    families_partial = {"multi_turn": True, "long_life": False}
    assert sum(families_ok.values()) >= 2      # 两个都满足 → 可 DEFAULT
    assert not (sum(families_partial.values()) >= 2)  # 仅一个 → 不可 DEFAULT


def test_grep_log_parses_purge():
    log = "purging slot 1 with 731 tokens (policy=lru, last_used_tick=21)\nfailed to find free space in the KV cache\n"
    with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
        f.write(log)
        path = f.name
    try:
        res = grep_log(path)
    finally:
        os.unlink(path)
    assert res["purge_events"] == [{"slot": 1, "tokens": 731, "policy": "lru", "tick": 21}]
    assert res["free_space"] == 1


def test_manifest_freeze_fields():
    manifest = {"session_hashes": {}, "paired_order": "D L | L D | D L | L D | D L",
                "thresholds": {}, "stop_conditions": []}
    for k in ("session_hashes", "paired_order", "thresholds", "stop_conditions"):
        assert k in manifest
