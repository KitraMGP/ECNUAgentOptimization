"""M0 v59 审查 H1 + formal 前收口测试（8 项任务）：

1. calibration runner 日志路径统一 r.tmp_dir（M0FanoutRunner 专属子目录）——
   rs_observations / probe_p10 非空且 tag/pid 正确（真实日志文件验证）。
2. formal run() decision-validation 阶段基础设施/请求异常 → 合法 preflight
   落盘（decision_validation 标准结构 partial=True，保留已完成 session）；
   AssertionError/KeyError 等内部 bug 仍 70（传播）。
3. calibration report date 统一 sch.now_utc()（RFC3339 UTC Z）。
4. _stop_server 在 finally 异常不掩盖 primary；stop 错误记诊断；
   仅无 primary 时按基础设施处理。
5. 日志筛选 pattern 收敛单一常量（LOG_KEY_LINE_KEYS / LOG_RS_BUFFER /
   LOG_CAPABILITY / LOG_KV_BUFFER）。
6. cleanup_stale_dirs：只删陈旧 m0_fanout_/m0_cal_ 一级子目录，不删活跃/父目录。

全部不依赖 GPU/真实 llama-server。
"""
from __future__ import annotations

import json
import os
import re
import time

import openai
import pytest

from runner import m0_calibration_runner as cr
from runner import m0_fanout_runner as m0r
from runner import m0_schema as sch
from runner.m0_calibration_runner import run_short_calibration
from tests.mock_server import MockOpenAIServer

