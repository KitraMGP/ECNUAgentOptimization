"""Workload 迁移一致性测试：FakeDriver 驱动四个场景，验证结果结构。"""
from __future__ import annotations

from tests.conftest import FakeDriver, default_row

# 导入即注册
import workload  # noqa: F401
from framework.workload import get_workload


def row_keys(row: dict) -> set:
    return set(row.keys())


def test_multi_turn_structure(fake_driver):
    wl = get_workload("multi_turn")
    spec = wl.generate({"rounds": 3})
    result = wl.run(fake_driver, spec)
    rows = result["rows"]
    assert len(rows) == 3
    assert rows[0]["round"] == 1
    # 旧行键 = {"round"} + chat() 返回键（含 E0 追加的 timings）
    expected = {"round", "text", "prompt_tokens", "completion_tokens",
                "total_tokens", "cached_tokens", "latency_ms", "rss_mb",
                "gpu_mb", "timings"}
    assert row_keys(rows[0]) == expected
    # 每轮 history 累积（消息数递增）
    assert len(fake_driver.calls[0]) == 2            # system + user
    assert len(fake_driver.calls[2]) == 6            # system + 5 条（u,a,u,a,u）
    ev = wl.evaluate(result, spec)
    assert ev["task_success"] is True
    assert ev["checked_rounds"] == 3


def test_multi_turn_evaluate_empty_text():
    wl = get_workload("multi_turn")
    spec = wl.generate({"rounds": 2})
    rows = [default_row(text=""), default_row(text="正常")]
    ev = wl.evaluate({"rows": rows}, spec)
    assert ev["task_success"] is False


def test_tool_call_full_chain():
    responses = [
        default_row(text="ACTION: search_orders(customer_id=C10086)"),
        default_row(text="ACTION: get_order_detail(order_id=SO00000)"),
        default_row(text="全部订单已汇总完毕。"),
    ]
    drv = FakeDriver(responses=responses)
    wl = get_workload("tool_call")
    spec = wl.generate({"steps": 3})
    result = wl.run(drv, spec)
    rows = result["rows"]
    assert len(rows) == 3
    assert rows[0]["action"] == "search_orders"
    assert rows[1]["action"] == "get_order_detail"
    assert rows[2]["action"] is None          # 无 ACTION -> 模型给出最终答案
    assert "step" in rows[0]
    ev = wl.evaluate(result, spec)
    assert ev["task_success"] is True
    assert ev["tool_calls"] == 2


def test_tool_call_stops_early(fake_driver):
    """模型第一轮就给出最终答案（无 ACTION）时立即停止。"""
    wl = get_workload("tool_call")
    spec = wl.generate({"steps": 6})
    result = wl.run(fake_driver, spec)
    assert len(result["rows"]) == 1
    ev = wl.evaluate(result, spec)
    assert ev["task_success"] is False
    assert ev["tool_calls"] == 0


def test_branch_structure(fake_driver):
    wl = get_workload("branch")
    spec = wl.generate({"branch_rounds": 2})
    result = wl.run(fake_driver, spec)
    data = result["rows"]
    assert set(data) == {"common", "branches"}
    assert set(data["branches"]) == {"A", "B"}
    assert len(data["branches"]["A"]) == 2
    assert len(data["branches"]["B"]) == 2
    # 行结构：sub_round + chat 键
    assert "sub_round" in data["branches"]["A"][0]
    # rows_for_summary：A + B + common（与旧脚本拼接一致）
    flat = wl.rows_for_summary(result)
    assert len(flat) == 2 + 2 + 1
    ev = wl.evaluate(result, spec)
    assert ev["task_success"] is True


def test_long_life_structure(fake_driver):
    wl = get_workload("long_life")
    spec = wl.generate({"rounds": 8, "secret": "9527", "ctx_size": 2048})
    result = wl.run(fake_driver, spec)
    rows = result["rows"]
    assert len(rows) == 8
    assert "round" in rows[0]
    meta = result["meta"]
    assert "task_success" in meta
    assert "truncations" in meta
    # 默认响应不含秘密数字 -> task_success False（evaluate 行为验证）
    ev = wl.evaluate(result, spec)
    assert ev["task_success"] is False
    assert ev["secret_recall"] == 0.0


def test_long_life_secret_recall_success():
    wl = get_workload("long_life")
    spec = wl.generate({"rounds": 5, "secret": "9527", "ctx_size": 2048})
    result = {"rows": [], "meta": {"task_success": True, "truncations": 1}}
    ev = wl.evaluate(result, spec)
    # E0.6：state_retention_rate 为主指标；task_success 保留作保真约束
    assert ev["state_retention_rate"] == 1.0
    assert ev["task_success"] is True
    assert ev["secret_recall"] == 1.0
    assert ev["truncations"] == 1


def test_long_life_state_retention_failure():
    wl = get_workload("long_life")
    spec = wl.generate({"rounds": 5, "secret": "9527", "ctx_size": 2048})
    result = {"rows": [], "meta": {"task_success": False, "truncations": 0}}
    ev = wl.evaluate(result, spec)
    assert ev["state_retention_rate"] == 0.0
    assert ev["task_success"] is False


def test_spec_fingerprint_deterministic():
    wl = get_workload("multi_turn")
    s1 = wl.generate({"rounds": 20})
    s2 = wl.generate({"rounds": 20})
    assert s1.fingerprint() == s2.fingerprint()
    s3 = wl.generate({"rounds": 21})
    assert s1.fingerprint() != s3.fingerprint()


def test_registry_has_all_scenarios():
    assert set(get_workload(n).name for n in
               ["multi_turn", "tool_call", "branch", "long_life"]) == {
        "multi_turn", "tool_call", "branch", "long_life"}
