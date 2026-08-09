"""fanout_prompts 纯函数测试（设计 §5.1 测试要求① 决策/确定性/canary/预算）。"""
from __future__ import annotations

import pytest

from runner.fanout_prompts import (
    BUCKETS,
    BUCKET_TARGETS,
    BUDGET_THRESHOLD,
    FANOUTS,
    MATRIX_PLAN,
    budget,
    build_branch_task,
    build_context,
    canary_for,
    decision_invalid,
    decision_messages,
    expected_unit_ids,
    fallback_branch,
    is_budget_legal,
    legal_matrix_plan,
    parse_action,
    tool_round_message,
)


class TestDecisionProtocol:
    def test_decision_messages_deterministic(self):
        m1 = decision_messages("ctx", 2)
        m2 = decision_messages("ctx", 2)
        assert m1 == m2
        assert m1[0]["role"] == "system"
        assert "ACTION: branch(b1) | ACTION: branch(b2)" in m1[1]["content"]

    def test_decision_messages_fanout_invalid(self):
        with pytest.raises(ValueError):
            decision_messages("ctx", 3)

    def test_decision_messages_enum_fanout8(self):
        m = decision_messages("ctx", 8)
        for i in range(1, 9):
            assert f"ACTION: branch(b{i})" in m[1]["content"]

    def test_parse_action(self):
        assert parse_action("ACTION: branch(b3)") == 3
        assert parse_action("前缀\nACTION: branch(b1)\n后缀") == 1
        assert parse_action("") == 0
        assert parse_action("ACTION: branch(b9)") == 9
        assert parse_action("branch(b2)") == 0  # 缺 ACTION: 前缀
        assert parse_action("ACTION: branch( b2 )") == 0  # 空格不匹配

    def test_decision_invalid_length(self):
        # finish_reason=length → INVALID_DECISION（即使含合法 ACTION）
        assert decision_invalid("ACTION: branch(b2)", "length", 4) is True

    def test_decision_invalid_no_action(self):
        assert decision_invalid("随便输出", "stop", 4) is True
        assert decision_invalid("", "stop", 4) is True

    def test_decision_invalid_valid(self):
        assert decision_invalid("ACTION: branch(b4)", "stop", 4) is False
        assert decision_invalid("ACTION: branch(b2)", "stop", 2) is False

    def test_decision_invalid_out_of_range(self):
        # fanout=2 时输出 b3 → 非法（枚举受限）
        assert decision_invalid("ACTION: branch(b3)", "stop", 2) is True

    def test_fallback_branch_fixed(self):
        # 固定路由 fallback：rep_index 模 fanout
        assert fallback_branch(2, 0) == "b1"
        assert fallback_branch(2, 1) == "b2"
        assert fallback_branch(2, 2) == "b1"
        assert fallback_branch(4, 3) == "b4"


class TestCanary:
    def test_canary_deterministic_and_distinct(self):
        c1 = canary_for(2, "b1")
        c2 = canary_for(2, "b1")
        assert c1 == c2
        assert c1.startswith("CANARY-B2-")
        assert canary_for(2, "b1") != canary_for(2, "b2")
        assert canary_for(4, "b1") != canary_for(8, "b1")

    def test_canary_in_branch_messages(self):
        from runner.fanout_prompts import branch_messages
        canary = canary_for(2, "b1")
        msgs = branch_messages(decision_messages("ctx", 2), "ACTION: branch(b1)",
                               "b1", "task", canary)
        assert canary in msgs[-1]["content"]

    def test_tool_round_message_protected(self):
        m = tool_round_message("b2", 1)
        assert m["role"] == "user"
        assert "<tool_response>" in m["content"]
        assert "</tool_response>" in m["content"]
        assert '"branch": "b2"' in m["content"]
        assert "tool" in m["content"]


class TestBudget:
    def test_budget_formula(self):
        # budget(P,B,N) = P + N*(P+B)
        assert budget(150, 150, 2) == 150 + 2 * 300 == 750
        assert budget(280, 480, 2) == 280 + 2 * 760 == 1800
        assert budget(400, 600, 2) == 400 + 2 * 1000 == 2400
        assert budget(150, 150, 4) == 150 + 4 * 300 == 1350
        assert budget(280, 480, 4) == 280 + 4 * 760 == 3320
        assert budget(150, 150, 8) == 150 + 8 * 300 == 2550

    def test_budget_illegal_combinations(self):
        assert budget(280, 480, 8) == 280 + 8 * 760 == 6360
        assert budget(400, 600, 4) == 400 + 4 * 1000 == 4400
        assert is_budget_legal(280, 480, 8) is False
        assert is_budget_legal(400, 600, 4) is False

    def test_budget_threshold(self):
        assert BUDGET_THRESHOLD == 3481  # 4096 * 0.85

    def test_budget_negative_raises(self):
        with pytest.raises(ValueError):
            budget(-1, 100, 2)

    def test_legal_matrix_plan_matches_doc(self):
        plan = legal_matrix_plan()
        # 文档合法矩阵：fanout2→3桶、fanout4→2桶、fanout8→1桶
        assert set(plan.keys()) == {2, 4, 8}
        assert plan[2] == ["short", "medium", "long"]
        assert plan[4] == ["short", "medium"]
        assert plan[8] == ["short"]
        assert "long" not in plan[4]  # 4400 > 3481
        assert "medium" not in plan[8]  # 6360 > 3481
        assert "long" not in plan[8]

    def test_expected_unit_ids_count_and_format(self):
        ids = expected_unit_ids()
        assert len(ids) == 24  # 6 合法组合 × 2 profile × 2 control
        assert len(set(ids)) == 24
        assert "off:q8_0-q8_0:f2:short" in ids
        assert "on:f16-f16:f8:short" in ids
        # 被预算排除的组合不出现
        assert not any(u.endswith(":f4:long") for u in ids)
        assert not any(u.endswith(":f8:medium") for u in ids)


class TestContext:
    def test_build_context_deterministic(self):
        c1 = build_context("short")
        c2 = build_context("short")
        assert c1 == c2
        assert build_context("short") != build_context("long")

    def test_build_context_bucket_invalid(self):
        with pytest.raises(ValueError):
            build_context("tiny")

    def test_build_branch_task_deterministic(self):
        t1 = build_branch_task("medium", "b3")
        t2 = build_branch_task("medium", "b3")
        assert t1 == t2
        assert "b3" in t1

    def test_bucket_targets_keys(self):
        assert set(BUCKET_TARGETS.keys()) == set(BUCKETS)
        for b in BUCKETS:
            p, br = BUCKET_TARGETS[b]
            assert p > 0 and br > 0
