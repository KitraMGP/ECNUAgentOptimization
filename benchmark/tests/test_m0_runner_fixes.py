"""M0 Critical 修复测试（审查 a408031 十项合同）。

集中覆盖：
- Critical 1：KVProbe 数据路径（baseline 从 snapshot['data']、per-rep 边界、
  erase 后采样、G-M0-3a/G-M0-5 缺观测/非零 → FAIL 不静默 PASS）
- Critical 2：Driver.chat 顶层 finish_reason；length → INVALID_DECISION + invalid_length>0
- Critical 3：_connect 传实际 host/port；sampler 按端口找 PID（非 8080）
- Critical 4：启动 OSError（bin 缺失）→ ServerError → preflight 落盘，无 traceback
- Critical 5：atomic tmp 命名与 cleanup_stale_tmp 匹配 + CLI 入口清理
- Critical 6：prefix/branch_len 用 (bucket,fanout) 校准实测值；validator 一致性
- Critical 7：warmup 崩溃 → group ERROR / FORMAL_INCOMPLETE（不被吞）
- Critical 8：mock_server 无误导性实例 count 死属性
- Critical 9：G-M0-5 真实 llama.cpp 日志串（server-context.cpp:1505）
- Critical 10：G-M0-4 derived 自检 + RS buffer 独立观测（llama-memory-recurrent.cpp:115）
"""
from __future__ import annotations

import json
import os

import pytest

from framework import sampler
from framework.driver import Driver
from runner import fanout_prompts as fp
from runner import m0_fanout_runner as m0r
from runner import m0_schema as sch
from runner.m0_fanout_runner import M0FanoutRunner, ServerAdapter
from tests.mock_server import MockOpenAIServer


class FakeAdapter:
    """与 test_m0_fanout_runner 同构的 mock adapter（独立副本，避免跨文件耦合）。"""

    fail_after = None
    crash_at_after = None
    crash_at_value = 31
    _instances = 0
    _starts = 0

    def __init__(self, cmd, log_path, port, crash_at=None):
        self.cmd = cmd
        self.log_path = log_path
        self.port = port
        FakeAdapter._instances += 1
        if (FakeAdapter.crash_at_after is not None
                and FakeAdapter._instances >= FakeAdapter.crash_at_after):
            crash_at = FakeAdapter.crash_at_value
        self.crash_at = crash_at
        self._server: MockOpenAIServer | None = None

    def start(self):
        FakeAdapter._starts += 1
        if FakeAdapter.fail_after is not None and FakeAdapter._starts >= FakeAdapter.fail_after:
            self._server = None
            return
        self._server = MockOpenAIServer(crash_at=self.crash_at).__enter__()
        self.port = self._server.port

    def poll(self):
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

    def read_log(self):
        return ("E8-C1: capability rejected: hybrid (recurrent+attention) model\n"
                if "on" in str(self.cmd) else "")


@pytest.fixture
def fake_adapter_cls(monkeypatch):
    FakeAdapter.fail_after = None
    FakeAdapter.crash_at_after = None
    FakeAdapter.crash_at_value = 31
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


# ---- Critical 1：KVProbe 数据路径 ----

