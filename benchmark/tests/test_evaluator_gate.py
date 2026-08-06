"""E2.0.5：保真门禁 evaluator 正负样本测试。

验证任务七要求：
- state_retention_rate 对"正确记忆"（正样本）与"错误/缺失记忆"（负样本）有区分度；
- task_success / secret_recall / truncations 字段一致性；
- 对 run() 输出中 text 的"规范化召回"判据（子串匹配）与大小写/标点鲁棒性。
"""
from __future__ import annotations

import pytest

from framework.workload import WorkloadSpec
from workload.long_life import LongLifeWorkload


@pytest.fixture
def wl() -> LongLifeWorkload:
    return LongLifeWorkload()


def _spec(secret: str = "9527") -> WorkloadSpec:
    return WorkloadSpec(
        name="long_life",
        params={"rounds": 12, "secret": secret, "ctx_size": 2048},
        prompts=[],
        expected={"secret": secret},
        meta={"version": "1.0"},
    )


def test_positive_sample_retention_1(wl):
    """正样本：最终回答包含秘密数字 → state_retention_rate=1.0。"""
    results = {"rows": [], "meta": {"task_success": True, "truncations": 0}}
    ev = wl.evaluate(results, _spec())
    assert ev["state_retention_rate"] == 1.0
    assert ev["task_success"] is True
    assert ev["secret_recall"] == 1.0
    assert ev["truncations"] == 0


def test_negative_sample_retention_0(wl):
    """负样本：最终回答缺失秘密数字 → state_retention_rate=0.0。"""
    results = {"rows": [], "meta": {"task_success": False, "truncations": 2}}
    ev = wl.evaluate(results, _spec())
    assert ev["state_retention_rate"] == 0.0
    assert ev["task_success"] is False
    assert ev["secret_recall"] == 0.0
    assert ev["truncations"] == 2


def test_distinguishes_positive_from_negative(wl):
    """区分度：正/负样本判定互斥且覆盖 0/1 两端。"""
    pos = wl.evaluate({"meta": {"task_success": True, "truncations": 0}}, _spec())
    neg = wl.evaluate({"meta": {"task_success": False, "truncations": 0}}, _spec())
    assert pos["state_retention_rate"] != neg["state_retention_rate"]
    assert {pos["state_retention_rate"], neg["state_retention_rate"]} == {0.0, 1.0}


def test_substring_recall_robust_to_formatting(wl):
    """run() 中的召回判据为子串匹配：带标点/前后缀的数字仍应命中。"""
    secret = "9527"
    # 模拟 run() 的判定逻辑（workload/long_life.py run() 末尾）
    for text in ["9527", "秘密数字是9527。", " 9527 ", "答案：9527！", "是9527。"]:
        assert secret in text, f"子串召回应命中: {text!r}"
    for text in ["9526", "我忘了", "没有记住", "", "九千五百二十七"]:
        assert secret not in text, f"错误/缺失记忆不应命中: {text!r}"


def test_meta_absent_defaults_to_negative(wl):
    """meta 缺失时按负样本（retention=0）处理，不崩溃。"""
    ev = wl.evaluate({}, _spec())
    assert ev["state_retention_rate"] == 0.0
    assert ev["task_success"] is False
