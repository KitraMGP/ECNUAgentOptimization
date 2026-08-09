"""m0_fanout_runner 测试（设计 §5.1 测试要求⑰/㉕/㉖/㉗ 等）：CLI、mock e2e、
首/第2+ group 失败、崩溃第 5 rep、SCHEMA_INVALID、原子写失败与 cleanup。"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from runner import m0_fanout_runner as m0r
from runner import m0_schema as sch
from runner.m0_fanout_runner import M0FanoutRunner, main
from runner.m0_schema import (
    EXIT_IO_WRITE_FAILED,
    EXIT_SCHEMA_INVALID,
    EXIT_USAGE,
)
from tests.mock_server import MockOpenAIServer


class FakeAdapter:
    """mock server adapter：真实绑定 MockOpenAIServer（非 no-op），可模拟启动失败/崩溃。"""

    fail_after = None    # 非 None：全局第 N 次 start 起 wait_health 失败（启动失败模拟）
    crash_at_after = None  # 非 None：从第 N 个 adapter 起带 crash_at=31（rep4 内崩溃）
    _instances = 0
    _starts = 0

    def __init__(self, cmd, log_path, port, crash_at=None):
        self.cmd = cmd
        self.log_path = log_path
        self.port = port
        FakeAdapter._instances += 1
        self._instance_no = FakeAdapter._instances
        if (FakeAdapter.crash_at_after is not None
                and self._instance_no >= FakeAdapter.crash_at_after):
            crash_at = 31  # f2 unit 每 5 POST；warmup short 10 + rep0-3 各 5（20）
            # → rep4 在 POST 31-35 内崩溃（第 5 rep 已开始）
        self.crash_at = crash_at
        self._server: MockOpenAIServer | None = None
        self.stopped = False

    def start(self):
        FakeAdapter._starts += 1
        if FakeAdapter.fail_after is not None and FakeAdapter._starts >= FakeAdapter.fail_after:
            self._server = None
            return
        self._server = MockOpenAIServer(crash_at=self.crash_at).__enter__()
        self.port = self._server.port  # runner 用 adapter.port 建连

    def poll(self):
        if self.crash_at is not None and self._server is not None \
                and self._server.httpd is not None \
                and self._server.httpd.count > self.crash_at:
            return 1  # 已退出（崩溃）
        if self._server is None:
            return 1
        return None

    def wait_health(self, timeout=120.0):
        if self._server is None:
            return False
        return True

    def stop(self):
        self.stopped = True
        if self._server is not None:
            self._server.__exit__(None, None, None)
            self._server = None

    def read_log(self):
        return "capability rejected: hybrid\n" if "on" in str(self.cmd) else ""


@pytest.fixture
def fake_adapter_cls(monkeypatch):
    FakeAdapter.fail_after = None
    FakeAdapter.crash_at_after = None
    FakeAdapter._instances = 0
    FakeAdapter._starts = 0
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
    return M0FanoutRunner(**args)


# ---- ⑰ cleanup / 端口 ----

class TestCleanup:
    def test_stop_server_cleans(self, tmp_path, fake_adapter_cls):
        r = make_runner(tmp_path, fake_adapter_cls)
        r._start_server(4, "q8_0", "q8_0", "off", "t")
        assert r._adapter is not None
        r._stop_server()
        assert r._adapter is None

    def test_cleanup_all_on_error(self, tmp_path, fake_adapter_cls):
        r = make_runner(tmp_path, fake_adapter_cls)
        r._start_server(4, "q8_0", "q8_0", "off", "t")
        r._cleanup_all()
        assert r._adapter is None


# ---- ㉕ CLI ----

class TestCLI:
    @pytest.mark.parametrize("arg", [
        "--fanout", "--prefix-len", "--branch-len", "--ctk",
        "--ctv", "--parallel", "--warmup", "--reps",
    ])
    def test_unknown_args_exit_64(self, capsys, arg):
        rc = main([arg, "1", "--server-bin", "b", "--model", "m"])
        out = capsys.readouterr().err
        assert rc == EXIT_USAGE
        assert "INVALID_CONFIGURATION" in out

    def test_required_args_missing(self, capsys):
        with pytest.raises(SystemExit) as e:
            main(["--model", "m"])
        assert e.value.code == EXIT_USAGE

    def test_ctx_size_positive(self, capsys):
        rc = main(["--server-bin", "b", "--model", "m", "--ctx-size", "0"])
        assert rc == EXIT_USAGE

    def test_unknown_arbitrary_arg(self, capsys):
        # 非 8 项清单的任意未知参数由 argparse 拦截 → SystemExit(64)
        with pytest.raises(SystemExit) as e:
            main(["--server-bin", "b", "--model", "m", "--bogus", "1"])
        assert e.value.code == EXIT_USAGE


# ---- mock e2e（正常路径：full 24-unit 矩阵） ----

class TestMockE2E:
    def test_full_matrix_smoke(self, tmp_path, fake_adapter_cls):
        r = make_runner(tmp_path, fake_adapter_cls)
        rc = r.run()
        assert rc == 0, rc
        with open(r.out_path, encoding="utf-8") as f:
            doc = json.load(f)
        ok, errs = sch.validate_result_envelope(doc)
        assert ok, errs
        meta = doc["meta"]
        assert meta["phase"] == "formal"
        assert meta["planned_units"] == 24
        assert meta["matrix_complete"] is True
        # mock 决策输出无 ACTION → 全部 fallback → HOLD_NOT_VALIDATED
        assert doc["verdict"] == "HOLD_NOT_VALIDATED"
        assert meta["decision_fallback"] is True
        assert doc["gates"]["G-M0-1"]["status"] == "FAIL"
        # 12 groups（off/on × 2 profile × 3 fanout）
        n_groups = (len(doc["modes"]["off"]["server_groups"])
                    + len(doc["modes"]["on"]["server_groups"]))
        assert n_groups == 12
        # 每 group 5 reps/unit、rep_index 0..4
        for control in ("off", "on"):
            for g in doc["modes"][control]["server_groups"]:
                assert g["status"] == "COMPLETED"
                assert g["warmup_count"] == 2
                assert all(0 <= rep["rep_index"] <= 4 for rep in g["replicates"])
        # executed_units == 24（complete 由 rep_index 完整性判定）
        assert meta["executed_units"] == 24

    def test_modes_only_started_groups_preflight(self, tmp_path, fake_adapter_cls):
        # 首个 server（校准）启动失败 → preflight/PREFLIGHT_INFRA（modes={}、
        # gates={}、不评估任何 gate——preflight ⇔ modes={} ∧ gates={}）
        FakeAdapter.fail_after = 1
        try:
            r = make_runner(tmp_path, fake_adapter_cls)
            rc = r.run()
            assert rc == 0
            with open(r.out_path, encoding="utf-8") as f:
                doc = json.load(f)
            assert doc["meta"]["phase"] == "preflight"
            assert doc["meta"]["preflight_reason"] == "validation_incomplete"
            assert doc["meta"]["matrix_complete"] is False
            assert doc["modes"] == {} and doc["gates"] == {}
            assert doc["meta"]["executed_units"] == 0
            assert doc["verdict"] == "INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE"
        finally:
            FakeAdapter.fail_after = None

    def test_second_group_failure_formal_incomplete(self, tmp_path, fake_adapter_cls):
        # 首 formal group 启动失败 → preflight 由 test_first_formal_group_failure
        # 覆盖（校准 1 + 验证 2 成功后 formal 首组失败）；
        # 第 2+ group 启动失败 → formal/FORMAL_INCOMPLETE 见
        # test_second_formal_group_failure（fail_start=5）。
        pass

    def test_first_formal_group_failure(self, tmp_path, fake_adapter_cls):
        # 校准（1 server）+ 验证（2 servers）成功后，首个 formal group 启动失败
        # → preflight/PREFLIGHT_INFRA、modes={}、gates={}、保留完整 decision_validation
        # 全局 start 计数：校准 1 次成功、验证 2 次成功（第 1..3 次 start 成功）；
        # formal 首组第 1 次尝试（全局第 4 次 start）失败、重试（5、6）也失败
        # → ServerError → FirstGroupStartFailed → preflight。
        FakeAdapter.fail_after = 4
        try:
            r = make_runner(tmp_path, fake_adapter_cls)
            rc = r.run()
            assert rc == 0
            with open(r.out_path, encoding="utf-8") as f:
                doc = json.load(f)
            meta = doc["meta"]
            assert meta["phase"] == "preflight"
            assert meta["preflight_reason"] == "validation_incomplete"
            assert meta["planned_units"] == 24
            assert meta["executed_units"] == 0
            assert meta["matrix_complete"] is False
            assert doc["modes"] == {} and doc["gates"] == {}
            # 保留完整 decision_validation（2 sessions×≥10）
            dv = meta["decision_validation"]
            assert dv is not None and len(dv["sessions"]) == 2
            assert all(s["requests"] >= 10 for s in dv["sessions"])
            assert meta["parity_ok"] is True
            assert meta["token_count_method"] == sch.TOKEN_COUNT_METHOD
            assert doc["verdict"] == "INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE"
        finally:
            FakeAdapter.fail_after = None

    def test_second_formal_group_failure(self, tmp_path, fake_adapter_cls):
        # 首个 formal group 成功后第 2 个失败 → formal/FORMAL_INCOMPLETE、
        # modes 只含已启动 groups、失败 group ERROR、matrix_complete=false
        # 全局 start：校准 1 + 验证 2 + formal 首组 1 = 4 次成功 → 第 2 组
        # 第 1 次尝试（全局第 5 次 start）失败、重试（6、7）也失败 → fail_after=5
        FakeAdapter.fail_after = 5
        try:
            r = make_runner(tmp_path, fake_adapter_cls)
            rc = r.run()
            assert rc == 0
            with open(r.out_path, encoding="utf-8") as f:
                doc = json.load(f)
            meta = doc["meta"]
            assert meta["phase"] == "formal"
            assert meta["matrix_complete"] is False
            assert meta["executed_units"] < 24
            assert doc["verdict"] == "INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE"
            groups = (doc["modes"]["off"]["server_groups"]
                      + doc["modes"]["on"]["server_groups"])
            assert len(groups) >= 1
            # 首组 COMPLETED，失败组 ERROR（error_type 必填）
            assert groups[0]["status"] == "COMPLETED"
            assert groups[-1]["status"] == "ERROR"
            assert groups[-1]["error_type"] in sch.ERROR_CODES
            assert groups[-1]["error_stage"] == "formal"
        finally:
            FakeAdapter.fail_after = None

    def test_crash_at_5th_rep(self, tmp_path, fake_adapter_cls):
        # 崩溃发生在某 unit 第 5 个 rep（rep_index=4）已开始且记录 ERROR →
        # 该 unit 的 rep_index 集合 {0..4} 完整 → 计 complete；但 group 崩溃 →
        # matrix_complete=false；与请求级 REP_ERROR（无进程崩溃、matrix_complete
        # 可 true）严格区分。
        # crash_at_after=4：从第 4 个 adapter（formal 首组）起带 crash_at=31
        # （f2 unit 每 5 POST：warmup short 10 + rep0-3 各 5 = 30 → rep4 内崩溃）。
        FakeAdapter.crash_at_after = 4
        try:
            r = make_runner(tmp_path, fake_adapter_cls)
            rc = r.run()
            assert rc == 0
            with open(r.out_path, encoding="utf-8") as f:
                doc = json.load(f)
            assert doc["meta"]["matrix_complete"] is False
            assert doc["verdict"] == "INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE"
            groups = (doc["modes"]["off"]["server_groups"]
                      + doc["modes"]["on"]["server_groups"])
            assert groups[-1]["status"] == "ERROR"
            assert groups[-1]["error_type"] == "server_crash"
            # 崩溃组 short unit 5 reps 均记录 → rep_index 集合完整
            g0 = groups[0]
            by_unit: dict = {}
            for rep in g0["replicates"]:
                by_unit.setdefault(rep["unit_id"], set()).add(rep["rep_index"])
            short = [u for u in by_unit if u.endswith(":f2:short")]
            assert short, by_unit
            assert by_unit[short[0]] == set(range(5))
            reps_short = [r for r in g0["replicates"] if r["unit_id"] == short[0]]
            assert any(r["rep_index"] == 4 and r["status"] == "ERROR"
                       and r["error_type"] == "server_crash" for r in reps_short)
        finally:
            FakeAdapter.crash_at_after = None


# ---- 原子写失败（⑬b-e） ----

class TestWriteFailure:
    def test_io_write_failed(self, tmp_path, fake_adapter_cls):
        r = make_runner(tmp_path, fake_adapter_cls)
        # 校准前直接走 _write_result（模拟落盘失败）
        doc = r._meta_context()
        doc["meta"].update({"phase": "preflight", "preflight_status": "FAILED",
                            "preflight_reason": "validation_incomplete"})
        r.out_path = os.path.join(str(tmp_path), "no_such_dir", "out.json")
        rc = r._write_result(doc)
        assert rc == EXIT_IO_WRITE_FAILED


# ---- main() 集成 smoke（CLI → mock 全流程） ----

class TestMainSmoke:
    def test_main_smoke(self, tmp_path, fake_adapter_cls, monkeypatch):
        # CLI → mock 全流程：真实 main() 解析后注入 FakeAdapter 的 runner
        real_runner = M0FanoutRunner

        def _fake_runner(**kw):
            kw["adapter_cls"] = fake_adapter_cls
            kw.setdefault("tmp_dir", str(tmp_path / "t2"))
            return real_runner(**kw)

        monkeypatch.setattr(m0r, "M0FanoutRunner", _fake_runner)
        out = str(tmp_path / "cli_out.json")
        rc = main(["--server-bin", "fake", "--model", "m.gguf",
                   "--out", out, "--tmp-dir", str(tmp_path / "t2")])
        assert rc == 0, rc
        assert os.path.exists(out)
        with open(out, encoding="utf-8") as f:
            doc = json.load(f)
        assert doc["meta"]["phase"] == "formal"

    def test_main_schema_invalid_direct(self, tmp_path, fake_adapter_cls):
        r = make_runner(tmp_path, fake_adapter_cls)
        rc = r._write_schema_invalid(
            [{"path": "$.meta", "code": "type_mismatch",
              "expected_type": "object", "actual_type": "string"}], "formal")
        assert rc == EXIT_SCHEMA_INVALID
        side = str(tmp_path / "out.schema_invalid.json")
        assert os.path.exists(side)
        with open(side, encoding="utf-8") as f:
            sidecar = json.load(f)
        ok, _ = sch.validate_schema_sidecar(sidecar)
        assert ok
        with open(r.out_path, encoding="utf-8") as f:
            env = json.load(f)
        assert env["meta"]["preflight_reason"] == "schema_invalid"
        ok2, _ = sch.validate_result_envelope(env)
        assert ok2