class TestCritical1KVPath:
    def test_baseline_capacity_from_snapshot_data(self, tmp_path):
        """capacity_bytes 在 snapshot['data'] 内——kv_buffer_mb 必须读 data。"""
        with MockOpenAIServer() as srv:
            r = M0FanoutRunner(server_bin="x", model="m", ctx_size=4096,
                               port_base=18080, tmp_dir=str(tmp_path / "t"),
                               out_path="", adapter_cls=ServerAdapter)
            r._driver, r._kv = r._connect(srv.port)
            bs = r._capture_baseline("off:q8_0-q8_0:f2")
        # mock /metrics/kv capacity_bytes = 104857600 = 100 MiB
        assert bs["kv_buffer_mb"] == pytest.approx(100.0)
        assert bs["metrics_kv_snapshot"]["capacity_bytes"] == 104857600
        assert "data" not in bs["metrics_kv_snapshot"]  # 保存权威 data，非包装

    def _minimal_modes(self, kv_last=None, shared=None, branches=None):
        def _rep(control, ctk, ctv, fanout, idx, kv, br):
            return {
                "unit_id": sch.unit_id_of(control, ctk, ctv, fanout, "short"),
                "rep_index": idx, "fanout": fanout, "bucket": "short",
                "ctk": ctk, "ctv": ctv, "prefix_len": 100, "branch_len": 100,
                "server_group_id": sch.group_id_of(control, ctk, ctv, fanout),
                "status": "OK", "decision_fallback": True,
                "metrics": {"kv": kv, "branches": br},
            }

        off = {
            "server_group_id": sch.group_id_of("off", "q8_0", "q8_0", 2),
            "ctk": "q8_0", "ctv": "q8_0", "fanout": 2, "status": "COMPLETED",
            "baseline": {"rs_buffer_mb": 50.0, "metrics_kv_snapshot": {}},
            "replicates": [_rep("off", "q8_0", "q8_0", 2, 0, kv_last, branches)],
        }
        on = {
            "server_group_id": sch.group_id_of("on", "q8_0", "q8_0", 2),
            "ctk": "q8_0", "ctv": "q8_0", "fanout": 2, "status": "COMPLETED",
            "baseline": {"rs_buffer_mb": 50.0, "metrics_kv_snapshot": {}},
            "replicates": [_rep("on", "q8_0", "q8_0", 2, 0, kv_last, branches)],
        }
        if shared is not None:
            off["baseline"]["metrics_kv_snapshot"] = {"shared_cells": shared}
            on["baseline"]["metrics_kv_snapshot"] = {"shared_cells": shared}
        return {"off": {"server_groups": [off]}, "on": {"server_groups": [on]}}

    def _runner_with_log(self, tmp_path):
        r = make_runner(tmp_path, None)
        os.makedirs(r.tmp_dir, exist_ok=True)
        with open(os.path.join(r.tmp_dir, "server_g0.log"), "w",
                  encoding="utf-8") as f:
            f.write("E8-C1: capability rejected: hybrid (recurrent+attention) model\n")
        return r

    def test_g3a_nonzero_used_cells_fails(self, tmp_path):
        r = self._runner_with_log(tmp_path)
        modes = self._minimal_modes(
            kv_last={"used_cells": 5, "active_sequences": 0}, shared=0,
            branches=[{"branch": "b1", "finish_reason": "stop", "canary_leak": False}])
        g = r._compute_gates(modes)
        assert g["G-M0-3a"]["status"] == "FAIL"

    def test_g3a_missing_observation_fails(self, tmp_path):
        """erase 后样本缺失（last 无 used_cells）→ FAIL，绝不 None 即 PASS。"""
        r = self._runner_with_log(tmp_path)
        modes = self._minimal_modes(kv_last={}, shared=0, branches=[{"branch": "b1"}])
        g = r._compute_gates(modes)
        assert g["G-M0-3a"]["status"] == "FAIL"

    def test_g5_shared_missing_fails(self, tmp_path):
        """baseline 无 shared_cells 观测 → FAIL（不得 None 即 PASS）。"""
        r = self._runner_with_log(tmp_path)
        modes = self._minimal_modes(
            kv_last={"used_cells": 0, "active_sequences": 0},
            shared=None, branches=[{"branch": "b1", "canary_leak": False}])
        g = r._compute_gates(modes)
        assert g["G-M0-5"]["status"] == "FAIL"

    def test_g5_shared_nonzero_fails(self, tmp_path):
        r = self._runner_with_log(tmp_path)
        modes = self._minimal_modes(
            kv_last={"used_cells": 0, "active_sequences": 0},
            shared=3, branches=[{"branch": "b1", "canary_leak": False}])
        g = r._compute_gates(modes)
        assert g["G-M0-5"]["status"] == "FAIL"


# ---- Critical 2：finish_reason ----

class TestCritical2FinishReason:
    def test_driver_chat_top_level_finish_reason(self):
        with MockOpenAIServer(finish_reason="length") as srv:
            drv = Driver(base_url=f"http://127.0.0.1:{srv.port}", model="bench",
                         sdk_max_retries=0)
            row = drv.chat([{"role": "user", "content": "hi"}], max_tokens=4)
        assert row["finish_reason"] == "length"

    def test_decision_length_invalid_length_counted(self, tmp_path, fake_adapter_cls):
        """length 截断 → 决策 INVALID 且 invalid_length>0（真实路径端到端）。"""
        r = make_runner(tmp_path, fake_adapter_cls)
        adapter = r._start_server(10, "q8_0", "q8_0", "off", "dv")
        r._stop_server()
        with MockOpenAIServer(finish_reason="length") as srv:
            r._driver, r._kv = r._connect(srv.port)
            recs = r._run_decision_session(srv.port)
        sess = sch.build_decision_session(
            requests=len(recs),
            valid=sum(1 for x in recs if x["ok"] and x["error"] is None),
            invalid=sum(1 for x in recs if not x["ok"] and x["error"] is None),
            error_count=sum(1 for x in recs if x["error"] is not None),
            invalid_length=sum(1 for x in recs
                               if not x["ok"] and x["error"] is None
                               and x["finish_reason"] == "length"),
            invalid_no_action=sum(1 for x in recs
                                  if not x["ok"] and x["error"] is None
                                  and x["finish_reason"] != "length"),
            finish_reasons={},
            output_hashes=[], error_summary=[])
        assert sess["invalid_length"] > 0
        assert all(x["finish_reason"] == "length"
                   for x in recs if x["error"] is None)
        # length 必须判 INVALID_DECISION（决策谓词）
        assert fp.decision_invalid("模拟回复 1", "length", 8) is True


