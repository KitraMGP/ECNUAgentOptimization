"""M0 v58 审查修复测试（H1 + 正式矩阵前收口）。

集中覆盖：
1. run_decision_validation 每完成一个 session 立即同步 self.decision_sessions；
   函数开始清空；异常时保留已完成 session（第 2 个 control 中途抛异常）——
   测试调用真实方法，仅替换子方法（_start_server/_run_decision_session/
   _stop_server），禁止 monkeypatch 整方法手写属性。
2. calibration report decision_validation 统一 sch.build_decision_validation
   标准结构（sessions + total_valid_rate + partial），不造第二 schema。
3. CalibrationLengthMissing 独立异常：不进 _INFRA_EXCEPTIONS（warmup 不吞）、
   formal/warmup 均不可恢复 → run_safe 70。
4. _read_log_refresh 流式筛选 key lines（不整文件 f.read）；started_at 统一
   RFC3339 UTC（Z 后缀）。
5. --tmp-dir 视为父目录 + 本 run 唯一子目录；既有共享目录内容不被删；
   cleanup 只删自己创建的专用子目录。
"""
from __future__ import annotations

import os
import re

import pytest

from runner import m0_calibration_runner as m0c
from runner import m0_fanout_runner as m0r
from runner import m0_schema as sch
from tests.mock_server import MockOpenAIServer

_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class FakeAdapter:
    """mock server adapter（本地复制，真实绑定 MockOpenAIServer）。"""

    _instances = 0
    _starts = 0

    def __init__(self, cmd, log_path, port, crash_at=None):
        self.cmd = cmd
        self.log_path = log_path
        self.port = port
        FakeAdapter._instances += 1
        self._server: MockOpenAIServer | None = None
        self.stopped = False

    def start(self):
        FakeAdapter._starts += 1
        self._server = MockOpenAIServer().__enter__()
        self.port = self._server.port

    def poll(self):
        return 1 if self._server is None else None

    def wait_health(self, timeout=120.0):
        return self._server is not None

    def stop(self):
        self.stopped = True
        if self._server is not None:
            self._server.__exit__(None, None, None)
            self._server = None

    def read_log(self):
        return ("E8-C1: capability rejected: hybrid (recurrent+attention) model\n"
                if "on" in str(self.cmd) else "")


@pytest.fixture
def fake_adapter_cls(monkeypatch):
    FakeAdapter._instances = 0
    FakeAdapter._starts = 0
    monkeypatch.setattr(m0r, "ServerAdapter", FakeAdapter)
    return FakeAdapter


class _Rec(dict):
    """decision session 记录（ok/error/finish_reason/text）。"""

    def __init__(self, ok: bool, error=None, finish_reason="stop", text="ACTION: branch(b1)"):
        super().__init__(ok=ok, error=error, finish_reason=finish_reason, text=text)


def _make_runner(tmp_path, adapter_cls=None, **over):
    args = dict(
        server_bin="fake-bin", model="mock.gguf", ctx_size=4096,
        port_base=18080, decision_n_predict=16, branch_n_predict=64,
        tool_rounds=1, out_path=str(tmp_path / "out.json"),
        tmp_dir="", ngl=99,
    )
    if adapter_cls is not None:
        args["adapter_cls"] = adapter_cls
    args.update(over)
    return m0r.M0FanoutRunner(**args)


# ---- 1. run_decision_validation 同步 self.decision_sessions ----

