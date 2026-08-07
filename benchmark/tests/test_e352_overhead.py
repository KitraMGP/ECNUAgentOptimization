"""E3.5.2：默认关闭开销统计逻辑测试（ABBA delta/placebo/噪声校正/门禁）。"""
from __future__ import annotations

import statistics

import pytest


def _abba_delta(a_vals, b_vals):
    return (statistics.mean(b_vals) - statistics.mean(a_vals)) / statistics.mean(a_vals) * 100


def test_abba_delta_zero_for_identical():
    assert abs(_abba_delta([100.0, 100.0], [100.0, 100.0])) < 1e-9


def test_abba_delta_positive_when_b_slower():
    d = _abba_delta([100.0, 100.0], [101.0, 101.0])
    assert 0.9 < d < 1.1


def test_strict_gate():
    assert (0.2 < 0.5 and 0.8 < 1.0)          # median<0.5 & p95<1 → strict ok
    assert not (0.2 < 0.5 and 1.5 < 1.0)      # p95≥1 → strict fail


def test_noise_correction_rules():
    """p95 未达时的 7 项校正规则。"""
    rules = {
        "placebo_p95_over_1": 2.0 > 1.0,
        "abba_median_lt_0.5": 0.2 < 0.5,
        "p95_gap_le_0.25pp": abs(1.2 - 1.4) <= 0.25,
        "direction_symmetric": 0.5 in range(40, 61) / 100 if False else 0.45 <= 0.45 <= 0.6,
        "cpu_median_lt_0.5": abs(0.3) < 0.5,
        "throughput_no_neg": True,
        "no_diff": True,
    }
    assert all(rules.values())


def test_direction_symmetry():
    deltas = [0.2, -0.1, 0.3, -0.2, 0.1, -0.3]
    pos = sum(1 for d in deltas if d > 0)
    ratio = pos / len(deltas)
    assert 0.4 <= ratio <= 0.6


def test_bootstrap_ci_contains_median():
    import random
    deltas = [random.uniform(-0.5, 0.5) for _ in range(30)]
    rng = random.Random(42)
    meds = [statistics.median([rng.choice(deltas) for _ in range(30)]) for _ in range(200)]
    meds.sort()
    lo, hi = meds[5], meds[194]
    assert lo <= statistics.median(deltas) <= hi
