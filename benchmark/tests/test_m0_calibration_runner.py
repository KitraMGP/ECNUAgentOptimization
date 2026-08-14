"""m0_calibration_runner 测试（v56 校准可复现性）：CLI 参数、mock e2e smoke、
representative_output + sha256 合同。全部不依赖 GPU/真实 llama-server。"""
from __future__ import annotations

import hashlib
import json
import os

import pytest

from runner import m0_calibration_runner as cr
from runner import m0_fanout_runner as m0r
from runner import m0_schema as sch
from runner.m0_calibration_runner import main, parse_args, run_short_calibration
from tests.mock_server import MockOpenAIServer


class FakeAdapter:
    """mock server adapter（本地复制，非 no-op，真实绑定 MockOpenAIServer）。"""

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
            return (True, "ok")  # v61 契约：tuple/bool

    def read_log(self):
        return ("E8-C1: capability rejected: hybrid (recurrent+attention) model\n"
                if "on" in str(self.cmd) else "")


@pytest.fixture
def fake_adapter_cls(monkeypatch):
    FakeAdapter._instances = 0
    FakeAdapter._starts = 0
    monkeypatch.setattr(m0r, "ServerAdapter", FakeAdapter)
    return FakeAdapter


def cal_args(tmp_path, **over):
    args = dict(
        server_bin="fake-bin", model="mock.gguf", ctx_size=4096, ngl=99,
        tmp_dir=str(tmp_path / "tmp"), run_id="smoke-test",
        decision_n_predict=16, do_probe=True,
    )
    args.update(over)
    return args


# ---- CLI 参数 ----

class TestCli:
    def test_parse_args_defaults(self):
        ns = parse_args([])
        # 默认值由仓库根推导（__file__ → parents[2]），非硬编码绝对路径
        assert ns.server_bin.endswith("llama.cpp/build-cuda/bin/llama-server")
        assert ns.model.endswith("models/qwen3-5-4B-Q4_K_M.gguf")
        assert ns.ctx_size == 4096 and ns.ngl == 99
        assert ns.run_id == "" and not ns.no_probe and not ns.keep_tmp
        assert ns.decision_n_predict == 16

    def test_parse_args_explicit(self):
        ns = parse_args(["--server-bin", "/x/bin", "--model", "/x/m.gguf",
                         "--out", "/x/r.json", "--tmp-dir", "/x/t",
                         "--ctx-size", "2048", "--ngl", "0",
                         "--run-id", "r1", "--no-probe", "--keep-tmp",
                         "--decision-n-predict", "8"])
        assert ns.server_bin == "/x/bin" and ns.model == "/x/m.gguf"
        assert ns.out == "/x/r.json" and ns.tmp_dir == "/x/t"
        assert ns.ctx_size == 2048 and ns.ngl == 0
        assert ns.run_id == "r1" and ns.no_probe and ns.keep_tmp
        assert ns.decision_n_predict == 8

    def test_main_missing_server_bin(self, capsys, tmp_path):
        rc = main(["--server-bin", str(tmp_path / "nope"),
                   "--model", str(tmp_path / "nope2")])
        assert rc == 1
        assert "文件不存在" in capsys.readouterr().err


# ---- mock e2e smoke（不依赖 GPU） ----

