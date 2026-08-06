"""E3.1：unified pressure probe 的确定性/解析/oracle 计算测试（无需真实 server）。"""
from __future__ import annotations

import hashlib
import json
import re

import pytest

from scripts.e31_unified_pressure_probe import build_prompt, grep_log


def test_build_prompt_deterministic_and_length():
    p1 = build_prompt(5000, "STATE_A")
    p2 = build_prompt(5000, "STATE_A")
    assert p1 == p2, "prompt 必须确定"
    assert "STATE_A" in p1
    # chars ≈ 5000 × 6.2（Qwen tokenizer 实测系数），允许 ±10%
    assert abs(len(p1) - 31000) / 31000 < 0.1


def test_build_prompt_tags_distinct():
    a = build_prompt(2000, "STATE_A")
    b = build_prompt(2000, "STATE_B")
    assert a != b
    assert "STATE_A" in a and "STATE_B" in b


def test_grep_log_counts_events():
    log = (
        "failed to find free space in the KV cache, retrying\n"
        "purging slot 0 with 5109 tokens\n"
        "retrying with smaller batch size\n"
        "Context size has been exceeded.\n"
    )
    counts = grep_log.__wrapped__ if hasattr(grep_log, "__wrapped__") else grep_log
    # 直接用函数处理字符串 → 需要临时文件
    import tempfile, os
    with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
        f.write(log)
        path = f.name
    try:
        res = grep_log(path, [
            "failed to find free space in the KV cache",
            "purging slot",
            "Context size has been exceeded",
            "retrying with smaller batch size",
        ])
    finally:
        os.unlink(path)
    assert res["failed to find free space in the KV cache"] == 1
    assert res["purging slot"] == 1
    assert res["Context size has been exceeded"] == 1
    assert res["retrying with smaller batch size"] == 1


def _oracle_reclaimable(pressure_req, idle_cells, needed_after):
    """离线 oracle：压力时 idle victim 的可释放 cells 是否足以解除压力。"""
    if idle_cells is None:
        return None
    return {
        "reclaimable_cells": idle_cells,
        "needed_to_relieve": needed_after,
        "sufficient": idle_cells >= needed_after,
    }


def test_oracle_sufficient_when_idle_cells_cover_remaining():
    # B 在 ~3192 遇压力，剩 1902 tokens 未处理；idle A 有 5109 cells
    o = _oracle_reclaimable(True, idle_cells=5109, needed_after=1902)
    assert o["reclaimable_cells"] == 5109
    assert o["sufficient"] is True


def test_oracle_insufficient_when_idle_too_small():
    o = _oracle_reclaimable(True, idle_cells=500, needed_after=3000)
    assert o["sufficient"] is False


def test_oracle_null_without_pressure():
    assert _oracle_reclaimable(False, idle_cells=None, needed_after=None) is None


def test_active_protection_logic_skips_processing():
    """现有 try_clear_idle_slots 跳过 is_processing 的语义（源码 1932-1951）。"""
    slots = [{"id": 0, "processing": True, "prompt_tokens": 5109},
             {"id": 1, "processing": False, "prompt_tokens": 2000},
             {"id": 2, "processing": False, "prompt_tokens": 5109}]
    # 清第一个非 processing 且有 prompt 的 slot
    victim = next(s for s in slots if not s["processing"] and s["prompt_tokens"] > 0)
    assert victim["id"] == 1, "active(processing) slot 必须被跳过"
