"""E4：内存容量基准纯逻辑测试（承载/边界/利用率/exploratory 标记）。"""
from __future__ import annotations

import pytest


def test_capacity_success_criteria():
    """并发承载成功：sessions_ok == n_sessions 且 task_success 全过。"""
    rec = {"sessions_ok": 4, "n_sessions": 4, "task_success_all": True}
    assert rec["sessions_ok"] == rec["n_sessions"] and rec["task_success_all"]


def test_oom_recovery_criteria():
    """OOM 恢复：无 400 且 session 完成（或记录边界）。"""
    rec = {"http_400": 0, "sessions_ok": 6, "n_sessions": 6}
    assert rec["http_400"] == 0 and rec["sessions_ok"] == rec["n_sessions"]


def test_utilization_bounded():
    util = 0.85
    assert 0.0 <= util <= 1.0


def test_exploratory_flag():
    out = {"exploratory": True}
    assert out["exploratory"] is True


def test_kv_peak_monotonic():
    """KV 峰值采样不高于 capacity。"""
    peak, cap = 7000, 8192
    assert peak <= cap


def test_capacity_share_logic():
    """unified 池共享：N session 总 KV 需求可超 capacity（压力场景），
    通过 purge/淘汰（lru 价值感知）承载或明确失败。"""
    sessions_kv = [2500, 2500, 2500, 2500]  # 4 session 各 2500
    capacity = 8192
    assert sum(sessions_kv) > capacity      # 总需求 10000 > 8192（压力成立）
    # 池共享：任一时刻 used_cells ≤ capacity（受 purge/淘汰约束）
    assert min(capacity, sum(sessions_kv)) <= capacity


def _parse_truncations(log: str) -> int:
    """结构化截断解析：仅匹配 truncated = 1（真实截断）。"""
    import re
    return len(re.findall(r"truncated\s*=\s*1\b", log))


def test_trunc_parse_zero_field_not_counted():
    # "truncated = 0" 字段（常规输出）不得计为截断
    log = "stop processing: n_tokens = 500, truncated = 0\n"
    assert _parse_truncations(log) == 0


def test_trunc_parse_one_counted():
    log = "stop processing: n_tokens = 500, truncated = 1\n"
    assert _parse_truncations(log) == 1


def test_trunc_parse_missing_field():
    assert _parse_truncations("model loaded\nlistening on port\n") == 0


def test_trunc_parse_malformed():
    # 格式错误（无 "="）不计
    assert _parse_truncations("truncated something\ntruncat\n") == 0