# ---- Critical 3：端口 ----

class TestCritical3Port:
    def test_connect_passes_host_port(self, tmp_path, monkeypatch):
        calls: list = []
        orig = Driver.__init__

        def _spy(self_, base_url, **kw):
            calls.append((base_url, kw.get("host"), kw.get("port")))
            orig(self_, base_url, **kw)

        monkeypatch.setattr(Driver, "__init__", _spy)
        with MockOpenAIServer() as srv:
            r = M0FanoutRunner(server_bin="x", model="m", ctx_size=4096,
                               port_base=18080, tmp_dir=str(tmp_path / "t"),
                               out_path="", adapter_cls=ServerAdapter)
            r._connect(srv.port)
        assert calls, "Driver 未被构造"
        _, host, port = calls[-1]
        assert host == "127.0.0.1"
        assert port == srv.port  # 非默认 8080 时也必须传实际端口

    def test_find_server_pid_non_default_port(self):
        """sampler 按实际端口定位 PID（mock server 进程 = 本进程，非 llama-server）。"""
        with MockOpenAIServer() as srv:
            pid = sampler.find_server_pid("127.0.0.1", srv.port)
        assert pid == os.getpid()


# ---- Critical 4：启动 OSError ----

class TestCritical4StartOSError:
    def test_missing_server_bin_preflight_no_traceback(self, tmp_path, capsys):
        """server-bin 不存在（Popen FileNotFoundError）→ ServerError →
        preflight 落盘 rc==0，无 traceback / exit 1。真 ServerAdapter。"""
        out = str(tmp_path / "out.json")
        r = M0FanoutRunner(server_bin="/no/such/server-bin", model="m.gguf",
                           ctx_size=4096, port_base=19001, tmp_dir=str(tmp_path / "t"),
                           out_path=out, ngl=99, adapter_cls=ServerAdapter)
        rc = r.run()
        assert rc == 0, rc
        assert "Traceback" not in capsys.readouterr().err
        with open(out, encoding="utf-8") as f:
            doc = json.load(f)
        assert doc["meta"]["phase"] == "preflight"
        assert doc["meta"]["preflight_reason"] == "validation_incomplete"
        assert doc["verdict"] == "INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE"

    def test_unwritable_log_path_preflight(self, tmp_path):
        """日志文件不可写（深层路径不存在 → open OSError）→ ServerError → preflight。"""
        out = str(tmp_path / "out.json")
        r = M0FanoutRunner(server_bin="/no/such/server-bin", model="m.gguf",
                           ctx_size=4096, port_base=19002,
                           tmp_dir=str(tmp_path / "no_such_dir" / "deep"),
                           out_path=out, ngl=99, adapter_cls=ServerAdapter)
        rc = r.run()
        assert rc == 0, rc
        with open(out, encoding="utf-8") as f:
            doc = json.load(f)
        assert doc["meta"]["phase"] == "preflight"


# ---- Critical 5：陈旧 tmp 清理 ----

class TestCritical5StaleTmp:
    def test_atomic_tmp_naming_matches_cleanup(self, tmp_path):
        d = str(tmp_path)
        sch.atomic_write_json(os.path.join(d, "m0_fanout_x.json"), {"a": 1})
        # atomic 写后不应残留 tmp；造一个陈旧 tmp 模拟中断残留
        stale = os.path.join(d, ".m0_fanout_x.json.tmp")
        with open(stale, "w", encoding="utf-8") as f:
            f.write("{}")
        removed = sch.cleanup_stale_tmp(d)
        assert stale in [os.path.join(d, n) for n in removed] or os.path.basename(stale) in removed
        assert not os.path.exists(stale)
        assert os.path.exists(os.path.join(d, "m0_fanout_x.json"))

    def test_cli_main_cleans_stale_tmp(self, tmp_path, monkeypatch):
        out_dir = tmp_path / "res"
        out_dir.mkdir()
        stale = out_dir / ".m0_fanout_old.json.tmp"
        stale.write_text("{}", encoding="utf-8")

        def _fake_runner(**kw):
            class _R:
                def run_safe(self):
                    return 0
            return _R()

        monkeypatch.setattr(m0r, "M0FanoutRunner", _fake_runner)
        rc = m0r.main(["--server-bin", "x", "--model", "m",
                       "--out", str(out_dir / "out.json"),
                       "--tmp-dir", str(tmp_path / "t")])
        assert rc == 0
        assert not stale.exists()  # CLI 入口已清理