class TestSmoke:
    def test_run_short_calibration_full(self, tmp_path, fake_adapter_cls):
        rep = run_short_calibration(**cal_args(tmp_path))
        assert rep["run_id"] == "smoke-test"
        assert isinstance(rep["parity_ok"], bool)
        assert isinstance(rep["parity_progress"], dict)
        assert isinstance(rep["calibrated_lengths"], dict)
        assert isinstance(rep["preflight_rejections"], list)
        dv = rep["decision_validation"]
        # v58：标准结构 sessions/total_valid_rate/partial（无第二 schema complete）
        assert set(dv.keys()) == {"sessions", "total_valid_rate", "partial"}
        assert isinstance(dv["partial"], bool)
        assert isinstance(dv["sessions"], list) and len(dv["sessions"]) <= 2
        for s in dv["sessions"]:
            assert "representative_output" in s and "representative_output_sha256" in s
            if s["representative_output"]:
                assert s["representative_output_sha256"] == hashlib.sha256(
                    s["representative_output"].encode("utf-8")).hexdigest()
            # validator 认可（含 v56 代表输出字段）
            assert sch.validate_decision_session(s) == []
        # probe 结构
        assert rep["probe_p10"] is not None
        assert "capability_rejected_lines" in rep["probe_p10"]
        assert "rs_buffer_lines" in rep["probe_p10"]
        # server 日志摘录：server tag / run_id / phase / key_lines
        for entry in rep["server_logs_summary"]:
            assert entry["server_tag"] in ("calib", "dv0", "dv1", "p10")
            assert entry["run_id"] == "smoke-test"
            assert entry["phase"]
            assert set(entry["key_lines"]) == {"rs_buffer", "capability_rejected", "kv_alloc"}
        assert "errors" in rep and "cleanup" in rep
        # gpu_rss_samples 明确采样来源（与 p10 静态探针区分）
        assert "source" in rep["gpu_rss_samples"]
        # v57（Medium 6）：peak_rss 跨 v55/v56 口径不可比声明 + probe 显式 stage
        assert "comparability" in rep["gpu_rss_samples"]
        assert "NOT" in rep["gpu_rss_samples"]["comparability"]
        assert rep["probe_p10"]["stage"] == "probe_p10"
        assert rep["probe_p10"]["pid"] is not None or rep["probe_p10"]["pid"] is None

    # ---- v57 审查（Medium 2）：server_logs_summary 用真实 _server_meta ----

    def test_server_logs_summary_uses_real_meta(self, tmp_path, fake_adapter_cls):
        """summary 只从 runner._server_meta 真实映射取 tag/pid/started_at，
        禁止 sorted index / 端口偏移推断。"""
        import time as _time
        r = m0r.M0FanoutRunner(server_bin="fake-bin", model="mock.gguf",
                               tmp_dir=str(tmp_path / "tmp"))
        t0 = _time.strftime("%Y-%m-%dT%H:%M:%S%z")
        # 手动构造 meta：顺序故意乱序 + 一个启动失败的 tag 缺 meta
        r._server_meta = {
            "dv1": {"port": 8083, "pid": 5555, "started_at": t0,
                    "log_path": str(tmp_path / "dv1.log")},
            "calib": {"port": 8080, "pid": 1111, "started_at": t0,
                      "log_path": str(tmp_path / "calib.log")},
        }
        (tmp_path / "calib.log").write_text(
            "llama_memory_recurrent::init: RS buffer size = 201.00 MiB\n"
            "E8-C1: capability rejected: hybrid model\n", encoding="utf-8")
        (tmp_path / "dv1.log").write_text(
            "llama_memory_recurrent::init: RS buffer size = 502.50 MiB\n",
            encoding="utf-8")
        summary = cr._server_logs_summary(r, "smoke-test")
        # 遍历顺序 = _server_meta 插入顺序（dict 有序），pid/started_at 真实
        assert [e["server_tag"] for e in summary] == ["dv1", "calib"]
        assert {e["pid"] for e in summary} == {5555, 1111}
        for e in summary:
            assert e["started_at"] == t0
        by_tag = {e["server_tag"]: e for e in summary}
        assert by_tag["calib"]["key_lines"]["rs_buffer"] == [
            "llama_memory_recurrent::init: RS buffer size = 201.00 MiB"]
        assert by_tag["calib"]["key_lines"]["capability_rejected"] == [
            "E8-C1: capability rejected: hybrid model"]
        assert by_tag["dv1"]["key_lines"]["rs_buffer"] == [
            "llama_memory_recurrent::init: RS buffer size = 502.50 MiB"]

    def test_server_logs_summary_refreshes_after_stop(self, tmp_path, fake_adapter_cls):
        """stop 后重新读盘日志（避免 health 时启动期快照不完整）：log_path 内容
        更新后 summary 反映最新日志，不沿用启动期快照。"""
        import time as _time
        r = m0r.M0FanoutRunner(server_bin="fake-bin", model="mock.gguf",
                               tmp_dir=str(tmp_path / "tmp"))
        log_p = tmp_path / "s.log"
        t0 = _time.strftime("%Y-%m-%dT%H:%M:%S%z")
        r._server_meta = {"p10": {"port": 8090, "pid": 999, "started_at": t0,
                                  "log_path": str(log_p)}}
        # 启动期快照：只有 health 前的一行（不完整）
        r._server_logs["p10"] = "server listening\n"
        log_p.write_text(
            "server listening\nRS buffer size = 502.50 MiB\nKV buffer size = 128 MiB\n",
            encoding="utf-8")
        summary = cr._server_logs_summary(r, "r")
        assert summary[0]["key_lines"]["rs_buffer"] == ["RS buffer size = 502.50 MiB"]
        assert summary[0]["key_lines"]["kv_alloc"] == ["KV buffer size = 128 MiB"]

    def test_run_short_calibration_no_probe(self, tmp_path, fake_adapter_cls):
        rep = run_short_calibration(**cal_args(tmp_path, do_probe=False))
        assert rep["probe_p10"] is None
        assert rep["metrics_kv"] is None

    def test_main_writes_report(self, tmp_path, fake_adapter_cls):
        bin_p = tmp_path / "llama-server"
        model_p = tmp_path / "mock.gguf"
        bin_p.write_text("#!/bin/sh\n")
        model_p.write_text("x")
        out = str(tmp_path / "rep.json")
        rc = main(["--server-bin", str(bin_p), "--model", str(model_p),
                   "--out", out, "--tmp-dir", str(tmp_path / "tmp"),
                   "--run-id", "cli-run"])
        assert rc == 0
        assert os.path.isfile(out)
        with open(out, encoding="utf-8") as f:
            doc = json.load(f)
        assert doc["run_id"] == "cli-run"
        assert doc["run_mode"] == "short-calibration (no formal matrix)"
        assert "decision_validation" in doc and "server_logs_summary" in doc

    # ---- v57 审查（Medium 3）：tmp 仅成功写盘后且非 --keep-tmp 才删除 ----

    def test_main_success_cleans_tmp(self, tmp_path, fake_adapter_cls):
        """报告成功写盘且非 --keep-tmp → 本 run 唯一子目录被删除（v58）。"""
        bin_p = tmp_path / "llama-server"
        model_p = tmp_path / "mock.gguf"
        bin_p.write_text("#!/bin/sh\n")
        model_p.write_text("x")
        tmp_dir = tmp_path / "tmp"
        rc = main(["--server-bin", str(bin_p), "--model", str(model_p),
                   "--out", str(tmp_path / "rep.json"),
                   "--tmp-dir", str(tmp_dir), "--run-id", "cli-run"])
        assert rc == 0
        # v58：--tmp-dir 视为父目录——父目录本身保留、仅本 run 子目录被删
        assert os.path.isdir(tmp_dir)
        assert not os.path.isdir(tmp_dir / "m0_cal_cli-run")

    def test_main_keep_tmp_preserves(self, tmp_path, fake_adapter_cls):
        """--keep-tmp → tmp-dir 保留。"""
        bin_p = tmp_path / "llama-server"
        model_p = tmp_path / "mock.gguf"
        bin_p.write_text("#!/bin/sh\n")
        model_p.write_text("x")
        tmp_dir = tmp_path / "tmp"
        rc = main(["--server-bin", str(bin_p), "--model", str(model_p),
                   "--out", str(tmp_path / "rep.json"),
                   "--tmp-dir", str(tmp_dir), "--run-id", "cli-run",
                   "--keep-tmp"])
        assert rc == 0
        # v58：keep-tmp 保留本 run 唯一子目录（父目录为共享父级，保留）
        assert os.path.isdir(tmp_dir / "m0_cal_cli-run")
        assert os.path.isdir(tmp_dir)

    def test_main_report_write_failure_keeps_tmp(self, tmp_path, fake_adapter_cls):
        """报告写失败 → tmp-dir 保留排障（不删除）。"""
        bin_p = tmp_path / "llama-server"
        model_p = tmp_path / "mock.gguf"
        bin_p.write_text("#!/bin/sh\n")
        model_p.write_text("x")
        tmp_dir = tmp_path / "tmp"
        # 输出路径为已存在目录（open(dir,"w") → IsADirectoryError）→ 写报告 OSError
        out_dir = tmp_path / "as_dir"
        out_dir.mkdir()
        rc = main(["--server-bin", str(bin_p), "--model", str(model_p),
                   "--out", str(out_dir),
                   "--tmp-dir", str(tmp_dir), "--run-id", "cli-run"])
        assert rc == 1
        assert os.path.isdir(tmp_dir)  # 排障保留

    # ---- v57 审查（Medium 4）：cleanup.leftover_pids 只查本 runner PID 集合 ----

    def test_cleanup_checks_only_sampled_pids(self, tmp_path, fake_adapter_cls,
                                              monkeypatch):
        """cleanup 只检查 peak.pids_seen（本 runner 采样 PID），不用全系统 pgrep
        ——mock 环境无采样 PID 时 leftover 为空，即使系统有其他 llama-server。"""
        rep = run_short_calibration(**cal_args(tmp_path))
        assert rep["cleanup"]["leftover_pids"] == []
        assert rep["cleanup"]["checked_pids"] == []
        assert "no system-wide pgrep" in rep["cleanup"]["method"]

    def test_cleanup_alive_and_dead_pids(self, monkeypatch):
        """pids_seen 中存活 PID 列出、已退出 PID 不列。"""
        import os as _os

        # 用当前进程（存活）模拟本 runner 采样 PID；另一个必然不存在的 PID
        alive = _os.getpid()
        dead = 99999999  # 大概率不存在（ProcessLookupError）
        monkeypatch.setattr(cr._PeakSampler, "pids_seen", [alive, dead],
                            raising=False)
        peak = cr._PeakSampler()
        peak.pids_seen = [alive, dead]
        assert sorted(int(x) for x in cr._pid_alive_filter(peak.pids_seen)) == [alive]

    # ---- v57 审查（Medium 5）：calibration 异常保留已完成部分与结构化 error ----

    def test_calibration_failure_preserves_partial(self, tmp_path, fake_adapter_cls,
                                                   monkeypatch):
        """calibration 中途失败：报告保留 parity_progress / calibrated_lengths /
        preflight_rejections 已完成部分 + 结构化 error（非纯字符串）。"""
        captured = {}

        def _calib_fail_after_partial(self):
            # 模拟已完成的校准部分
            self.parity_progress = {"completed": ["short-P", "short-B"], "errors": []}
            self.calibrated_lengths = {
                ("short", 2): {"prefix": 120, "branch": 40},
                ("short", 4): {"prefix": 120, "branch": 30},
            }
            self.preflight_rejections = [
                {"unit_id": "long/N8/q8_0/off", "reason": "budget_exceeded"}]
            raise RuntimeError("mock calibration boom")

        monkeypatch.setattr(m0r.M0FanoutRunner, "run_calibration",
                            _calib_fail_after_partial)
        rep = run_short_calibration(**cal_args(tmp_path))
        assert rep["parity_progress"]["completed"] == ["short-P", "short-B"]
        assert rep["calibrated_lengths"]["short/fanout2"] == {"prefix": 120,
                                                              "branch": 40}
        assert len(rep["preflight_rejections"]) == 1
        assert rep["preflight_rejections"][0]["unit_id"] == "long/N8/q8_0/off"
        # 结构化 error（非字符串）
        assert rep["errors"] == [{
            "code": "validation_incomplete", "stage": "calibration", "count": 1,
            "detail": "RuntimeError: mock calibration boom"}]

    def test_dv_failure_preserves_partial_sessions(self, tmp_path, fake_adapter_cls,
                                                   monkeypatch):
        """decision_validation 中途失败：保留已收集 sessions（partial）与结构化 error。

        v58：不再 monkeypatch 整方法手写属性——只替换子方法 _run_decision_session
        （真实 run_decision_validation 流程：session 0 完成、session 1 中途抛异常），
        report 落标准结构（sessions/total_valid_rate/partial，无第二 schema）。
        """
        calls = {"n": 0}

        def fake_run_session(self, port):
            calls["n"] += 1
            if calls["n"] == 2:  # session 1（control=on）中途抛异常
                raise RuntimeError("mock dv boom")
            return [dict(ok=True, error=None, finish_reason="stop",
                         text="ACTION: branch(b1)")] * 10, False

        monkeypatch.setattr(m0r.M0FanoutRunner, "_run_decision_session", fake_run_session)
        rep = run_short_calibration(**cal_args(tmp_path))
        dv = rep["decision_validation"]
        assert set(dv.keys()) == {"sessions", "total_valid_rate", "partial"}
        assert "complete" not in dv  # v58：不造第二 schema（由 partial 派生）
        assert dv["partial"] is True
        assert len(dv["sessions"]) == 1
        assert dv["sessions"][0]["requests"] == 10
        assert rep["errors"][0]["stage"] == "decision_validation"
        assert rep["errors"][0]["count"] == 1
        assert rep["errors"][0]["code"] == "validation_incomplete"

    def test_pid_alive_semantics(self):
        import os as _os
        assert cr._pid_alive(_os.getpid()) is True
        assert cr._pid_alive(99999999) is False
        assert cr._pid_alive(0) is False
        assert cr._pid_alive(-5) is False

    def test_main_runner_failure_keeps_tmp(self, tmp_path, fake_adapter_cls, monkeypatch):
        """runner 内部异常 → tmp-dir 保留（不删除）。"""
        bin_p = tmp_path / "llama-server"
        model_p = tmp_path / "mock.gguf"
        bin_p.write_text("#!/bin/sh\n")
        model_p.write_text("x")
        tmp_dir = tmp_path / "tmp"

        def _boom(*a, **k):
            raise RuntimeError("runner boom")
        monkeypatch.setattr(cr, "run_short_calibration", _boom)
        rc = main(["--server-bin", str(bin_p), "--model", str(model_p),
                   "--out", str(tmp_path / "rep.json"),
                   "--tmp-dir", str(tmp_dir), "--run-id", "cli-run"])
        assert rc == 1
        assert os.path.isdir(tmp_dir)  # 排障保留