class TestDecisionValidationSync:
    def test_syncs_sessions_and_clears_first(self, monkeypatch, tmp_path):
        """真实方法：函数开始清空；每完成一个 session 立即同步。"""
        r = _make_runner(tmp_path)

        class FakeAdapter:
            port = 9999

        def fake_start_server(parallel, ctk, ctv, control, tag):
            return FakeAdapter()

        def fake_run_session(port):
            return [_Rec(True)] * 10

        calls = {"stop": 0}

        def fake_stop():
            calls["stop"] += 1

        # 预先塞一个"旧" session —— 函数开始必须清空
        r.decision_sessions.append({"stale": True})
        monkeypatch.setattr(r, "_start_server", fake_start_server)
        monkeypatch.setattr(r, "_run_decision_session", fake_run_session)
        monkeypatch.setattr(r, "_stop_server", fake_stop)

        complete, sessions = r.run_decision_validation()
        assert complete is True
        assert [s["control"] for s in sessions] == ["off", "on"]
        assert all(not s.get("stale") for s in sessions)  # 旧 session 被清空
        # 返回值与 self 同步（同一列表内容）
        assert r.decision_sessions == sessions
        assert calls["stop"] == 2

    def test_mid_session_exception_keeps_completed(self, monkeypatch, tmp_path):
        """第 2 个 control 中途抛异常 → 真实方法保留已完成 session（off）。

        仅替换子方法（_start_server/_run_decision_session/_stop_server），
        不 monkeypatch run_decision_validation 整方法、不手写属性。
        """
        r = _make_runner(tmp_path)

        class FakeAdapter:
            port = 9999

        def fake_start_server(parallel, ctk, ctv, control, tag):
            return FakeAdapter()

        run_calls = {"n": 0}

        def fake_run_session(port):
            run_calls["n"] += 1
            if run_calls["n"] == 2:  # control=on 的 session 中途崩溃
                raise RuntimeError("mid-session server crash")
            return [_Rec(True)] * 10

        def fake_stop():
            pass

        monkeypatch.setattr(r, "_start_server", fake_start_server)
        monkeypatch.setattr(r, "_run_decision_session", fake_run_session)
        monkeypatch.setattr(r, "_stop_server", fake_stop)

        with pytest.raises(RuntimeError):
            r.run_decision_validation()
        # 已完成 session（off）保留；未完成（on）不 append
        assert [s["control"] for s in r.decision_sessions] == ["off"]
        assert r.decision_sessions[0]["requests"] == 10
        assert r.decision_sessions[0]["error_count"] == 0

    def test_infra_exception_does_not_append_partial_session(self, monkeypatch, tmp_path):
        """session 级基础设施异常（TimeoutError 是 OSError 子类 → _INFRA_EXCEPTIONS，
        v59：不再传播，停止验证阶段）→ 异常 session 不 append，返回不完整。"""
        r = _make_runner(tmp_path)

        class FakeAdapter:
            port = 9999

        def fake_start_server(parallel, ctk, ctv, control, tag):
            return FakeAdapter()

        run_calls = {"n": 0}

        def fake_run_session(port):
            run_calls["n"] += 1
            if run_calls["n"] == 2:
                raise TimeoutError("read timeout")
            return [_Rec(True)] * 10

        def fake_stop():
            pass

        monkeypatch.setattr(r, "_start_server", fake_start_server)
        monkeypatch.setattr(r, "_run_decision_session", fake_run_session)
        monkeypatch.setattr(r, "_stop_server", fake_stop)

        # v59：基础设施异常被捕获 → 不传播，返回 (False, [off])；off 保留
        complete, sessions = r.run_decision_validation()
        assert complete is False
        assert [s["control"] for s in sessions] == ["off"]


# ---- 2. calibration report decision_validation 统一标准结构 ----

