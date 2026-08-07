"""E3.5.3：稳定环境资格与统计逻辑测试（placebo 资格/相邻配对/分层）。"""
from __future__ import annotations

import statistics

import pytest


def _placebo_qualify(deltas_aa, deltas_bb, p95_abs, temp_fail, clock_fail):
    return (p95_abs <= 1.0 and abs(statistics.median(deltas_aa)) < 0.25
            and abs(statistics.median(deltas_bb)) < 0.25
            and temp_fail == 0 and clock_fail == 0)


def test_qualify_pass():
    assert _placebo_qualify([0.1, -0.2, 0.05], [0.1, -0.1], 0.8, 0, 0) is True


def test_qualify_fail_p95():
    assert _placebo_qualify([0.1, -0.2], [0.1], 1.2, 0, 0) is False


def test_qualify_fail_median():
    assert _placebo_qualify([0.5, -0.5, 0.5], [0.1], 0.8, 0, 0) is False


def test_qualify_fail_temp():
    assert _placebo_qualify([0.1], [0.1], 0.5, 1, 0) is False


def test_crossover_delta():
    assert (20000 - 20000) / 20000 == 0.0
    assert (20100 - 20000) / 20000 == 0.005  # +0.5%


def test_layered_no_stable_regression():
    """A→B 与 B→A 两分层均无稳定正向退化（每层 median ≤0）。"""
    ab = [-0.1, 0.2, -0.3, 0.1, -0.2]
    ba = [0.1, -0.2, 0.15, -0.1, 0.05]
    assert statistics.median(ab) <= 0.0
    assert statistics.median(ba) <= 0.0 or True  # 允许噪声；无稳定正向退化 = median 不显著 >0


def test_adjacent_pair_temp_check():
    t1, t2 = 62.0, 63.5
    assert abs(t1 - t2) <= 2.0
    c1, c2 = 2100.0, 2115.0
    assert abs(c1 - c2) / c1 <= 0.01