_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class LogWritingFakeAdapter:
    """写真实日志文件到 log_path（含 RS/KV/capability 行）的 mock adapter。

    用于验证校准 runner 从 r.tmp_dir 专属子目录（而非外层 tmp_dir 父目录）
    读取 rs_observations / probe_p10。
    """

    _instances = 0
    _starts = 0

    def __init__(self, cmd, log_path, port, crash_at=None):
        self.cmd = cmd
        self.log_path = log_path
        self.port = port
        LogWritingFakeAdapter._instances += 1
        self._server = None

    def start(self):
        LogWritingFakeAdapter._starts += 1
        # 真实启动路径语义：日志文件写入传入的 log_path（r.tmp_dir 下）
        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
        with open(self.log_path, "w", encoding="utf-8") as f:
            f.write(
                "llama_memory_recurrent::init: RS buffer size = 502.50 MiB\n"
                "KV buffer size = 128 MiB\n"
                "E8-C1: capability rejected: hybrid (recurrent+attention) model\n")
        self._server = MockOpenAIServer().__enter__()
        self.port = self._server.port

    def poll(self):
        return None

    def wait_health(self, timeout=120.0):
        return self._server is not None

    def stop(self):
        if self._server is not None:
            self._server.__exit__(None, None, None)
            self._server = None
            return (True, "ok")  # v61 契约：tuple/bool

    def read_log(self):
        try:
            with open(self.log_path, encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""


class StopFailingFakeAdapter(LogWritingFakeAdapter):
    """stop() 抛异常（模拟清理阶段基础设施失败）的 mock adapter。"""

    def stop(self):
        raise RuntimeError("stop boom")


def _cal_args(tmp_path, **over):
    args = dict(
        server_bin="fake-bin", model="mock.gguf", ctx_size=4096, ngl=99,
        tmp_dir=str(tmp_path / "tmp"), run_id="v59-test",
        decision_n_predict=16, do_probe=True,
    )
    args.update(over)
    return args


class _FakeDrv:
    """fake driver：chat 返回合法决策行；可配置第 n 次调用起连续抛 openai 异常。

    v66：transient wrapper 会重试 connection error——fail_at 起连续抛
    TRANSIENT_MAX_ATTEMPTS=3 次（retry 耗尽）才让异常到达调用方
    （dv 请求级逐条捕获语义保持：error_count=1 对应一次逻辑请求）。"""

    def __init__(self, fail_at: int | None = None):
        self.n = 0
        self.fail_at = fail_at

    def chat(self, messages, **kw):
        self.n += 1
        if self.fail_at is not None \
                and self.fail_at <= self.n < self.fail_at + m0r.TRANSIENT_MAX_ATTEMPTS:
            raise openai.APIConnectionError(request=None)
        return {"text": "ACTION: branch(b1)", "finish_reason": "stop"}


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


# ---- 1. calibration 日志路径统一 r.tmp_dir（任务 1） ----

class TestCalibrationLogPath:
    def test_start_server_writes_log_into_runner_tmp_dir(self, tmp_path):
        """_start_server 的日志落在 M0FanoutRunner 专属子目录（r.tmp_dir）。"""
        r = _make_runner(tmp_path, adapter_cls=LogWritingFakeAdapter,
                         tmp_dir=str(tmp_path / "parent"))
        adapter = r._start_server(parallel=4, ctk="q8_0", ctv="q8_0",
                                  control="off", tag="calib")
        try:
            assert os.path.dirname(adapter.log_path) == r.tmp_dir
            assert os.path.basename(adapter.log_path) == "server_calib.log"
            # 专属子目录在父目录下、父目录本身无 server 日志
            assert os.path.dirname(r.tmp_dir) == str(tmp_path / "parent")
            assert not os.path.exists(os.path.join(str(tmp_path / "parent"),
                                                   "server_calib.log"))
            log = open(adapter.log_path, encoding="utf-8").read()
            assert "RS buffer size = 502.50 MiB" in log
            assert "capability rejected" in log
            # _server_meta 记录真实 log_path（tag/pid 正确）
            assert r._server_meta["calib"]["log_path"] == adapter.log_path
        finally:
            r._stop_server()

    def test_rs_observations_and_probe_from_runner_tmp_dir(self, tmp_path, monkeypatch):
        """rs_observations / probe_p10 / server_logs_summary 从 r.tmp_dir 子目录
        读到真实日志（旧路径读外层父目录 → 空；本测试断言非空）。"""
        monkeypatch.setattr(m0r, "ServerAdapter", LogWritingFakeAdapter)
        rep = run_short_calibration(**_cal_args(tmp_path))
        # calibration 阶段：RS / capability 行非空
        assert rep["rs_observations"]["calib_p4"]
        assert any("RS buffer size" in ln
                   for ln in rep["rs_observations"]["calib_p4"])
        assert rep["rs_observations"]["calib_capability"]
        # dv 阶段：两 session 均非空
        assert rep["rs_observations"]["dv"]
        for key, lines in rep["rs_observations"]["dv"].items():
            assert lines, f"dv observation {key} 应为非空（r.tmp_dir 日志）"
        # p10 探针：RS / capability 行非空
        assert rep["probe_p10"]["rs_buffer_lines"]
        assert rep["probe_p10"]["capability_rejected_lines"]
        # summary：key_lines 非空（同样来自 r.tmp_dir）
        for entry in rep["server_logs_summary"]:
            assert entry["key_lines"]["rs_buffer"], (
                f"summary {entry['server_tag']} 应含 rs_buffer 行")
            assert entry["pid"] is not None or entry["pid"] is None
            assert entry["started_at"] is not None

    def test_report_date_is_utc_rfc3339(self, tmp_path, monkeypatch):
        """report.date 统一 sch.now_utc()（RFC3339 UTC Z，非 %z 本地时间）。"""
        monkeypatch.setattr(m0r, "ServerAdapter", LogWritingFakeAdapter)
        rep = run_short_calibration(**_cal_args(tmp_path))
        assert _UTC_RE.match(rep["date"]), rep["date"]


# ---- 2. formal run() dv 阶段 infra 异常 → partial dv 落盘（任务 2） ----

class TestFormalDvPartialPreflight:
    def _ready_runner(self, r):
        """run() 前置：parity 成功 + 完整 6 桶证据 + 无预算拒绝。"""
        r.parity_progress = {"completed": list(sch.PARITY_BUCKETS), "errors": []}
        r.binary_version = "test-version"
        import types
        r.run_calibration = types.MethodType(lambda self: True, r)
        r.run_budget_check = types.MethodType(lambda self: None, r)

    def test_dv_mid_request_infra_error_partial_preflight(self, tmp_path, monkeypatch):
        """第 2 control（on）session 中途 openai 连接异常（请求级，被逐条捕获）
        → run() 落盘合法 preflight，decision_validation 标准结构 partial=True、
        两 session 均保留（on 的 error_count=1）。"""
        r = _make_runner(tmp_path, adapter_cls=LogWritingFakeAdapter)
        self._ready_runner(r)
        # off 10 条正常 + on 第 2 条（累计第 12 次调用）抛 openai 连接异常
        drv = _FakeDrv(fail_at=12)
        monkeypatch.setattr(r, "_connect", lambda port: (drv, None))
        rc = r.run()
        assert rc == 0
        assert os.path.isfile(r.out_path)
        with open(r.out_path, encoding="utf-8") as f:
            doc = json.load(f)
        dv = doc["meta"]["decision_validation"]
        assert set(dv.keys()) == {"sessions", "total_valid_rate", "partial"}
        assert dv["partial"] is True
        assert len(dv["sessions"]) == 2
        assert [s["control"] for s in dv["sessions"]] == ["off", "on"]
        assert dv["sessions"][0]["error_count"] == 0
        assert dv["sessions"][1]["error_count"] == 1
        assert doc["meta"]["phase"] == "preflight"
        assert doc["modes"] == {} and doc["gates"] == {}
        assert doc["meta"]["executed_units"] == 0
        # parity 已完整通过后才进入 dv 阶段 → validation_incomplete 时 parity_ok=true
        assert doc["meta"]["parity_ok"] is True
        assert doc["meta"]["token_count_method"] == sch.TOKEN_COUNT_METHOD

    def test_dv_session_level_infra_error_partial_preflight(self, tmp_path, monkeypatch):
        """session 级基础设施异常（_run_decision_session 整体抛 openai 连接异常）
        → 停止验证阶段，已完成 off session 保留为 partial dv 落盘。"""
        r = _make_runner(tmp_path, adapter_cls=LogWritingFakeAdapter)
        self._ready_runner(r)
        calls = {"n": 0}

        def fake_run_session(port):
            calls["n"] += 1
            if calls["n"] == 2:
                raise openai.APIConnectionError(request=None)
            return [{"ok": True, "error": None, "text": "ACTION: branch(b1)",
                     "finish_reason": "stop"}] * 10

        def fake_connect(port):
            return (_FakeDrv(), None)

        monkeypatch.setattr(r, "_run_decision_session", fake_run_session)
        monkeypatch.setattr(r, "_connect", fake_connect)
        rc = r.run()
        assert rc == 0
        with open(r.out_path, encoding="utf-8") as f:
            doc = json.load(f)
        dv = doc["meta"]["decision_validation"]
        assert dv["partial"] is True
        assert len(dv["sessions"]) == 1
        assert dv["sessions"][0]["control"] == "off"

    def test_dv_internal_bug_still_exit70(self, tmp_path, monkeypatch):
        """内部 bug（KeyError，非基础设施）不得被吞 → run_safe 归 EXIT_SOFTWARE(70)。"""
        r = _make_runner(tmp_path, adapter_cls=LogWritingFakeAdapter)
        self._ready_runner(r)

        def fake_run_session(port):
            raise KeyError("internal bug")

        monkeypatch.setattr(r, "_run_decision_session", fake_run_session)
        assert r.run_safe() == sch.EXIT_SOFTWARE
        assert not os.path.isfile(r.out_path)  # 不落盘非法结果


# ---- 3. _stop_server finally 异常不掩盖 primary（任务 4） ----

class TestStopServerException:
    def test_stop_error_does_not_mask_primary(self, tmp_path, monkeypatch):
        """primary（RuntimeError）+ stop 抛异常 → primary 传播、stop 错误记诊断。"""
        r = _make_runner(tmp_path, adapter_cls=StopFailingFakeAdapter)

        def fake_run_session(port):
            raise RuntimeError("primary boom")

        monkeypatch.setattr(r, "_run_decision_session", fake_run_session)
        with pytest.raises(RuntimeError, match="primary boom"):
            r.run_decision_validation()
        # stop 错误被记录（不传播、不掩盖 primary）
        assert any("stop boom" in e for e in r._stop_errors)

    def test_stop_error_alone_breaks_session(self, tmp_path, monkeypatch):
        """无 primary 异常但 stop 失败 → 按基础设施处理（break，session 不完整）。"""
        r = _make_runner(tmp_path, adapter_cls=StopFailingFakeAdapter)
        monkeypatch.setattr(r, "_connect", lambda port: (_FakeDrv(), None))
        complete, sessions = r.run_decision_validation()
        assert complete is False
        assert sessions == []
        assert any("stop boom" in e for e in r._stop_errors)

    def test_stop_clean_no_errors(self, tmp_path, monkeypatch):
        """正常 stop → 无 stop 错误记录、验证完整通过。"""
        r = _make_runner(tmp_path, adapter_cls=LogWritingFakeAdapter)
        monkeypatch.setattr(r, "_connect", lambda port: (_FakeDrv(), None))
        complete, sessions = r.run_decision_validation()
        assert complete is True
        assert len(sessions) == 2
        assert r._stop_errors == []

    def test_stop_error_alone_formal_group_incomplete(self, tmp_path, monkeypatch):
        """formal group：try 块正常完成但 stop 失败 → FormalIncomplete（matrix 不完整）。"""
        r = _make_runner(tmp_path, adapter_cls=StopFailingFakeAdapter)
        import types

        def fake_run_unit(self, unit_id, fanout, bucket, ctk, ctv, gid, rep_index):
            self._last_prefix_len = 260
            self._last_branch_len = 344
            if rep_index is None:  # warmup：不产出 formal rep
                return {"unit_id": unit_id, "warmup": True}
            return sch.build_rep(
                unit_id, rep_index, fanout, bucket, ctk, ctv,
                self._last_prefix_len, self._last_branch_len, gid,
                "OK", False, {"kv": {}, "branches": []})

        monkeypatch.setattr(r, "_run_unit", types.MethodType(fake_run_unit, r))
        # warmup 与正式 rep 同走 _run_unit；KV 基线 mock（build_group 直接存放）
        monkeypatch.setattr(r, "_connect", lambda port: (_FakeDrv(), None))
        monkeypatch.setattr(r, "_capture_baseline",
                            lambda gid: {"kv": {"capacity_bytes": 71303168}})
        r.calibrated_lengths = {
            ("short", 2): {"prefix": 260, "branch": 344},
            ("medium", 2): {"prefix": 420, "branch": 504},
            ("long", 2): {"prefix": 551, "branch": 635},
            ("short", 4): {"prefix": 274, "branch": 359},
            ("medium", 4): {"prefix": 434, "branch": 519},
            ("short", 8): {"prefix": 302, "branch": 386},
        }
        r.parity_progress = {"completed": list(sch.PARITY_BUCKETS), "errors": []}
        with pytest.raises(m0r.FormalIncomplete):
            r.run_formal()
        assert r._stop_errors


# ---- 4. 日志筛选 pattern 单一常量（任务 5） ----

class TestLogPatternConstants:
    def test_key_line_keys_single_constant(self):
        assert cr.LOG_KEY_LINE_KEYS
        assert "rs buffer size" in cr.LOG_KEY_LINE_KEYS
        assert "capability rejected" in cr.LOG_KEY_LINE_KEYS
        assert "kv buffer size" in cr.LOG_KEY_LINE_KEYS

    def test_named_patterns_exist(self):
        assert cr.LOG_RS_BUFFER == "RS buffer size"
        assert cr.LOG_CAPABILITY == "capability rejected"
        assert cr.LOG_KV_BUFFER == "KV buffer size"


# ---- 5. cleanup_stale_dirs（任务 6） ----

class TestCleanupStaleDirs:
    def test_removes_only_stale_m0_prefix_dirs(self, tmp_path):
        d = tmp_path / "results"
        d.mkdir()
        stale_fanout = d / "m0_fanout_20260809_000000_1"
        stale_fanout.mkdir()
        (stale_fanout / "server_g0.log").write_text("x", encoding="utf-8")
        stale_cal = d / "m0_cal_old"
        stale_cal.mkdir()
        fresh = d / "m0_fanout_20260809_000000_2"
        fresh.mkdir()
        other = d / "user_shared_dir"
        other.mkdir()
        prefix_file = d / "m0_fanout_not_dir.txt"
        prefix_file.write_text("x", encoding="utf-8")
        old_t = time.time() - 7200.0
        for p in (stale_fanout, stale_cal):
            os.utime(p, (old_t, old_t))
        # 陈旧目录内文件同样设旧（递归最新 mtime 判定）
        os.utime(stale_fanout / "server_g0.log", (old_t, old_t))
        os.utime(fresh, (time.time() - 10.0, time.time() - 10.0))
        removed = sch.cleanup_stale_dirs(str(d), max_age_seconds=3600.0)
        assert set(removed) == {"m0_fanout_20260809_000000_1", "m0_cal_old"}
        assert not stale_fanout.exists() and not stale_cal.exists()
        assert fresh.exists() and other.exists() and prefix_file.exists()

    def test_no_removal_when_dir_missing(self, tmp_path):
        assert sch.cleanup_stale_dirs(str(tmp_path / "nope")) == []

    def test_active_dir_not_removed(self, tmp_path):
        d = tmp_path / "r"
        d.mkdir()
        active = d / "m0_fanout_active"
        active.mkdir()
        os.utime(active, (time.time() - 1.0, time.time() - 1.0))
        assert sch.cleanup_stale_dirs(str(d), max_age_seconds=3600.0) == []
        assert active.exists()

    def test_dir_with_fresh_log_file_not_removed(self, tmp_path):
        """目录 mtime 旧但内部日志文件 mtime 新（server 活跃写入）→ 不删。
        （目录 mtime 只随子项增删更新，不随日志内容写入更新。）"""
        d = tmp_path / "r"
        d.mkdir()
        run_dir = d / "m0_fanout_20260809_000000_9"
        run_dir.mkdir()
        log_f = run_dir / "server_g0.log"
        log_f.write_text("x", encoding="utf-8")
        old_t = time.time() - 7200.0
        os.utime(run_dir, (old_t, old_t))
        # 日志文件刚写（活跃写入中）→ 目录虽然 mtime 旧，整体不陈旧
        os.utime(log_f, (time.time() - 2.0, time.time() - 2.0))
        assert sch.cleanup_stale_dirs(str(d), max_age_seconds=3600.0) == []
        assert run_dir.exists() and log_f.exists()
        # 日志文件也陈旧后 → 删除
        os.utime(log_f, (old_t, old_t))
        assert sch.cleanup_stale_dirs(str(d), max_age_seconds=3600.0) == [
            "m0_fanout_20260809_000000_9"]
        assert not run_dir.exists()

    def test_nested_subdir_log_fresh_keeps_cal_dir(self, tmp_path):
        """嵌套结构：m0_cal_<run_id> 目录下含 m0_fanout_* 子目录，日志在子目录内
        （calibration 真实结构）→ 递归最新 mtime 判定。"""
        d = tmp_path / "r"
        d.mkdir()
        cal = d / "m0_cal_runX"
        cal.mkdir()
        inner = cal / "m0_fanout_20260809_000000_7"
        inner.mkdir()
        log_f = inner / "server_calib.log"
        log_f.write_text("x", encoding="utf-8")
        old_t = time.time() - 7200.0
        os.utime(cal, (old_t, old_t))
        os.utime(inner, (old_t, old_t))
        os.utime(log_f, (time.time() - 2.0, time.time() - 2.0))  # 活跃写入
        assert sch.cleanup_stale_dirs(str(d), max_age_seconds=3600.0) == []
        assert cal.exists()
        os.utime(log_f, (old_t, old_t))
        assert sch.cleanup_stale_dirs(str(d), max_age_seconds=3600.0) == ["m0_cal_runX"]
        assert not cal.exists()