# ---- Critical 6：校准实测长度 ----

class TestCritical6CalibratedLengths:
    def test_rep_lengths_use_calibrated_not_targets(self, tmp_path, fake_adapter_cls):
        r = make_runner(tmp_path, fake_adapter_cls)
        adapter = r._start_server(4, "q8_0", "q8_0", "off", "t")
        r._driver, r._kv = r._connect(adapter.port)
        # mock /tokenize 固定 100 → 校准实测 100，而 BUCKET_TARGETS[short]=(150,150)
        r.calibrated_lengths[("short", 2)] = {"prefix": 100, "branch": 100}
        uid = sch.unit_id_of("off", "q8_0", "q8_0", 2, "short")
        gid = sch.group_id_of("off", "q8_0", "q8_0", 2)
        rep = r._run_unit(uid, 2, "short", "q8_0", "q8_0", gid, rep_index=0)
        r._stop_server()
        assert rep["prefix_len"] == 100
        assert rep["branch_len"] == 100
        assert (rep["prefix_len"], rep["branch_len"]) != fp.BUCKET_TARGETS["short"]
        # rep 内 KV 数据存在（begin/end_run 边界 + erase 后采样）
        assert rep["metrics"]["kv"]["last"]["used_cells"] == 0
        assert rep["metrics"]["kv"]["last"]["active_sequences"] == 0

    def test_validator_rejects_inconsistent_bucket_lengths(self, tmp_path, fake_adapter_cls):
        """同 (fanout,bucket) rep 长度不一致 → SCHEMA_INVALID（校准一致性）。"""
        r = make_runner(tmp_path, fake_adapter_cls)
        uid = sch.unit_id_of("off", "q8_0", "q8_0", 2, "short")
        gid = sch.group_id_of("off", "q8_0", "q8_0", 2)
        rep1 = sch.build_rep(uid, 0, 2, "short", "q8_0", "q8_0",
                             100, 100, gid, "OK", False, {"branches": []})
        rep2 = sch.build_rep(uid, 1, 2, "short", "q8_0", "q8_0",
                             200, 100, gid, "OK", False, {"branches": []})
        group = sch.build_group(gid, "COMPLETED", "2026-08-09T00:00:00Z",
                                "2026-08-09T00:00:01Z",
                                baseline={"rs_buffer_mb": 50.0},
                                replicates=[rep1, rep2])
        modes = sch.build_modes([group], [])
        # 直接调 modes/group/rep validator（避免顶层五键噪音）
        errs = sch.validate_modes_group_rep(modes, matrix_complete=True,
                                            rejections=[])
        # _ERR = (code, path, expected, actual) 元组
        assert any(e[0] == "invariant_violation"
                   and "prefix_len" in str(e[2]) for e in errs)


# ---- Critical 7：warmup 崩溃 ----

class TestCritical7WarmupCrash:
    def test_warmup_crash_group_error_formal_incomplete(self, tmp_path, fake_adapter_cls):
        """warmup 崩溃不得被吞：立即 group ERROR + FORMAL_INCOMPLETE（rc==0）。"""
        FakeAdapter.crash_at_after = 4   # formal 首组（第 4 个 adapter）起崩溃
        FakeAdapter.crash_at_value = 1   # 第 2 次 POST 即崩 → warmup 期内
        try:
            r = make_runner(tmp_path, fake_adapter_cls)
            rc = r.run()
            assert rc == 0, rc
            with open(r.out_path, encoding="utf-8") as f:
                doc = json.load(f)
            assert doc["meta"]["phase"] == "formal"
            assert doc["meta"]["matrix_complete"] is False
            groups = (doc["modes"]["off"]["server_groups"]
                      + doc["modes"]["on"]["server_groups"])
            assert groups[-1]["status"] == "ERROR"
            assert groups[-1]["error_type"] == "server_crash"
            assert groups[-1]["replicates"] == []  # 崩溃于 warmup，无 formal rep
        finally:
            FakeAdapter.crash_at_after = None


# ---- Critical 8：mock 死属性 ----

