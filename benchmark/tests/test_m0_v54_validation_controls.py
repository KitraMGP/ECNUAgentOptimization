"""M0 v54 测试：decision-validation 双 control（off/on 各一 session）。

覆盖：
- run_decision_validation 两次 session 分别用 control off/on（VALIDATION_CONTROLS）
- session 记录含 control 字段且顺序为 ["off", "on"]
- validate_decision_session 接受含 control 的 session（非严格白名单，不报错）
"""
from __future__ import annotations

import pytest

from runner import m0_fanout_runner as m0r
from runner import m0_schema as sch
from runner.m0_fanout_runner import M0FanoutRunner


class _Rec(dict):
    def __init__(self, ok: bool, error=None, text: str = "x", finish_reason: str = "stop"):
        super().__init__(ok=ok, error=error, text=text, finish_reason=finish_reason)


def test_validation_controls_constant():
    """VALIDATION_CONTROLS 固定为 ("off", "on") 且与 SESSIONS 等长。"""
    assert m0r.VALIDATION_CONTROLS == ("off", "on")
    assert len(m0r.VALIDATION_CONTROLS) == m0r.VALIDATION_SESSIONS == 2


def test_session_from_recs_records_control():
    """_session_from_recs 记录 control 字段且不影响计数不变量。"""
    recs = [_Rec(True)] * 10
    for ctl in ("off", "on"):
        s = M0FanoutRunner._session_from_recs(recs, ctl)
        assert s["control"] == ctl
        # 不变量仍成立
        assert s["requests"] == s["valid"] + s["invalid"] + s["error_count"] == 10
        assert s["invalid"] == s["invalid_length"] + s["invalid_no_action"] == 0
        assert len(s["output_hashes"]) == s["valid"] + s["invalid"]


def test_run_decision_validation_uses_both_controls(monkeypatch):
    """run_decision_validation 两次 session 分别 off/on，session 顺序匹配。"""
    started: list[str] = []
    r = M0FanoutRunner(server_bin="bin", model="m", tmp_dir="/tmp/m0_v54_test")

    class FakeAdapter:
        port = 9999

    calls = {"n": 0}

    def fake_start_server(parallel, ctk, ctv, control, tag):
        calls["n"] += 1
        started.append(control)
        return FakeAdapter()

    def fake_run_session(port):
        return [_Rec(True)] * 10

    def fake_stop():
        pass

    monkeypatch.setattr(r, "_start_server", fake_start_server)
    monkeypatch.setattr(r, "_run_decision_session", fake_run_session)
    monkeypatch.setattr(r, "_stop_server", fake_stop)

    complete, sessions = r.run_decision_validation()
    assert complete is True
    assert started == ["off", "on"]
    assert [s["control"] for s in sessions] == ["off", "on"]
    assert all(s["requests"] >= 10 and s["error_count"] == 0 for s in sessions)


def test_run_decision_validation_partial_on_second_failure(monkeypatch):
    """第二 session 启动失败 → partial（只有 control off 的 session）。"""
    r = M0FanoutRunner(server_bin="bin", model="m", tmp_dir="/tmp/m0_v54_test")

    class FakeAdapter:
        port = 9999

    def fake_start_server(parallel, ctk, ctv, control, tag):
        if control == "on":
            raise m0r.ServerError("start failed")
        return FakeAdapter()

    def fake_run_session(port):
        return [_Rec(True)] * 10

    def fake_stop():
        pass

    monkeypatch.setattr(r, "_start_server", fake_start_server)
    monkeypatch.setattr(r, "_run_decision_session", fake_run_session)
    monkeypatch.setattr(r, "_stop_server", fake_stop)

    complete, sessions = r.run_decision_validation()
    assert complete is False
    assert len(sessions) == 1
    assert sessions[0]["control"] == "off"


def test_validate_decision_session_accepts_control_key():
    """validator 对含 control 的 session 不报错（control 为可选诊断字段）。"""
    base = {
        "requests": 10, "valid": 10, "invalid": 0, "error_count": 0,
        "invalid_length": 0, "invalid_no_action": 0,
        "finish_reasons": {"stop": 10},
        "output_hashes": ["a"] * 10, "error_summary": [],
        # v57：v56+ 格式 valid>0 必须非空代表输出（v56 的"同空允许"仅 valid=0
        # 场景成立；valid>0 时测试数据必须带非空 ACTION 文本）
        "representative_output": "ACTION: branch(b1)",
        "representative_output_sha256": "03c79f2833c2f6fd56d38d750104c23bafee2b024e6c59f0c580d03b5414606b",
    }
    for ctl in ("off", "on"):
        s = dict(base, control=ctl)
        errs = sch.validate_decision_session(s)
        assert errs == [], f"control={ctl} 不应产生错误: {errs}"
