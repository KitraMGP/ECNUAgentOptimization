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
        assert dv["complete"] is False or dv["complete"] is True
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
