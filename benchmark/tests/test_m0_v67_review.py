"""M0 v67 review 收口测试（dv ServerCrash 显式捕获 + 停止后续 session）。

覆盖（任务 1）：
- `_run_decision_session` 显式捕获 ServerCrash：当前请求计入
  requests/error_count/error_summary code=server_crash，立即 break 不再
  执行剩余请求（server 已死，剩余必败）；
- `run_decision_validation` 依 requests<target 停止后续 session（崩溃后
  不再启动新 server）；partial session 保留证据 → 上层构造 partial dv →
  合法 PREFLIGHT_INFRA 落盘（rc=0、validator 通过、formal 未启动）；
- 首 session 崩溃 e2e（正）：dv0 崩溃 → 仅 1 个 partial session、后续
  session（dv1）未启动；
- 第二 session 崩溃 e2e（正）：dv1 崩溃 → session 0 完整（requests=10、
  error_count=0）+ session 1 partial（含 server_crash）、无后续 session。
"""
from __future__ import annotations

import json

import pytest

from runner import m0_fanout_runner as m0r
from runner import m0_schema as sch
from tests import mock_server as mserver


class FakeAdapter:
    """与 test_m0_runner_fixes 同构的 mock adapter（独立副本，避免跨文件耦合）。

    crash_at_after：第 N 个实例起 crash_at=crash_at_value（adapter 实例顺序
    = 校准 calib(1) → dv0(2) → dv1(3)，run() 全链路下 dv0=2、dv1=3）。
    """

    crash_at_after = None
    crash_at_value = 0  # dv 首个 chat 即崩溃（httpd.count=1 > 0）
    poll_dead = None
    _instances = 0

    def __init__(self, cmd, log_path, port, crash_at=None):
        self.cmd = cmd
        self.log_path = log_path
        self.port = port
        FakeAdapter._instances += 1
        if (FakeAdapter.crash_at_after is not None
                and FakeAdapter._instances >= FakeAdapter.crash_at_after):
            crash_at = FakeAdapter.crash_at_value
        self.crash_at = crash_at
        self._server: mserver.MockOpenAIServer | None = None

    def start(self):
        self._server = mserver.MockOpenAIServer(crash_at=self.crash_at).__enter__()
        self.port = self._server.port

    def poll(self):
        if FakeAdapter.poll_dead:
            return 1
        if self.crash_at is not None and self._server is not None \
                and self._server.httpd is not None \
                and self._server.httpd.count > self.crash_at:
            return 1
        if self._server is None:
            return 1
        return None

    def wait_health(self, timeout=120.0):
        return self._server is not None

    def stop(self):
        if self._server is not None:
            self._server.__exit__(None, None, None)
            self._server = None
            return (True, "ok")

    def read_log(self):
        return ""


@pytest.fixture
def fake_adapter_cls(monkeypatch):
    FakeAdapter.crash_at_after = None
    FakeAdapter.crash_at_value = 0
    FakeAdapter.poll_dead = None
    FakeAdapter._instances = 0
    monkeypatch.setattr(m0r, "ServerAdapter", FakeAdapter)
    return FakeAdapter


def make_runner(tmp_path, fake_adapter_cls, **over):
    args = dict(
        server_bin="fake-bin", model="mock.gguf", ctx_size=4096,
        port_base=18080, decision_n_predict=16, branch_n_predict=64,
        tool_rounds=1, out_path=str(tmp_path / "out.json"),
        tmp_dir=str(tmp_path / "tmp"), ngl=99, adapter_cls=fake_adapter_cls,
    )
    args.update(over)
    return m0r.M0FanoutRunner(**args)


def _load_doc(tmp_path):
    with open(str(tmp_path / "out.json"), encoding="utf-8") as f:
        return json.load(f)