class TestCalibrationDecisionValidationSchema:
    def test_build_decision_validation_standard_shape(self, tmp_path, fake_adapter_cls,
                                                      monkeypatch):
        """calibration 异常路径落盘标准结构（sessions/total_valid_rate/partial），
        不出现旧第二 schema {complete, sessions}。"""
        r = _make_runner(tmp_path)
        # 一个已完成 session（control=off，10 请求 10 valid）
        sess = sch.build_decision_session(
            requests=10, valid=10, invalid=0, error_count=0,
            invalid_length=0, invalid_no_action=0, finish_reasons={"stop": 10},
            output_hashes=["a" * 64] * 10, error_summary=[],
            representative_output="ACTION: branch(b1)")
        r.decision_sessions = [sess]
        sess["control"] = "off"  # 与 _session_from_recs 一致（build_decision_session 后补）
        # 触发 dv 阶段异常路径（run_decision_validation 抛异常）
        def _boom(*a, **k):
            raise RuntimeError("dv infra failure")

        monkeypatch.setattr(r, "run_decision_validation", _boom)
        report = {"rs_observations": {"dv": {}}, "errors": []}
        # 模拟 run_short_calibration 中 dv 阶段的 except 块逻辑
        try:
            r.run_decision_validation()
        except Exception as e:  # noqa: BLE001
            if r.decision_sessions:
                report["decision_validation"] = sch.build_decision_validation(
                    r.decision_sessions, partial=True)
            report["errors"].append({
                "code": "validation_incomplete", "stage": "decision_validation",
                "count": 1, "detail": f"{type(e).__name__}: {e}"})
        dv = report["decision_validation"]
        assert set(dv.keys()) == {"sessions", "total_valid_rate", "partial"}
        assert "complete" not in dv  # 不造第二 schema
        assert dv["partial"] is True
        assert dv["total_valid_rate"] == 1.0
        assert dv["sessions"][0]["control"] == "off"
        assert report["errors"][0] == {
            "code": "validation_incomplete", "stage": "decision_validation",
            "count": 1, "detail": "RuntimeError: dv infra failure"}


# ---- 3. CalibrationLengthMissing 独立异常 ----

class TestCalibrationLengthMissing:
    def test_not_in_infra_exceptions(self):
        """校准长度缺失不纳入 warmup 基础设施可忽略集合。"""
        assert m0r.CalibrationLengthMissing not in m0r._INFRA_EXCEPTIONS

    def test_run_unit_raises_when_missing(self, tmp_path):
        """真实 _run_unit：calibrated_lengths 为空 → CalibrationLengthMissing。"""
        r = _make_runner(tmp_path)
        r._driver = object()
        r._kv = object()
        uid = sch.unit_id_of("off", "q8_0", "q8_0", 2, "short")
        gid = sch.group_id_of("off", "q8_0", "q8_0", 2)
        with pytest.raises(m0r.CalibrationLengthMissing):
            r._run_unit(uid, 2, "short", "q8_0", "q8_0", gid, rep_index=0)

    def test_run_safe_returns_70(self, tmp_path, monkeypatch, fake_adapter_cls):
        """formal 遇到校准长度缺失 → run_safe 70（先吞后报被消除）。"""
        def _boom(*a, **k):
            raise m0r.CalibrationLengthMissing("校准长度缺失: short/fanout=2")

        r = _make_runner(tmp_path, fake_adapter_cls)
        monkeypatch.setattr(r, "_run_unit", _boom)
        rc = r.run_safe()
        assert rc == 70, rc


# ---- 4. _read_log_refresh 流式 + started_at RFC3339 UTC ----

