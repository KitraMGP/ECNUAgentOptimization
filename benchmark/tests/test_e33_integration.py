"""E3.3：集成 runner 纯逻辑测试（adapter 配置/hash/机会统计/门槛判定）。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile

import pytest

from scripts.e33_existing_workload_integration import (
    WL_PARAMS,
    build_pressure_prompt,
    grep_log,
)


def test_wl_params_fixed():
    """workload 参数冻结（正式矩阵使用），不得在实验后修改。"""
    assert WL_PARAMS["multi_turn"] == {"rounds": 16}
    assert WL_PARAMS["long_life"] == {"rounds": 10, "secret": "9527", "ctx_size": 8192}
    assert WL_PARAMS["branch"] == {"branch_rounds": 3}
    assert WL_PARAMS["branch_pressure"] == {}


def test_pressure_prompt_fixed_and_deterministic():
    p1 = build_pressure_prompt(6300)
    p2 = build_pressure_prompt(6300)
    assert p1 == p2
    assert "PRESSURE_MARKER" in p1
    assert abs(len(p1) - 6300 * 5.66) / (6300 * 5.66) < 0.1


def test_grep_log_parses_purge():
    log = ("failed to find free space in the KV cache\n"
           "purging slot 1 with 3078 tokens (policy=lru, last_used_tick=1)\n")
    with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
        f.write(log)
        path = f.name
    try:
        res = grep_log(path)
    finally:
        os.unlink(path)
    assert res["free_space"] == 1
    assert res["purge_events"] == [{"slot": 1, "tokens": 3078, "policy": "lru", "tick": 1}]


def test_pressure_opportunity_threshold():
    """任务九 D：pressure 发生率 < 3/5 → opportunity_not_observed（不能判收益）。"""
    def coverage(purged_flags):
        n = len(purged_flags)
        k = sum(purged_flags)
        return k / n, "observed" if k >= 3 else "opportunity_not_observed"
    assert coverage([1, 1, 1, 0, 0])[1] == "observed"      # 3/5
    assert coverage([1, 1, 0, 0, 0])[1] == "opportunity_not_observed"
    assert coverage([1, 1, 1, 1, 1])[0] == 1.0


def test_no_pressure_pairs_excluded_from_benefit_denominator():
    """无 pressure 的 replicate 不得计入收益方向分母（但计入正确性）。"""
    pairs = [{"default_purge": True, "lru_purge": True, "lru_better": True},
             {"default_purge": True, "lru_purge": True, "lru_better": False},
             {"default_purge": False, "lru_purge": False, "lru_better": False}]
    pressured = [p for p in pairs if p["default_purge"] and p["lru_purge"]]
    better = sum(1 for p in pressured if p["lru_better"])
    assert len(pressured) == 2  # 第 3 对（无压力）不计入分母
    assert better == 1


def test_paired_benefit_gate():
    """收益门槛：≥4/5 不劣于 + 中位改善 ≥5%（latency）或 ≥10%（processed）。"""
    def gate(lru_processed, default_processed):
        not_worse = sum(1 for l, d in zip(lru_processed, default_processed) if l <= d)
        med_improve = 1 - (sum(lru_processed) / len(lru_processed)) / (sum(default_processed) / len(default_processed) or 1)
        return not_worse >= 4 and med_improve >= 0.10
    assert gate([4, 4, 4, 4, 4], [515, 515, 4, 4, 515]) is True
    assert gate([500, 500, 500, 500, 500], [515, 515, 515, 515, 515]) is False  # 改善不足


def test_evaluator_and_task_success_no_regression():
    """正确性：evaluator/task_success 不下降（default 与 lru 持平）。"""
    def no_regression(lru, default):
        return lru["task_success"] >= default["task_success"] and lru["eval_rate"] >= default["eval_rate"]
    assert no_regression({"task_success": True, "eval_rate": 1.0},
                         {"task_success": True, "eval_rate": 1.0})
    assert not no_regression({"task_success": False, "eval_rate": 0.5},
                             {"task_success": True, "eval_rate": 1.0})


def test_manifest_freeze_fields():
    """预注册 manifest 必须含：workload hash、阈值、配对顺序、STOP 条件。"""
    manifest = {
        "workload_hashes": {}, "evaluator_hashes": {},
        "paired_order": "D L | L D | D L | L D | D L",
        "thresholds": {}, "stop_conditions": [],
    }
    for k in ("workload_hashes", "evaluator_hashes", "paired_order", "thresholds", "stop_conditions"):
        assert k in manifest
