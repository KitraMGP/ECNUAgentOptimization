"""E2.4：Long-Branch probe 的确定性测试（无需真实 server）。

覆盖：
- build_sequence 确定性（bc2/bc4/long 序列固定、prompt 长度固定）；
- evaluator 逻辑：同 branch_id 响应 hash 一致（无串扰）、跨 branch 不同（有区分度）；
- branch identity 校验：跨分支响应 hash 不相同。
"""
from __future__ import annotations

import hashlib
import re

import pytest

from scripts.e2_long_branch_probe import build_sequence, _norm


def _hash(text: str) -> str:
    return _norm(text)


def test_build_sequence_deterministic():
    for branches in ("2", "4", "long"):
        s1 = build_sequence(branches)
        s2 = build_sequence(branches)
        assert s1 == s2, f"{branches}: 序列必须确定"
        # 每个 branch_id 内 prompt 长度固定（X/Y 之间长度可以不同）
        by_bid: dict = {}
        for bid, p in s1:
            by_bid.setdefault(bid, set()).add(len(p))
        for bid, lens in by_bid.items():
            assert len(lens) == 1, f"{branches}/{bid}: 同分支 prompt 长度应固定"


def test_bc2_sequence_has_two_rounds_of_x_y():
    seq = build_sequence("2")
    ids = [bid for bid, _ in seq]
    assert ids == ["X", "Y", "X", "Y"]  # 两轮 X/Y 交替回访


def test_bc4_sequence_alternates():
    seq = build_sequence("4")
    ids = [bid for bid, _ in seq]
    assert ids == ["X1", "Y1", "X2", "Y2", "X1", "Y1"]  # 分支数 > slot 数（有界回退场景）


def test_evaluator_detects_cross_branch_contamination():
    """模拟 evaluator：同分支 hash 一致 = 无串扰；跨分支不同 = 有区分度。"""
    hx = _hash("X branch answer 123")
    hy = _hash("Y branch answer 456")
    hx2 = _hash("X branch answer 123")  # 同分支回访输出一致
    # 无串扰场景
    by_branch = {"X": [hx, hx2], "Y": [hy]}
    ok = all(len(set(hs)) == 1 for hs in by_branch.values())
    distinct = len(set(h for hs in by_branch.values() for h in hs)) > 1
    assert ok and distinct
    # 串扰场景（X 回访输出了 Y 的内容）
    by_branch_bad = {"X": [hx, hy], "Y": [hy]}
    ok_bad = all(len(set(hs)) == 1 for hs in by_branch_bad.values())
    assert not ok_bad, "串扰必须被检出"


def test_norm_hash_is_stable_and_distinct():
    a = _hash("The quick brown fox")
    b = _hash("The quick brown fox")     # 相同输入 → 相同 hash
    c = _hash("The quick brown fox!")    # 规范化去标点 → 相同 hash（预期）
    d = _hash("The quick brown cat")
    assert a == b
    assert len(a) == 16
    assert a == c                         # 规范化语义：标点不影响 hash
    assert a != d                         # 内容不同 → hash 不同


def test_prompt_lengths_are_fixed():
    """固定 prompt token 长度（任务要求）：以字符长度代理（token 长度与字符
    长度单调相关，序列固定即 token 数固定）。"""
    seq = build_sequence("long")
    x_lens = [len(p) for b, p in seq if b == "X"]
    assert len(set(x_lens)) == 1, "同一分支的多轮 prompt 长度必须固定"
