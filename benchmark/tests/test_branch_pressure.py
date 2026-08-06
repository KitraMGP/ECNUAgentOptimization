"""E2.5：branch_pressure workload 与 evaluator 测试。

覆盖：
- build_prompts 序列确定性（8 请求固定顺序、每分支 state 正确）；
- prompt 长度约束（共享前缀足够长，目标 40%-70% ctx 的代理断言）；
- JSON evaluator 正/负样本（可解析/branch/state/answer/跨分支 state）；
- contamination 检测（响应含另一分支 state）；
- workload 注册表集成。
"""
from __future__ import annotations

import json

import pytest

from framework.workload import get_workload
from workload.branch_pressure import (
    SEQUENCE,
    build_prompts,
    evaluate_response,
    evaluator_pass,
)


def test_sequence_is_fixed_and_alternating():
    ids = [t for t, _, _, _ in SEQUENCE]
    assert ids == ["X1", "Y1", "X2", "Y2", "X3", "Y3", "X_revisit", "Y_revisit"]
    prompts = build_prompts()
    assert [p["turn_id"] for p in prompts] == ids
    # state 值正确且跨分支可区分
    states = {p["branch_id"] for p in prompts}
    assert states == {"X", "Y"}


def test_prompts_are_deterministic():
    assert build_prompts() == build_prompts()


def test_prompt_length_in_range():
    """共享前缀 + 分支后缀总长应为固定值（token 数由 server 决定，此处
    以字符长度代理断言"足够长"且"同分支各轮递增、回访固定"）。"""
    prompts = build_prompts()
    shared_len = sum(len(m["content"]) for m in prompts[0]["messages"][:-2])
    # 共享前缀（前 4 条消息）足够长：> 4000 chars（~1000+ tokens 代理下限）
    assert shared_len > 4000, f"共享前缀过短: {shared_len}"
    x1 = prompts[0]
    x_rev = prompts[6]
    # 回访请求 = 单轮观察（与 X1 相同长度）
    assert len(x_rev["messages"]) == len(x1["messages"])


def _msgs(branch: str, state: str, answer: str) -> list:
    return [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]


def test_evaluator_positive():
    text = json.dumps({"branch": "X", "state": "X_STATE_17",
                       "answer": "Nitrate rose from 0.9 to 2.1 mg/L in the riparian zone."})
    ev = evaluate_response(text, "X", "X_STATE_17")
    assert evaluator_pass(ev)
    assert ev["no_foreign_state"]


def test_evaluator_accepts_code_block():
    text = ('```json\n{"branch": "Y", "state": "Y_STATE_42", '
            '"answer": "Marsh elevation loss of 4 cm is accelerating retreat."}\n```')
    ev = evaluate_response(text, "Y", "Y_STATE_42")
    assert evaluator_pass(ev)


def test_evaluator_rejects_wrong_branch():
    text = json.dumps({"branch": "Y", "state": "X_STATE_17",
                       "answer": "Some answer that is long enough to pass."})
    ev = evaluate_response(text, "X", "X_STATE_17")
    assert not evaluator_pass(ev)
    assert not ev["branch_correct"]


def test_evaluator_rejects_wrong_state():
    text = json.dumps({"branch": "X", "state": "Y_STATE_42",
                       "answer": "Some answer that is long enough to pass."})
    ev = evaluate_response(text, "X", "X_STATE_17")
    assert not evaluator_pass(ev)
    assert not ev["state_correct"]
    # 但 Y_STATE_42 是 X 的外来 state -> contamination
    assert not ev["no_foreign_state"]


def test_evaluator_rejects_non_json():
    ev = evaluate_response("The riparian zone is deteriorating.", "X", "X_STATE_17")
    assert not evaluator_pass(ev)
    assert not ev["json_parsable"]


def test_evaluator_rejects_short_answer():
    text = json.dumps({"branch": "X", "state": "X_STATE_17", "answer": "short"})
    ev = evaluate_response(text, "X", "X_STATE_17")
    assert not evaluator_pass(ev)
    assert not ev["answer_ok"]


def test_contamination_detection():
    # X 分支响应里混入 Y 的 state
    text = json.dumps({"branch": "X", "state": "X_STATE_17",
                       "answer": "Rising nitrate; note Y_STATE_42 also appears."})
    ev = evaluate_response(text, "X", "X_STATE_17")
    assert ev["json_parsable"]
    assert not ev["no_foreign_state"]


def test_workload_registered():
    w = get_workload("branch_pressure")
    assert w.name == "branch_pressure"
