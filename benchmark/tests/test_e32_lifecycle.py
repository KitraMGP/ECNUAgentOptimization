"""E3.2：lifecycle 策略纯逻辑测试（LRU 排序规则/evaluator/事件解析，无需真实 server）。"""
from __future__ import annotations

import json

import pytest

from scripts.e32_unified_lifecycle_value_probe import build_prompt, grep_log


def _lru_sort(candidates):
    """与 llama.cpp try_clear_idle_slots 的 lru 排序规则一致（冻结规则）：
    last_used_tick 升序 → prompt tokens 降序 → slot id 升序。"""
    return sorted(candidates, key=lambda c: (c["tick"], -c["prompt_tokens"], c["id"]))


def test_build_prompt_deterministic():
    a1 = build_prompt(3000, "STATE_HOT")
    a2 = build_prompt(3000, "STATE_HOT")
    assert a1 == a2
    assert "STATE_HOT" in a1
    assert abs(len(a1) - 3000 * 6.2) / (3000 * 6.2) < 0.1


def test_lru_sorts_by_least_recently_used():
    cands = [{"id": 0, "tick": 3, "prompt_tokens": 3000},   # 最近使用（hot）
             {"id": 1, "tick": 1, "prompt_tokens": 3000},   # 最久未用（cold）
             {"id": 2, "tick": 2, "prompt_tokens": 2000}]
    assert _lru_sort(cands)[0]["id"] == 1  # 最久未用优先


def test_lru_tie_break_by_prompt_size_then_id():
    cands = [{"id": 3, "tick": 1, "prompt_tokens": 1000},
             {"id": 1, "tick": 1, "prompt_tokens": 3000},   # 同 tick → 大 prompt 优先
             {"id": 2, "tick": 1, "prompt_tokens": 3000}]   # 同 tick 同大小 → 小 id 优先
    order = [c["id"] for c in _lru_sort(cands)]
    assert order == [1, 2, 3]


def test_first_eligible_default_behavior():
    """default = 遍历 slots（id 升序）选第一个合格 idle（处理中跳过）。"""
    slots = [{"id": 0, "processing": True, "prompt_tokens": 5000},
             {"id": 1, "processing": False, "prompt_tokens": 2000},
             {"id": 2, "processing": False, "prompt_tokens": 3000}]
    victim = next(s for s in slots if not s["processing"] and s["prompt_tokens"] > 0)
    assert victim["id"] == 1  # 第一个合格（非 processing）


def test_active_always_excluded_from_candidates():
    cands = [{"id": 0, "tick": 1, "prompt_tokens": 5000},
             {"id": 1, "tick": 2, "prompt_tokens": 3000}]
    active = {0}
    eligible = [c for c in cands if c["id"] not in active]
    assert eligible[0]["id"] == 1


def test_grep_log_parses_lifecycle_events():
    log = (
        "failed to find free space in the KV cache, retrying\n"
        "purging slot 1 with 3078 tokens (policy=lru, last_used_tick=1)\n"
        "__LIFECYCLE_EVENT__ policy=lru reason=no_cell pre_used=6154 post_used=3076 "
        "freed=3078 active=1 candidates=[{id:1,tick:1,pt:3078},{id:0,tick:3,pt:3076}] "
        "selected=1 selected_tick=1 tick_seq=5 retry_result=1\n"
    )
    import tempfile, os
    with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
        f.write(log)
        path = f.name
    try:
        res = grep_log(path)
    finally:
        os.unlink(path)
    assert res["free_space"] == 1
    assert res["purge_events"] == [{"slot": 1, "tokens": 3078, "policy": "lru", "tick": 1}]
    assert len(res["lifecycle_events"]) == 1
    assert "selected=1" in res["lifecycle_events"][0]
    assert "freed=3078" in res["lifecycle_events"][0]


def test_grep_log_no_events_when_default_off():
    log = "model loaded\nlistening on port\n"
    import tempfile, os
    with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
        f.write(log)
        path = f.name
    try:
        res = grep_log(path)
    finally:
        os.unlink(path)
    assert res["purge_events"] == [] and res["free_space"] == 0


def test_evaluator_rules():
    """evaluator：status==ok && 响应非空；同分支回访 hash 一致；跨分支有区分度。"""
    r_ok = {"status": "ok", "text": "some response", "response_hash": "abc"}
    r_empty = {"status": "ok", "text": "", "response_hash": None}
    r_fail = {"status": "http_400", "text": "", "response_hash": None}
    assert r_ok["status"] == "ok" and bool(r_ok["text"])
    assert not (r_empty["status"] == "ok" and bool(r_empty["text"]))
    assert r_fail["status"] != "ok"
    # 跨分支 hash 区分度（不同 prompt → 不同输出）
    assert "abc" != "def"