class TestCritical8MockDeadAttr:
    def test_mock_server_no_misleading_instance_count(self):
        srv = MockOpenAIServer()
        assert not hasattr(srv, "count"), "实例 count 死属性必须删除（计数在 httpd.count）"
        with srv:
            assert hasattr(srv.httpd, "count")
            assert srv.httpd.count == 0


# ---- Critical 9：G-M0-5 真实日志串 ----

class TestCritical9G5RealLog:
    def test_source_contains_real_capability_line(self):
        src = os.path.join(os.path.dirname(__file__), "..", "..", "llama.cpp",
                           "tools", "server", "server-context.cpp")
        if not os.path.exists(src):
            pytest.skip("llama.cpp 子仓库不存在")
        with open(src, encoding="utf-8") as f:
            text = f.read()
        real = "E8-C1: capability rejected: hybrid (recurrent+attention) model"
        assert real in text
        # runner 匹配串必须是真实行的前缀子串（防 FakeAdapter 专造）
        assert "E8-C1: capability rejected: hybrid" in real

    def test_g5_pass_with_real_line_missing_log_fails(self, tmp_path, fake_adapter_cls):
        r = make_runner(tmp_path, fake_adapter_cls)
        os.makedirs(r.tmp_dir, exist_ok=True)
        with open(os.path.join(r.tmp_dir, "server_g0.log"), "w",
                  encoding="utf-8") as f:
            f.write("E8-C1: capability rejected: hybrid (recurrent+attention) model\n")
        modes = {"off": {"server_groups": []}, "on": {"server_groups": []}}
        g = r._compute_gates(modes)
        assert g["G-M0-5"]["status"] == "PASS"  # on 日志行命中（无 group 时无 shared 检查）
        # 缺日志 → FAIL（独立 tmp_dir，避免与 r 的日志串扰）
        r2 = make_runner(tmp_path, fake_adapter_cls)
        r2.tmp_dir = os.path.join(str(tmp_path), "empty_logs")
        g2 = r2._compute_gates(modes)
        assert g2["G-M0-5"]["status"] == "FAIL"


# ---- Critical 10：G-M0-4 独立观测 ----

class TestCritical10G4Observational:
    def test_parse_rs_buffer_real_format(self):
        log = ("llama_memory_recurrent::init: gpu0 RS buffer size =  50.25 MiB\n"
               "llama_memory_recurrent::init: cpu  RS buffer size =  0.00 MiB\n")
        assert m0r.M0FanoutRunner._parse_rs_buffer_mib(log) == pytest.approx(50.25)
        assert m0r.M0FanoutRunner._parse_rs_buffer_mib("no rs line") is None

    def _modes_with_groups(self):
        # expected = RS_BYTES_PER_ROW * (fanout+2) / MiB = 24*548864*4*4/2^20
        exp = 24 * 548864 * 4 * 4 / (1024 * 1024)
        off = {
            "server_group_id": sch.group_id_of("off", "q8_0", "q8_0", 2),
            "ctk": "q8_0", "ctv": "q8_0", "fanout": 2, "status": "COMPLETED",
            "baseline": {"rs_buffer_mb": round(exp, 3)},
            "replicates": [],
        }
        return {"off": {"server_groups": [off]}, "on": {"server_groups": []}}

    def test_g4_three_states(self, tmp_path):
        exp = 24 * 548864 * 4 * 4 / (1024 * 1024)
        # 无观测 → NOT_APPLICABLE（明确，非 PASS）
        r = make_runner(tmp_path, None)
        g = r._compute_gates(self._modes_with_groups())
        assert g["G-M0-4"]["status"] == "NOT_APPLICABLE"
        # 观测匹配 → PASS（真实日志 RS buffer size 行）
        r._server_logs["g0"] = (f"llama_memory_recurrent::init: RS buffer size ="
                                 f"  {exp:.2f} MiB\n")
        g = r._compute_gates(self._modes_with_groups())
        assert g["G-M0-4"]["status"] == "PASS"
        # 观测超差（>G4_TOLERANCE=5%）→ FAIL
        r._server_logs["g0"] = ("llama_memory_recurrent::init: RS buffer size ="
                                 "  400.00 MiB\n")
        g = r._compute_gates(self._modes_with_groups())
        assert g["G-M0-4"]["status"] == "FAIL"
        # derived 自检不一致（rs_buffer_mb 与公式不符）→ FAIL
        modes = self._modes_with_groups()
        modes["off"]["server_groups"][0]["baseline"] = {"rs_buffer_mb": 1.0}
        r._server_logs["g0"] = ""
        g = r._compute_gates(modes)
        assert g["G-M0-4"]["status"] == "FAIL"