class TestDvServerCrash:
    def test_first_session_crash_preflight(self, tmp_path, fake_adapter_cls):
        """正 e2e：dv0（首个验证 session）崩溃 → 当前请求计入 server_crash、
        立即 break、session partial（requests<10）→ 停止后续 session（dv1 未
        启动）→ 合法 PREFLIGHT_INFRA 落盘（rc=0、validator 通过、formal 未启动）。"""
        fake_adapter_cls.crash_at_after = 2  # 实例 1=calib 正常、实例 2=dv0 崩溃
        r = make_runner(tmp_path, fake_adapter_cls)
        rc = r.run()
        assert rc == 0, rc
        doc = _load_doc(tmp_path)
        ok, errs = sch.validate_result_envelope(doc)
        assert ok, errs
        meta = doc["meta"]
        assert meta["phase"] == "preflight"
        assert meta["preflight_status"] == "FAILED"
        assert meta["preflight_reason"] == "validation_incomplete"
        dv = meta["decision_validation"]
        assert dv["partial"] is True
        # 首 session 崩溃：只保留 1 个 partial session（dv1 未启动）
        assert len(dv["sessions"]) == 1
        s0 = dv["sessions"][0]
        assert s0["requests"] < m0r.VALIDATION_REQUESTS
        assert s0["error_count"] == 1
        assert any(e["code"] == "server_crash" for e in s0["error_summary"])
        # 崩溃请求计入后立即 break：剩余请求不执行
        assert s0["requests"] == 1
        # formal 未启动
        assert doc["modes"] == {} and doc["gates"] == {}
        assert meta["executed_units"] == 0
        assert meta["matrix_complete"] is False

    def test_second_session_crash_preflight(self, tmp_path, fake_adapter_cls):
        """正 e2e：dv1（第二验证 session）崩溃 → session 0 完整保留（requests=10、
        error_count=0）+ session 1 partial（含 server_crash）→ 停止后续（无 session 2）
        → 合法 PREFLIGHT_INFRA 落盘。"""
        fake_adapter_cls.crash_at_after = 3  # 实例 1=calib、2=dv0 正常、3=dv1 崩溃
        r = make_runner(tmp_path, fake_adapter_cls)
        rc = r.run()
        assert rc == 0, rc
        doc = _load_doc(tmp_path)
        ok, errs = sch.validate_result_envelope(doc)
        assert ok, errs
        meta = doc["meta"]
        assert meta["phase"] == "preflight"
        assert meta["preflight_reason"] == "validation_incomplete"
        dv = meta["decision_validation"]
        assert dv["partial"] is True
        assert len(dv["sessions"]) == 2
        assert [s["control"] for s in dv["sessions"]] == ["off", "on"]
        # session 0 完整（dv0 正常跑完 10 条）
        assert dv["sessions"][0]["requests"] == m0r.VALIDATION_REQUESTS
        assert dv["sessions"][0]["error_count"] == 0
        # session 1 崩溃 partial（当前请求计入 server_crash 后 break）
        s1 = dv["sessions"][1]
        assert s1["requests"] < m0r.VALIDATION_REQUESTS
        assert s1["error_count"] == 1
        assert any(e["code"] == "server_crash" for e in s1["error_summary"])
        assert s1["requests"] == 1
        # formal 未启动
        assert doc["modes"] == {} and doc["gates"] == {}
        assert meta["executed_units"] == 0

    def test_no_crash_completes_validation(self, tmp_path, fake_adapter_cls):
        """反 e2e：无崩溃 → 两 session 均完整（requests=10、error_count=0）→
        dv complete（此测试只跑到 dv 阶段即 full run 继续 formal 太重，
        用 run_decision_validation 级断言 complete=True）。"""
        r = make_runner(tmp_path, fake_adapter_cls)
        complete, sessions = r.run_decision_validation()
        assert complete is True
        assert len(sessions) == 2
        assert all(s["requests"] == m0r.VALIDATION_REQUESTS for s in sessions)
        assert all(s["error_count"] == 0 for s in sessions)