# ---- representative_output + sha256（v56 合同） ----

class TestRepresentativeOutput:
    def test_build_and_validate(self):
        text = "ACTION: branch(b2)"
        sess = sch.build_decision_session(
            requests=1, valid=1, invalid=0, error_count=0,
            representative_output=text)
        assert sess["representative_output"] == text
        assert sess["representative_output_sha256"] == hashlib.sha256(
            text.encode("utf-8")).hexdigest()
        assert sch.validate_decision_session(sess) == []
        # 输出必须通过 parse_action（valid 前置）
        from runner.fanout_prompts import parse_action
        assert parse_action(text) == 2

    def test_empty_allowed(self):
        sess = sch.build_decision_session(
            requests=0, valid=0, invalid=0, error_count=0)
        assert sess["representative_output"] == ""
        assert sess["representative_output_sha256"] == ""
        assert sch.validate_decision_session(sess) == []

    def test_bad_sha_rejected(self):
        sess = sch.build_decision_session(
            requests=1, valid=1, invalid=0, error_count=0,
            representative_output="ACTION: branch(b1)")
        sess["representative_output_sha256"] = "0" * 64
        errs = sch.validate_decision_session(sess)
        assert any(e[0] == "invariant_violation" for e in errs)

    def test_non_hex_sha_rejected(self):
        sess = sch.build_decision_session(
            requests=1, valid=1, invalid=0, error_count=0,
            representative_output="ACTION: branch(b1)")
        sess["representative_output_sha256"] = "not-hex"
        errs = sch.validate_decision_session(sess)
        assert any(e[0] == "type_mismatch" for e in errs)

    def test_asymmetry_rejected(self):
        sess = sch.build_decision_session(
            requests=1, valid=1, invalid=0, error_count=0,
            representative_output="ACTION: branch(b1)")
        sess["representative_output"] = ""
        errs = sch.validate_decision_session(sess)
        assert any(e[0] == "invariant_violation" for e in errs)

    # ---- v57 审查（Medium 1）：v55 旧格式向后兼容 + valid>0 非空语义 ----

    def test_v55_legacy_session_without_rep_fields_accepted(self):
        """v55 旧 run1 数据：两个 representative 字段同时缺失 → 按旧格式接受，
        即使 valid>0 也不强制代表输出（旧格式无此字段）。"""
        sess = {
            "control": "off", "requests": 10, "valid": 9, "invalid": 1,
            "error_count": 0, "invalid_length": 1, "invalid_no_action": 0,
            "finish_reasons": {"stop": 9, "length": 1},
            "output_hashes": ["a"] * 10, "error_summary": [],
        }
        assert "representative_output" not in sess
        assert "representative_output_sha256" not in sess
        assert sch.validate_decision_session(sess) == []

    def test_v55_legacy_inside_sessions_accepted(self):
        """旧 run1 decision_validation.sessions 整体 validator 接受（含缺字段）。"""
        sess = {
            "control": "off", "requests": 10, "valid": 8, "invalid": 2,
            "error_count": 0, "invalid_length": 2, "invalid_no_action": 0,
            "finish_reasons": {"stop": 8, "length": 2},
            "output_hashes": ["a"] * 10, "error_summary": [],
        }
        dv = sch.build_decision_validation([sess], partial=False)
        assert sch.validate_decision_validation(dv, phase="preflight") == []

    def test_rep_only_field_present_rejected(self):
        """任一字段存在则两者必须同时存在：只给 representative_output → 拒绝。"""
        sess = sch.build_decision_session(
            requests=1, valid=1, invalid=0, error_count=0,
            representative_output="ACTION: branch(b1)")
        del sess["representative_output_sha256"]
        errs = sch.validate_decision_session(sess)
        assert any(e[0] == "invariant_violation" and "同时存在或同时缺失" in e[2]
                   for e in errs)

    def test_sha_only_field_present_rejected(self):
        """只给 representative_output_sha256 → 拒绝。"""
        sess = sch.build_decision_session(
            requests=1, valid=1, invalid=0, error_count=0,
            representative_output="ACTION: branch(b1)")
        del sess["representative_output"]
        errs = sch.validate_decision_session(sess)
        assert any(e[0] == "invariant_violation" for e in errs)

    def test_valid_gt0_with_empty_rep_rejected(self):
        """v56+ 格式：valid>0 且 representative_output 空白 → 拒绝（valid>0 必须
        代表输出非空）。"""
        sess = sch.build_decision_session(
            requests=1, valid=1, invalid=0, error_count=0,
            representative_output="ACTION: branch(b1)")
        sess["representative_output"] = ""
        sess["representative_output_sha256"] = ""  # 同空：仅触发 valid>0 语义
        errs = sch.validate_decision_session(sess)
        assert any(e[0] == "invariant_violation" and "valid>0" in e[2]
                   for e in errs)

    def test_valid_zero_with_empty_rep_accepted(self):
        """valid=0 可同空（空 session 合法形态）。"""
        sess = sch.build_decision_session(
            requests=0, valid=0, invalid=0, error_count=0)
        sess["representative_output"] = ""
        sess["representative_output_sha256"] = ""
        assert sch.validate_decision_session(sess) == []