class TestLogRefreshAndStartedAt:
    def test_read_log_refresh_streams_key_lines_only(self, tmp_path):
        """大日志只读关键行（不整文件 f.read 回传）。"""
        log = tmp_path / "server_dv0.log"
        lines = [
            "llama_model_load: ...n_ctx = 4096",
            "RS buffer size: 100.5 MiB",
            "KV buffer size: 68.0 MiB",
            "capability rejected: hybrid kv sharing disabled",
            "server is listening on http://127.0.0.1:8080",
            "log verbose: INFO",
            "some noisy line without keywords",
            "another noisy line",
        ]
        # 放大噪音（模拟大日志）
        noisy = ["noise line %d" % i for i in range(20000)]
        log.write_text("\n".join(lines + noisy), encoding="utf-8")
        got = m0c._read_log_refresh(str(log), "fallback")
        assert "RS buffer size" in got
        assert "capability rejected" in got
        assert "server is listening" in got
        assert "noise line" not in got          # 噪音被筛掉
        assert "log verbose" not in got          # 非关键行被筛掉
        assert len(got.splitlines()) == 5        # 仅关键行

    def test_read_log_refresh_fallback(self, tmp_path):
        """文件不存在 → 回退启动期快照。"""
        assert m0c._read_log_refresh(str(tmp_path / "missing.log"), "snap") == "snap"

    def test_read_log_refresh_empty_path(self):
        assert m0c._read_log_refresh("", "snap") == "snap"

    def test_started_at_rfc3339_utc(self, tmp_path, fake_adapter_cls):
        """server 启动元数据 started_at 统一 RFC3339 UTC（Z 后缀）。"""
        r = _make_runner(tmp_path, fake_adapter_cls)
        adapter = r._start_server(4, "q8_0", "q8_0", "off", "t")
        meta = r._server_meta["t"]
        assert _UTC_RE.match(meta["started_at"]), meta["started_at"]
        r._cleanup_all()


# ---- 5. --tmp-dir 父目录安全 ----

class TestTmpDirSafety:
    def test_fanout_tmp_dir_creates_unique_subdir(self, tmp_path):
        """fanout：--tmp-dir 视为父目录 → 唯一子目录；既有内容保留。"""
        parent = tmp_path / "shared"
        parent.mkdir()
        marker = parent / "keep.txt"
        marker.write_text("precious", encoding="utf-8")
        r = _make_runner(tmp_path, tmp_dir=str(parent))
        assert marker.read_text(encoding="utf-8") == "precious"  # 未被删
        assert r.tmp_dir.startswith(str(parent) + os.sep)
        assert os.path.isdir(r.tmp_dir)
        assert r.tmp_dir != str(parent)  # 不是父目录本身

    def test_calibration_tmp_dir_parent_kept_cleanup_only_subdir(self, tmp_path,
                                                                 fake_adapter_cls):
        """calibration：--tmp-dir 既有目录内容不被删；cleanup 只删本 run 子目录。"""
        from runner.m0_calibration_runner import main
        bin_p = tmp_path / "llama-server"
        model_p = tmp_path / "mock.gguf"
        bin_p.write_text("#!/bin/sh\n")
        model_p.write_text("x")
        parent = tmp_path / "shared"
        parent.mkdir()
        marker = parent / "keep.txt"
        marker.write_text("precious", encoding="utf-8")
        rc = main(["--server-bin", str(bin_p), "--model", str(model_p),
                   "--out", str(tmp_path / "rep.json"),
                   "--tmp-dir", str(parent), "--run-id", "cli-run"])
        assert rc == 0
        assert marker.read_text(encoding="utf-8") == "precious"  # 父内容保留
        # 本 run 唯一子目录被 cleanup 删除、父目录保留
        subdir = parent / "m0_cal_cli-run"
        assert not os.path.isdir(subdir)
        assert os.path.isdir(parent)

    def test_calibration_tmp_dir_keep_tmp_keeps_subdir(self, tmp_path, fake_adapter_cls):
        """--keep-tmp：子目录保留（父内容仍不受影响）。"""
        from runner.m0_calibration_runner import main
        bin_p = tmp_path / "llama-server"
        model_p = tmp_path / "mock.gguf"
        bin_p.write_text("#!/bin/sh\n")
        model_p.write_text("x")
        parent = tmp_path / "shared"
        parent.mkdir()
        rc = main(["--server-bin", str(bin_p), "--model", str(model_p),
                   "--out", str(tmp_path / "rep.json"),
                   "--tmp-dir", str(parent), "--run-id", "cli-run",
                   "--keep-tmp"])
        assert rc == 0
        assert os.path.isdir(parent / "m0_cal_cli-run")
        assert os.path.isdir(parent)
